"""Comptes d'accès au dashboard (en plus de l'admin défini dans le .env).

Fichier data/users.json (droits 600) : {"lea": {"hash": "...", "salt": "...", "role": "viewer"}}
Mots de passe hachés avec scrypt ; jamais stockés en clair.

Rôles :
  admin  : tout (dont /setup et /callback)
  viewer : lecture seule (dashboard et API de lecture)
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import string
from pathlib import Path

from .config import settings

ROLES = ("viewer", "admin")
_SCRYPT = {"n": 2**14, "r": 8, "p": 1, "dklen": 32}


def users_path() -> Path:
    return Path(settings.db_path).parent / "users.json"


def _hash(password: str, salt: bytes) -> str:
    return hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT).hex()


def generate_password() -> str:
    """16 caractères minuscules/chiffres en 4 groupes : facile à taper sur mobile (~82 bits)."""
    alphabet = string.ascii_lowercase.replace("l", "").replace("o", "") + "23456789"
    raw = "".join(secrets.choice(alphabet) for _ in range(16))
    return "-".join(raw[i:i + 4] for i in range(0, 16, 4))


def load() -> dict[str, dict]:
    p = users_path()
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save(users: dict[str, dict]) -> None:
    p = users_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(users, fh, indent=2)
    os.replace(tmp, p)


def add(name: str, role: str = "viewer", password: str | None = None) -> str:
    name = name.strip().lower()
    if not name or ":" in name:
        raise ValueError("nom invalide")
    if name == settings.dashboard_user.lower():
        raise ValueError(f"« {name} » est le compte admin du .env")
    if role not in ROLES:
        raise ValueError(f"rôle inconnu : {role}")
    password = password or generate_password()
    salt = secrets.token_bytes(16)
    users = load()
    users[name] = {"hash": _hash(password, salt), "salt": salt.hex(), "role": role}
    _save(users)
    return password


def remove(name: str) -> bool:
    users = load()
    if users.pop(name.strip().lower(), None) is None:
        return False
    _save(users)
    return True


def verify(name: str, password: str) -> str | None:
    """Retourne le rôle si les identifiants sont valides, sinon None."""
    if secrets.compare_digest(name.encode(), settings.dashboard_user.encode()) and settings.dashboard_password:
        return "admin" if secrets.compare_digest(password.encode(), settings.dashboard_password.encode()) else None
    u = load().get(name.strip().lower())
    if not u:
        _hash(password, b"0" * 16)  # temps constant : ne révèle pas si l'utilisateur existe
        return None
    ok = secrets.compare_digest(_hash(password, bytes.fromhex(u["salt"])), u["hash"])
    return u.get("role", "viewer") if ok else None
