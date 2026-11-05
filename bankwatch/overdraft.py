"""Suivi des découverts (rules/overdraft.json).

{
  "grace_hours": 24,          # au-delà : frais probables (agios, commission d'intervention)
  "remind_every_days": 3,     # rappel tant que ça dure
  "banks": {"sg": {"note": "SG : commission d'intervention 8 € par opération + agios"}}
}

Pas d'historique de soldes en base : on le RECONSTITUE à partir des opérations
(solde à J = solde actuel − somme des opérations postérieures à J).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .db import Database
from .rules import HIGH, MEDIUM, Alert, eur

OVERDRAFT_FILE = Path("rules/overdraft.json")
BANKS = {"boursobank": "BoursoBank", "sg": "Société Générale", "indy": "Indy", "amex": "American Express"}


def load_config(path: Path | None = None) -> dict[str, Any]:
    p = path or OVERDRAFT_FILE
    d = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    d.setdefault("grace_hours", 24)
    d.setdefault("remind_every_days", 3)
    d.setdefault("banks", {})
    d.setdefault("exclude_banks", ["amex"])  # une carte à débit différé est négative par nature
    d.setdefault("default_tariff", {"rate": 0.16, "op_fee": 8.0, "op_fee_cap_month": 80.0, "rejection_fee": 20.0})
    return d


FEE_WORDS = ("agios", "interets debiteurs", "intérêts débiteurs", "commission d'intervention", "commission intervention",
             "frais prelevement impaye", "frais prélèvement impayé", "frais impaye", "frais de rejet", "frais rejet",
             "frais de forcage", "frais forçage", "lettre d'information", "frais pour compte debiteur")


def is_fee(counterparty: str, label: str) -> bool:
    t = f"{counterparty} {label}".lower().replace("é", "e").replace("è", "e")
    return any(w.replace("é", "e").replace("è", "e") in t for w in FEE_WORDS)


def observed_fees(db: Database, account_id: str, since: date, until: date) -> list[dict[str, Any]]:
    """Frais bancaires réellement facturés sur la période (lignes reconnues par leur libellé)."""
    out = []
    for t in db.transactions(since=since, until=until, account_id=account_id):
        if t.is_debit and t.status == "booked" and is_fee(t.counterparty, t.label):
            out.append({"date": t.booking_date.isoformat(), "amount": abs(t.amount), "label": (t.counterparty or t.label)[:60], "id": t.id})
    return out


def calibrate_rate(db: Database, account_id: str, balance: float, today: date) -> float | None:
    """Taux annuel implicite d'après les agios déjà facturés (12 mois) : agios / Σ(solde négatif × jours)."""
    hist = balance_history(db, account_id, balance, today, days=365)
    agios = [t for t in db.transactions(since=today - timedelta(days=365), account_id=account_id)
             if t.is_debit and any(w in f"{t.counterparty} {t.label}".lower() for w in ("agios", "interets debiteurs", "intérêts débiteurs"))]
    if not agios:
        return None
    neg_days = sum(-b for d, b in hist if b < 0 and d < max(a.booking_date for a in agios))
    if neg_days <= 0:
        return None
    return round(sum(abs(a.amount) for a in agios) * 365 / neg_days, 4)


def estimate_fees(db: Database, od: Overdraft, today: date, cfg: dict) -> dict[str, Any]:
    base = cfg.get("default_tariff") or {"rate": 0.16, "op_fee": 8.0, "op_fee_cap_month": 80.0, "rejection_fee": 20.0}
    tariff = {**base, **{k: v for k, v in cfg.get("banks", {}).get(od.bank, {}).items() if k in base}}
    calibrated = calibrate_rate(db, od.account_id, od.balance, today)
    rate = calibrated if calibrated is not None else float(tariff["rate"])
    neg = [(d, b) for d, b in od.history if d >= od.since and b < 0]
    neg_balance_days = sum(-b for _, b in neg)
    agios = round(neg_balance_days * rate / 365, 2)
    # commissions d'intervention : débits passés pendant le découvert (hors frais eux-mêmes), plafonnées par mois
    authorized = float(cfg.get("banks", {}).get(od.bank, {}).get("authorized_overdraft", 0) or 0)
    neg_dates = {d for d, b in neg if -b > authorized}  # commissions : seulement au-delà de l'autorisation
    ops_by_month: dict[str, int] = {}
    for t in db.transactions(since=od.since, until=today, account_id=od.account_id):
        if t.is_debit and t.status == "booked" and t.booking_date in neg_dates and not is_fee(t.counterparty, t.label):
            ops_by_month[f"{t.booking_date:%Y-%m}"] = ops_by_month.get(f"{t.booking_date:%Y-%m}", 0) + 1
    commissions = round(sum(min(n * float(tariff["op_fee"]), float(tariff["op_fee_cap_month"])) for n in ops_by_month.values()), 2)
    observed = observed_fees(db, od.account_id, od.since, today)
    per_day = round(((-od.balance) * rate / 365), 2)
    return {
        "rate": rate, "rate_source": "calibré sur tes agios" if calibrated is not None
        else cfg.get("banks", {}).get(od.bank, {}).get("rate_note") or "hypothèse (tarif découvert non autorisé)",
        "authorized_overdraft": authorized,
        "agios": agios, "commissions": commissions, "ops_in_overdraft": sum(ops_by_month.values()),
        "estimated_total": round(agios + commissions, 2), "per_day": per_day,
        "observed": observed, "observed_total": round(sum(f["amount"] for f in observed), 2),
        "tariff": tariff,
    }


