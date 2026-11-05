"""Webhooks sortants au format CloudEvents (https://cloudevents.io), signés HMAC-SHA256.

Abonnés dans rules/webhooks.json :
  [{"name": "budget", "url": "http://127.0.0.1:8118/hooks/bankwatch", "secret_env": "BW_WEBHOOK_SECRET_BUDGET",
    "events": ["sync.completed"]}]
Le secret partagé vit dans l'environnement (.env), jamais dans le fichier de règles.

Requête envoyée :
  POST <url>
  Content-Type: application/cloudevents+json
  X-BankWatch-Event: sync.completed
  X-BankWatch-Signature: t=<timestamp unix>,v1=<hex HMAC-SHA256(secret, "<t>.<corps>")>
Le récepteur recalcule la signature sur le corps brut, vérifie v1 en temps constant et rejette un
horodatage de plus de 5 minutes (anti-rejeu). Même schéma que Stripe / GitHub.

Types d'événements (préfixe « dev.bankwatch. » dans l'enveloppe) :
  sync.completed : {accounts: [{id, bank, new}], new_transactions, errors}
  alert.created  : {kind, severity, title, amount, account_id}
Un échec d'envoi est journalisé, réessayé 3 fois, et n'interrompt jamais la synchro.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger("bankwatch.webhooks")
WEBHOOKS_FILE = Path("rules/webhooks.json")
EVENT_PREFIX = "dev.bankwatch."
SOURCE = "urn:bankwatch"
RETRIES = 3


def load_subscribers(path: Path | None = None) -> list[dict[str, Any]]:
    p = path or WEBHOOKS_FILE
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    subs = data.get("subscribers", data) if isinstance(data, dict) else data
    return [s for s in subs if isinstance(s, dict) and s.get("url")]


def build_event(event_type: str, data: dict[str, Any], source: str = SOURCE) -> dict[str, Any]:
    return {
        "specversion": "1.0",
        "id": uuid.uuid4().hex,
        "source": source,
        "type": EVENT_PREFIX + event_type,
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "datacontenttype": "application/json",
        "data": data,
    }


def sign(secret: str, body: bytes, ts: int | None = None) -> str:
    ts = int(time.time()) if ts is None else ts
    mac = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return f"t={ts},v1={mac}"


def verify_signature(secret: str, body: bytes, header: str | None, tolerance: int = 300,
                     now: int | None = None) -> bool:
    """Côté récepteur (fourni ici pour les tests et pour les applis en Python)."""
    if not header:
        return False
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    try:
        ts = int(parts.get("t", ""))
    except ValueError:
        return False
    if abs((now if now is not None else int(time.time())) - ts) > tolerance:
        return False
    expected = sign(secret, body, ts).split("v1=")[1]
    return hmac.compare_digest(expected, parts.get("v1", ""))


def _secret_for(sub: dict[str, Any]) -> str:
    if sub.get("secret_env"):
        name = sub["secret_env"]
        if os.environ.get(name):
            return os.environ[name]
        try:  # pas dans l'environnement du processus : on lit le .env (comme pydantic-settings)
            from dotenv import dotenv_values

            return str(dotenv_values(".env").get(name) or "")
        except ImportError:
            return ""
    return str(sub.get("secret", ""))


def emit(event_type: str, data: dict[str, Any], subscribers: list[dict[str, Any]] | None = None,
         timeout: float = 5.0) -> list[dict[str, Any]]:
    """Envoie l'événement à chaque abonné intéressé. Retourne un compte rendu par abonné."""
    subs = load_subscribers() if subscribers is None else subscribers
    event = build_event(event_type, data)
    body = json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode()
    report = []
    for sub in subs:
        wanted = sub.get("events")
        if wanted and event_type not in wanted and "*" not in wanted:
            continue
        secret = _secret_for(sub)
        if not secret:
            log.warning("webhook %s : secret absent (%s), envoi ignoré", sub.get("name"), sub.get("secret_env"))
            report.append({"name": sub.get("name"), "ok": False, "error": "secret manquant"})
            continue
        headers = {
            "Content-Type": "application/cloudevents+json",
            "X-BankWatch-Event": event_type,
            "X-BankWatch-Delivery": event["id"],
            "User-Agent": "BankWatch-Webhooks/1.0",
        }
        ok, status, last_err = False, None, None
        for attempt in range(RETRIES):
            headers["X-BankWatch-Signature"] = sign(secret, body)
            try:
                r = httpx.post(sub["url"], content=body, headers=headers, timeout=timeout)
                status = r.status_code
                if r.status_code < 300:
                    ok = True
                    break
                last_err = f"HTTP {r.status_code}"
                if 400 <= r.status_code < 500 and r.status_code != 429:
                    break  # erreur de notre côté (signature, URL) : inutile d'insister
            except httpx.HTTPError as e:
                last_err = str(e)
            if attempt < RETRIES - 1:
                time.sleep(1.5 * (attempt + 1))
        report.append({"name": sub.get("name"), "ok": ok, "status": status, "error": None if ok else last_err})
        if not ok:
            log.warning("webhook %s : échec (%s)", sub.get("name"), last_err)
    return report


def emit_sync_completed(results: list[Any], new_count: int) -> list[dict[str, Any]]:
    data = {
        "accounts": [{"id": r.account_id, "bank": r.bank, "new": r.new_count, "error": r.error}
                     for r in results],
        "new_transactions": new_count,
        "errors": sum(1 for r in results if r.error),
    }
    return emit("sync.completed", data)
