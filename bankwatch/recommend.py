"""Recommandations de virements entre tes comptes.

Besoin  = compte à découvert (montant + marge) ou prélèvements à venir non couverts (manque calculé par forecast).
Source  = autre compte avec un surplus = solde − ses propres prélèvements à venir (fenêtre + règlement de carte
          à prévoir) − marge. Les comptes personnels sont proposés avant les comptes pro.

Config (rules/forecast.json) :
  "accounts": {"indy": {"pro": true, "label": "Indy (compte pro EI)"}, "boursobank": {}, "sg": {}}
  "transfer_round": 5      # arrondi des montants recommandés
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .db import Database
from .forecast import BANKS, Coverage, coverage, load_config
from .overdraft import current_overdrafts
from .rules import MEDIUM, Alert, eur
from .sync import wallet_banks


@dataclass
class Need:
    account_id: str
    bank: str
    name: str
    amount: float
    reasons: list[str] = field(default_factory=list)


@dataclass
class Source:
    account_id: str
    bank: str
    name: str
    balance: float
    reserved: float      # prélèvements à venir + marge + règlements de carte à prévoir
    pro: bool

    @property
    def available(self) -> float:
        return round(max(0.0, self.balance - self.reserved), 2)


@dataclass
class Recommendation:
    amount: float
    from_account: Source
    to: Need
    note: str = ""

    @property
    def fingerprint(self) -> str:
        return f"reco:{self.to.account_id}:{self.from_account.account_id}:{int(self.amount // 10)}"


def display_names(db: Database) -> dict[str, str]:
    """« Société Générale » si la banque n'a qu'un compte, sinon « Société Générale · …0042 » (fin d'IBAN ou nom)."""
    accs = db.accounts(include_ignored=False)
    counts: dict[str, int] = {}
    for a in accs:
        counts[a["bank"]] = counts.get(a["bank"], 0) + 1
    out = {}
    for a in accs:
        base = BANKS.get(a["bank"], a["bank"])
        if counts[a["bank"]] > 1:
            suffix = ("…" + a["iban"][-4:]) if a["iban"] else (a["name"] or a["id"][:6])
            base = f"{base} · {suffix}"
        out[a["id"]] = base
    return out


def _acc_cfg(cfg_raw: dict, bank: str) -> dict:
    return (cfg_raw.get("accounts") or {}).get(bank, {})


def analyse(db: Database, today: date | None = None) -> tuple[list[Need], list[Source], list[Recommendation]]:
    today = today or date.today()
    cfg = load_config()
    import json

    from .forecast import FORECAST_FILE

    raw = json.loads(FORECAST_FILE.read_text(encoding="utf-8")) if FORECAST_FILE.exists() else {}
    rnd = float(raw.get("transfer_round", 5))
    covs: dict[str, Coverage] = {c.account_id: c for c in coverage(db, today, cfg)}
    ods = {o.account_id: o for o in current_overdrafts(db, today)}

    needs: list[Need] = []
    sources: list[Source] = []
    for acc_id, c in covs.items():
        if _acc_cfg(raw, c.bank).get("wallet") or c.bank in wallet_banks():
            continue  # portefeuille (PayPal) : ni besoin, ni source
        reasons = []
        need = 0.0
        if acc_id in ods:
            od = ods[acc_id]
            need = max(need, od.amount + c.margin)
            reasons.append(f"découvert de {eur(od.amount)} depuis le {od.since:%d/%m} ({od.days} j)")
        if c.shortfall > 0:
            need = max(need, c.shortfall)
            nxt = min(i.due for i in c.items)
            reasons.append(f"{eur(c.total)} de prélèvements avant le {nxt:%d/%m} non couverts")
        if need > 0:
            needs.append(Need(acc_id, c.bank, c.name, math.ceil(need / rnd) * rnd, reasons))
        else:
            reserved = c.total + c.margin + sum(i.amount for i in c.later)
            sources.append(Source(acc_id, c.bank, c.name, c.balance, round(reserved, 2), bool(_acc_cfg(raw, c.bank).get("pro"))))

    sources.sort(key=lambda s: (s.pro, -s.available))  # perso d'abord, puis le plus garni
    recos: list[Recommendation] = []
    remaining = {s.account_id: s.available for s in sources}
    for n in needs:
        left = n.amount
        for s in sources:
            if left <= 0:
                break
            avail = remaining[s.account_id]
            if avail < rnd:
                continue
            amt = min(left, math.floor(avail / rnd) * rnd)
            if amt <= 0:
                continue
            note = _acc_cfg(raw, s.bank).get("note", "")
            if s.pro and not note:
                note = "compte pro : à passer en prélèvement de l'exploitant / rémunération"
            recos.append(Recommendation(amt, s, n, note))
            remaining[s.account_id] = round(avail - amt, 2)
            left = round(left - amt, 2)
        if left > 0 and needs:
            recos.append(Recommendation(left, Source("", "", "", 0, 0, False), n, "aucun compte n'a assez de disponible"))
    return needs, sources, recos


def detect_recommendations(db: Database, today: date | None = None) -> list[Alert]:
    today = today or date.today()
    _, _, recos = analyse(db, today)
    names = display_names(db)
    alerts: list[Alert] = []
    for r in recos:
        if not r.from_account.account_id:
            continue
        to_bank, from_bank = names.get(r.to.account_id, r.to.bank), names.get(r.from_account.account_id, r.from_account.bank)
        why = " ; ".join(r.to.reasons)
        alerts.append(Alert(
            kind="recommendation", severity=MEDIUM,
            title=f"💡 Virer {eur(r.amount)} de {from_bank} vers {to_bank}",
            message=f"{to_bank} : {why}.\n{from_bank} : {eur(r.from_account.balance)} de solde, "
                    f"{eur(r.from_account.available)} disponibles après ses propres échéances et la marge."
                    + (f"\n{r.note}" if r.note else ""),
            fingerprint=f"{r.fingerprint}:{today.isocalendar()[1]}", amount=r.amount))
    return alerts


def recommendation_dicts(db: Database, today: date | None = None) -> dict[str, Any]:
    needs, sources, recos = analyse(db, today)
    names = display_names(db)
    return {
        "names": names,
        "needs": [{"account_id": n.account_id, "bank": n.bank, "name": n.name, "amount": n.amount, "reasons": n.reasons} for n in needs],
        "sources": [{"account_id": s.account_id, "bank": s.bank, "name": s.name, "balance": s.balance, "reserved": s.reserved,
                     "available": s.available, "pro": s.pro} for s in sources],
        "recommendations": [{"amount": r.amount, "from_bank": r.from_account.bank, "from_name": r.from_account.name,
                             "from_label": names.get(r.from_account.account_id, BANKS.get(r.from_account.bank, r.from_account.bank)),
                             "to_label": names.get(r.to.account_id, BANKS.get(r.to.bank, r.to.bank)),
                             "from_available": r.from_account.available, "to_bank": r.to.bank, "to_name": r.to.name,
                             "reasons": r.to.reasons, "note": r.note, "possible": bool(r.from_account.account_id)} for r in recos],
    }
