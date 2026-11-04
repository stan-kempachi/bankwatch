"""Client minimal pour l'API GoCardless Bank Account Data (ex-Nordigen).

Flux :
  1. token()            -> jeton d'accès (secret_id / secret_key)
  2. institutions()     -> liste des banques du pays (ids type SG_SOGEFRPP)
  3. create_requisition(institution_id) -> lien à ouvrir pour donner son consentement
  4. requisition(id)    -> liste des account_ids une fois le consentement donné
  5. transactions(account_id, date_from) -> transactions bookées + pending
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import date
from typing import Any

import httpx

from .config import settings


class GoCardlessError(RuntimeError):
    pass


@dataclass
class Institution:
    id: str
    name: str
    bic: str
    transaction_total_days: int
    max_access_valid_for_days: int


class GoCardlessClient:
    def __init__(
        self,
        secret_id: str | None = None,
        secret_key: str | None = None,
        base_url: str | None = None,
    ) -> None:
        self.secret_id = secret_id or settings.gc_secret_id
        self.secret_key = secret_key or settings.gc_secret_key
        self.base_url = (base_url or settings.gc_base_url).rstrip("/")
        if not self.secret_id or not self.secret_key:
            raise GoCardlessError(
                "BW_GC_SECRET_ID / BW_GC_SECRET_KEY manquants (portail bankaccountdata.gocardless.com)"
            )
        self._http = httpx.Client(base_url=self.base_url, timeout=30)
        self._access: str | None = None
        self._access_exp: float = 0

    # ------------------------------------------------------------------ auth
    def _token(self) -> str:
        if self._access and time.time() < self._access_exp - 60:
            return self._access
        r = self._http.post(
            "/token/new/", json={"secret_id": self.secret_id, "secret_key": self.secret_key}
        )
        self._raise(r)
        data = r.json()
        self._access = data["access"]
        self._access_exp = time.time() + int(data.get("access_expires", 86400))
        return self._access

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token()}", "Accept": "application/json"}

    @staticmethod
    def _raise(r: httpx.Response) -> None:
        if r.status_code >= 400:
            try:
                detail = r.json()
            except Exception:
                detail = r.text
            raise GoCardlessError(f"HTTP {r.status_code} {r.request.url}: {detail}")

    def _get(self, path: str, **params: Any) -> Any:
        r = self._http.get(path, headers=self._headers(), params=params or None)
        self._raise(r)
        return r.json()

    def _post(self, path: str, payload: dict[str, Any]) -> Any:
        r = self._http.post(path, headers=self._headers(), json=payload)
        self._raise(r)
        return r.json()

    # ---------------------------------------------------------- institutions
    def institutions(self, country: str | None = None) -> list[Institution]:
        data = self._get("/institutions/", country=(country or settings.gc_country).lower())
        return [
            Institution(
                id=i["id"],
                name=i["name"],
                bic=i.get("bic", ""),
                transaction_total_days=int(i.get("transaction_total_days") or 90),
                max_access_valid_for_days=int(i.get("max_access_valid_for_days") or 90),
            )
            for i in data
        ]

    def find_institution(self, query: str, country: str | None = None) -> list[Institution]:
        q = query.lower()
        return [i for i in self.institutions(country) if q in i.name.lower() or q in i.id.lower()]

    # ----------------------------------------------------------- requisitions
    def create_requisition(
        self, institution: Institution, reference: str, user_language: str = "FR"
    ) -> dict[str, Any]:
        """Crée un accord utilisateur + une réquisition. Retourne {'id', 'link', ...}."""
        agreement = self._post(
            "/agreements/enduser/",
            {
                "institution_id": institution.id,
                "max_historical_days": min(
                    settings.gc_history_days, institution.transaction_total_days
                ),
                "access_valid_for_days": min(
                    settings.gc_access_days, institution.max_access_valid_for_days
                ),
                "access_scope": ["balances", "details", "transactions"],
            },
        )
        return self._post(
            "/requisitions/",
            {
                "redirect": settings.gc_redirect_url,
                "institution_id": institution.id,
                "reference": reference,
                "agreement": agreement["id"],
                "user_language": user_language,
            },
        )

    def requisition(self, requisition_id: str) -> dict[str, Any]:
        return self._get(f"/requisitions/{requisition_id}/")

    def requisitions(self) -> list[dict[str, Any]]:
        data = self._get("/requisitions/", limit=100)
        return data.get("results", data)

    # --------------------------------------------------------------- accounts
    def account(self, account_id: str) -> dict[str, Any]:
        return self._get(f"/accounts/{account_id}/")

    def account_details(self, account_id: str) -> dict[str, Any]:
        return self._get(f"/accounts/{account_id}/details/").get("account", {})

    def balances(self, account_id: str) -> list[dict[str, Any]]:
        return self._get(f"/accounts/{account_id}/balances/").get("balances", [])

    def transactions(
        self, account_id: str, date_from: date | None = None, date_to: date | None = None
    ) -> dict[str, list[dict[str, Any]]]:
        params: dict[str, str] = {}
        if date_from:
            params["date_from"] = date_from.isoformat()
        if date_to:
            params["date_to"] = date_to.isoformat()
        data = self._get(f"/accounts/{account_id}/transactions/", **params)
        tx = data.get("transactions", {})
        return {"booked": tx.get("booked", []), "pending": tx.get("pending", [])}
