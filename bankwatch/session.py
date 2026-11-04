"""Sessions du dashboard : cookie signé (HMAC-SHA256), 30 jours, HttpOnly + Secure.

Clé de signature : BW_SECRET_KEY, sinon générée une fois dans data/secret.key (droits 600).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from .config import settings

COOKIE = "bw_session"
MAX_AGE = 30 * 24 * 3600
_key_cache: bytes | None = None


def secret_key() -> bytes:
    global _key_cache
    if _key_cache:
        return _key_cache
    if settings.secret_key:
        _key_cache = settings.secret_key.encode()
        return _key_cache
    p = Path(settings.db_path).parent / "secret.key"
    if p.exists():
        _key_cache = p.read_bytes().strip()
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        key = secrets.token_urlsafe(32).encode()
        fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            fh.write(key)
        _key_cache = key
    return _key_cache


def _sign(payload: bytes) -> str:
    return base64.urlsafe_b64encode(hmac.new(secret_key(), payload, hashlib.sha256).digest()).decode().rstrip("=")


def issue(user: str, role: str, now: float | None = None) -> str:
    data = {"u": user, "r": role, "exp": int((now or time.time()) + MAX_AGE)}
    payload = base64.urlsafe_b64encode(json.dumps(data, separators=(",", ":")).encode()).decode().rstrip("=")
    return f"{payload}.{_sign(payload.encode())}"


def verify(token: str | None, now: float | None = None) -> tuple[str, str] | None:
    """Retourne (user, role) si le cookie est valide et non expiré."""
    if not token or "." not in token:
        return None
    payload, _, sig = token.rpartition(".")
    if not hmac.compare_digest(sig, _sign(payload.encode())):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    except Exception:
        return None
    if data.get("exp", 0) < (now or time.time()):
        return None
    return data["u"], data["r"]