@dataclass
class Overdraft:
    account_id: str
    bank: str
    name: str
    balance: float            # négatif
    since: date               # premier jour à découvert (reconstitué)
    days: int                 # jours révolus
    lowest: float             # point bas sur la période
    history: list[tuple[date, float]]  # 30 derniers jours (date, solde fin de journée)
    note: str = ""

    @property
    def amount(self) -> float:
        return round(-self.balance, 2)


def balance_history(db: Database, account_id: str, balance: float, today: date, days: int = 30) -> list[tuple[date, float]]:
    """Solde fin de journée pour chacun des `days` derniers jours, reconstitué depuis les opérations."""
    since = today - timedelta(days=days)
    txs = db.transactions(since=since + timedelta(days=1), account_id=account_id)
    by_day: dict[date, float] = {}
    for t in txs:  # en attente comprises : les soldes bancaires (et notre estimation) les intègrent
        by_day[t.booking_date] = by_day.get(t.booking_date, 0.0) + t.amount
    out: list[tuple[date, float]] = []
    bal = balance
    d = today
    while d > since:
        out.append((d, round(bal, 2)))
        bal -= by_day.get(d, 0.0)  # en remontant, on retire les opérations du jour
        d -= timedelta(days=1)
    return list(reversed(out))


def current_overdrafts(db: Database, today: date | None = None, cfg: dict | None = None) -> list[Overdraft]:
    today = today or date.today()
    cfg = cfg or load_config()
    out = []
    for a in db.accounts(include_ignored=False):
        eff = db.effective_balance(a["id"])["estimate"]
        bal = eff if eff is not None else a["balance"]
        if a["bank"] in cfg["exclude_banks"] or bal is None or bal >= 0:
            continue
        hist = balance_history(db, a["id"], float(bal), today)
        since = today
        for d, b in reversed(hist):
            if b >= 0:
                break
            since = d
        neg = [b for d, b in hist if d >= since]
        out.append(Overdraft(a["id"], a["bank"], a["name"] or "", float(bal), since, (today - since).days,
                             min(neg) if neg else float(bal), hist,
                             cfg["banks"].get(a["bank"], {}).get("note", "")))
    return out


def detect_overdraft(db: Database, today: date | None = None, cfg: dict | None = None) -> list[Alert]:
    today = today or date.today()
    cfg = cfg or load_config()
    grace_days = max(1, int(cfg["grace_hours"]) // 24)
    every = max(1, int(cfg["remind_every_days"]))
    alerts: list[Alert] = []
    for od in current_overdrafts(db, today, cfg):
        bank = BANKS.get(od.bank, od.bank)
        tail = f"\n{od.note}" if od.note else ""
        todo = f"→ Virer au moins **{eur(od.amount)}** sur {bank}."
        if od.days < grace_days:
            alerts.append(Alert(
                kind="overdraft_start", severity=HIGH,
                title=f"{bank} : à découvert de {eur(od.amount)}",
                message=f"Solde {eur(od.balance).replace(' €', '')} € depuis le {od.since:%d/%m}. "
                        f"Frais probables au-delà de {cfg['grace_hours']} h.\n{todo}{tail}",
                fingerprint=f"od:start:{od.account_id}:{od.since}", amount=od.amount))
        else:
            step = od.days // every
            fees = estimate_fees(db, od, today, cfg)
            cost = (f"Frais estimés : ~{eur(fees['estimated_total'])} (agios {eur(fees['agios'])} + commissions {eur(fees['commissions'])}), "
                    f"soit ~{eur(fees['per_day'])}/jour d'agios ; déjà facturé : {eur(fees['observed_total'])}.")
            alerts.append(Alert(
                kind="overdraft_ongoing", severity=HIGH if od.days >= grace_days else MEDIUM,
                title=f"{bank} : découvert de {eur(od.amount)} depuis {od.days} j (~{eur(fees['estimated_total'])} de frais)",
                message=f"À découvert depuis le {od.since:%d/%m} (point bas {eur(od.lowest).replace(' €', '')} €).\n{cost}\n{todo}{tail}",
                fingerprint=f"od:ongoing:{od.account_id}:{od.since}:{step}", amount=od.amount))
    return alerts


def overdraft_dicts(db: Database, today: date | None = None) -> list[dict[str, Any]]:
    cfg = load_config()
    return [{
        "account_id": o.account_id, "bank": o.bank, "name": o.name, "balance": o.balance, "amount": o.amount,
        "since": o.since.isoformat(), "days": o.days, "lowest": o.lowest, "note": o.note,
        "grace_hours": cfg["grace_hours"], "fees_likely": o.days * 24 >= cfg["grace_hours"],
        "history": [{"date": d.isoformat(), "balance": b} for d, b in o.history],
        "fees": estimate_fees(db, o, today or date.today(), cfg),
    } for o in current_overdrafts(db, today, cfg)]
