"""Client Enable Banking (https://enablebanking.com) — API AIS, mode « restricted » pour usage perso.

Authentification : JWT RS256 signé avec la clé privée de l'application (fichier .pem
téléchargé à la création de l'app dans le Control Panel), `kid` = Application ID.

Flux :
  1. aspsps("FR")              -> banques disponibles (nom exact à passer à /auth)
  2. start_auth(nom, pays)     -> URL à ouvrir : consentement + SCA sur l'appli de la banque
  3. la banque redirige vers BW_EB_REDIRECT_URL?code=...&state=...
  4. create_session(code)      -> session + comptes (uid)
  5. balances(uid), transactions(uid, date_from) (pagination par continuation_key)
"""

from __future__ import annotations

import time
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import jwt

from .config import settings


class EnableBankingError(RuntimeError):
    pass


class EnableBankingClient:
    def __init__(self, app_id: str | None = None, key_path: str | Path | None = None,
                 base_url: str | None = None, http: httpx.Client | None = None) -> None:
        self.app_id = app_id or settings.eb_app_id
        self.key_path = Path(key_path or settings.eb_key_path)
        self.base_url = (base_url or settings.eb_base_url).rstrip("/")
        if not self.app_id:
            raise EnableBankingError("BW_EB_APP_ID manquant (Application ID du Control Panel)")
        if not self.key_path.exists():
            raise EnableBankingError(f"Clé privée introuvable : {self.key_path} (BW_EB_KEY_PATH)")
        self._key = self.key_path.read_bytes()
        self._http = http or httpx.Client(base_url=self.base_url, timeout=40)
        self._jwt: str | None = None
        self._jwt_exp = 0.0

    # ------------------------------------------------------------------ auth
    def _token(self) -> str:
        now = int(time.time())
        if self._jwt and now < self._jwt_exp - 60:
            return self._jwt
        exp = now + 3600
        self._jwt = jwt.encode(
            {"iss": "enablebanking.com", "aud": "api.enablebanking.com", "iat": now, "exp": exp},
            self._key, algorithm="RS256", headers={"kid": self.app_id},
        )
        self._jwt_exp = exp
        return self._jwt

    def _req(self, method: str, path: str, **kw: Any) -> Any:
        headers = {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}
        for attempt in range(3):
            r = self._http.request(method, path, headers=headers, **kw)
            if r.status_code == 429 and attempt < 2:
                time.sleep(2 * (attempt + 1))
                continue
            if r.status_code >= 400:
                try:
                    detail = r.json()
                except Exception:
                    detail = r.text[:500]
                raise EnableBankingError(f"HTTP {r.status_code} {method} {path}: {detail}")
            return r.json() if r.content else {}
        raise EnableBankingError(f"429 persistant sur {path}")

    # ----------------------------------------------------------- application
    def application(self) -> dict[str, Any]:
        return self._req("GET", "/application")

    def aspsps(self, country: str = "FR") -> list[dict[str, Any]]:
        return self._req("GET", "/aspsps", params={"country": country.upper()}).get("aspsps", [])

    def find_aspsp(self, query: str, country: str = "FR") -> list[dict[str, Any]]:
        q = query.lower()
        return [a for a in self.aspsps(country) if q in a.get("name", "").lower()]

    # ------------------------------------------------------- consentement
    def start_auth(self, aspsp_name: str, country: str = "FR", state: str | None = None,
                   valid_days: int | None = None, psu_type: str = "personal") -> dict[str, Any]:
        """Retourne {'url': ..., 'authorization_id': ..., 'state': ...}."""
        state = state or uuid.uuid4().hex
        valid_until = datetime.now(timezone.utc) + timedelta(days=valid_days or settings.eb_access_days)
        body = {
            "access": {"valid_until": valid_until.isoformat(timespec="seconds")},
            "aspsp": {"name": aspsp_name, "country": country.upper()},
            "state": state,
            "redirect_url": settings.eb_redirect_url,
            "psu_type": psu_type,
        }
        data = self._req("POST", "/auth", json=body)
        data["state"] = state
        return data

    def create_session(self, code: str) -> dict[str, Any]:
        return self._req("POST", "/sessions", json={"code": code})

    def session(self, session_id: str) -> dict[str, Any]:
        return self._req("GET", f"/sessions/{session_id}")

    # ---------------------------------------------------------------- comptes
    def account_details(self, uid: str) -> dict[str, Any]:
        return self._req("GET", f"/accounts/{uid}/details")

    def balances(self, uid: str) -> list[dict[str, Any]]:
        return self._req("GET", f"/accounts/{uid}/balances").get("balances", [])

    def transactions(self, uid: str, date_from: date | None = None, max_pages: int = 50) -> list[dict[str, Any]]:
        params: dict[str, str] = {}
        if date_from:
            params["date_from"] = date_from.isoformat()
        out: list[dict[str, Any]] = []
        for _ in range(max_pages):
            data = self._req("GET", f"/accounts/{uid}/transactions", params=params)
            out.extend(data.get("transactions", []))
            key = data.get("continuation_key")
            if not key:
                break
            params["continuation_key"] = key
        return out


