"""Jetons d'API pour les autres applications (lecture seule).

Fichier data/tokens.json (droits 600) : {"budget": {"hash": "<sha256>", "created": "2026-11-05T22:00:00"}}
Le jeton lui-même (préfixe « bw_ », 256 bits d'entropie) n'est affiché qu'à la création et n'est
jamais stocké en clair ; sha256 suffit car il n'est pas devinable (pas de force brute possible).

Usage côté client :  Authorization: Bearer bw_…
Droits : identiques au rôle « viewer » (API de lecture, jamais /setup ni /callback).
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from datetime import datetime
from pathlib import Path

from .config import settings

PREFIX = "bw_"


def tokens_path() -> Path:
    return Path(settings.db_path).parent / "tokens.json"


def _load() -> dict[str, dict]:
    p = tokens_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


def _save(data: dict[str, dict]) -> None:
    p = tokens_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    os.chmod(p, 0o600)


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create(name: str) -> str:
    """Crée (ou remplace) le jeton de l'application `name` et le retourne — à afficher une seule fois."""
    name = name.strip().lower()
    if not name or not all(c.isalnum() or c in "-_" for c in name):
        raise ValueError("nom : lettres, chiffres, - et _ uniquement")
    token = PREFIX + secrets.token_urlsafe(32)
    data = _load()
    data[name] = {"hash": _digest(token), "created": datetime.now().isoformat(timespec="seconds")}
    _save(data)
    return token


def verify(token: str | None) -> str | None:
    """Nom de l'application propriétaire du jeton, ou None."""
    if not token or not token.startswith(PREFIX):
        return None
    d = _digest(token)
    for name, rec in _load().items():
        if hmac.compare_digest(rec.get("hash", ""), d):
            return name
    return None


def list_tokens() -> dict[str, dict]:
    return _load()


def revoke(name: str) -> bool:
    data = _load()
    if data.pop(name.strip().lower(), None) is None:
        return False
    _save(data)
    return True