# ------------------------------------------------------------- normalisation
def session_accounts(session: dict[str, Any]) -> list[dict[str, Any]]:
    """Les comptes d'une session : objets {uid, account_id:{iban}, name, currency...} ou simples uid."""
    out = []
    for a in session.get("accounts", []):
        if isinstance(a, str):
            out.append({"uid": a})
        else:
            out.append(a)
    return out


def account_label(acc: dict[str, Any]) -> tuple[str | None, str | None, str]:
    """(iban, nom, devise)."""
    acc_id = acc.get("account_id") or {}
    iban = acc_id.get("iban") if isinstance(acc_id, dict) else None
    name = acc.get("name") or acc.get("product") or acc.get("details") or None
    return iban, name, acc.get("currency") or "EUR"


def pick_balance(balances: list[dict[str, Any]]) -> tuple[float, str | None] | None:
    prefer = ("ITAV", "CLAV", "XPCD", "ITBD", "CLBD", "OPBD", "PRCD", "OTHR")
    by_type = {b.get("balance_type"): b for b in balances}
    for t in prefer:
        if t in by_type:
            b = by_type[t]
            return float(b["balance_amount"]["amount"]), b.get("reference_date")
    if balances:
        b = balances[0]
        return float(b["balance_amount"]["amount"]), b.get("reference_date")
    return None


def to_raw(tx: dict[str, Any]) -> dict[str, Any]:
    """Convertit une transaction Enable Banking au format interne (proche Berlin Group / GoCardless)
    pour réutiliser `db.tx_from_gocardless`."""
    amount = float(tx["transaction_amount"]["amount"])
    if tx.get("credit_debit_indicator") == "DBIT":
        amount = -abs(amount)
    elif tx.get("credit_debit_indicator") == "CRDT":
        amount = abs(amount)
    remit = tx.get("remittance_information") or []
    if isinstance(remit, str):
        remit = [remit]
    creditor = (tx.get("creditor") or {}).get("name") or ""
    debtor = (tx.get("debtor") or {}).get("name") or ""
    booking = tx.get("booking_date") or tx.get("transaction_date") or tx.get("value_date")
    raw = {
        "transactionId": tx.get("entry_reference") or tx.get("transaction_id"),
        "bookingDate": booking,
        "valueDate": tx.get("value_date"),
        "transactionAmount": {"amount": str(amount), "currency": tx["transaction_amount"].get("currency", "EUR")},
        "creditorName": creditor,
        "debtorName": debtor,
        "remittanceInformationUnstructuredArray": [r for r in remit if r],
        "additionalInformation": tx.get("note") or "",
        "_eb": tx,
    }
    return raw


def is_pending(tx: dict[str, Any]) -> bool:
    return tx.get("status") in ("PDNG", "pending", "INFO")
