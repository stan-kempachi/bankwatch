"""Prévision des prélèvements à venir et couverture par le solde (rules/forecast.json).

{
  "horizon_days": 10,     # fenêtre de prévision
  "margin": 50,           # marge de sécurité à garder sur le compte
  "card_settlements": [   # cartes à débit différé réglées depuis un autre compte
    {"label": "Règlement American Express", "card_bank": "amex", "debit_bank": "boursobank",
     "match": ["american express"], "due_day": 30, "notice_days": 3}
  ]
}

Le montant attendu d'un règlement de carte = solde dû sur la carte (solde négatif).
Les autres prélèvements à venir viennent des récurrents détectés (cadence + dernier montant).
"""

from __future__ import annotations

import calendar
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .db import Database
from .rules import HIGH, LOW, MEDIUM, Alert, eur, recurring_merchants

FORECAST_FILE = Path("rules/forecast.json")
BANKS = {"boursobank": "BoursoBank", "sg": "Société Générale", "indy": "Indy", "amex": "American Express"}


@dataclass
class ForecastConfig:
    horizon_days: int = 10
    margin: float = 50.0
    card_settlements: list[dict[str, Any]] = field(default_factory=list)


def load_config(path: Path | None = None) -> ForecastConfig:
    p = path or FORECAST_FILE
    if not p.exists():
        return ForecastConfig()
    d = json.loads(p.read_text(encoding="utf-8"))
    return ForecastConfig(horizon_days=int(d.get("horizon_days", 10)), margin=float(d.get("margin", 50)),
                          card_settlements=list(d.get("card_settlements", [])))


def next_due_day(today: date, day: int) -> date:
    """Prochaine occurrence du jour `day` (borné à la fin du mois), aujourd'hui inclus."""
    def clamp(y: int, m: int) -> date:
        return date(y, m, min(day, calendar.monthrange(y, m)[1]))
    d = clamp(today.year, today.month)
    if d < today:
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        d = clamp(y, m)
    return d


@dataclass
class Upcoming:
    account_id: str
    bank: str
    due: date
    label: str
    amount: float          # montant positif attendu
    kind: str              # "card" | "recurring"
    estimate: bool = False # montant estimé (solde carte en cours, peut encore bouger)
    notice_days: int = 3
    beyond: bool = False   # au-delà de l'horizon : affiché « à prévoir », sans alerte


@dataclass
class Coverage:
    account_id: str
    bank: str
    name: str
    balance: float
    items: list[Upcoming]          # dans l'horizon : comptent pour le manque et les alertes
    margin: float
    later: list[Upcoming] = field(default_factory=list)  # règlements de carte au-delà de l'horizon : « à prévoir »

    @property
    def projected_shortfall(self) -> float:
        """Manque projeté si rien ne bouge d'ici le prochain règlement de carte hors fenêtre."""
        if not self.later:
            return 0.0
        return round(max(0.0, self.total + sum(i.amount for i in self.later) + self.margin - self.balance), 2)

    @property
    def total(self) -> float:
        return round(sum(i.amount for i in self.items), 2)

    @property
    def shortfall(self) -> float:
        """Ce qu'il manque pour couvrir les prélèvements + la marge (0 si tout est couvert ou rien à venir)."""
        if not self.items:
            return 0.0
        return round(max(0.0, self.total + self.margin - self.balance), 2)

    @property
    def ok(self) -> bool:
        return self.shortfall == 0


def upcoming(db: Database, today: date | None = None, cfg: ForecastConfig | None = None) -> list[Upcoming]:
    today = today or date.today()
    cfg = cfg or load_config()
    horizon = today + timedelta(days=cfg.horizon_days)
    accounts = {a["id"]: a for a in db.accounts(include_ignored=False)}
    out: list[Upcoming] = []

    # 1) règlements de cartes à débit différé
    for cs in cfg.card_settlements:
        debit_accs = [a for a in accounts.values() if a["bank"] == cs["debit_bank"]]
        card_accs = [a for a in db.accounts() if a["bank"] == cs["card_bank"]]
        if not debit_accs or not card_accs:
            continue
        owed = sum(max(0.0, -(db.effective_balance(a["id"])["estimate"] or 0)) for a in card_accs)
        due = next_due_day(today, int(cs.get("due_day", 1)))
        if owed <= 0:
            continue
        # plusieurs comptes chez la banque débitrice : on prend celui qui a déjà payé la carte, sinon le 1er
        target = debit_accs[0]
        for a in debit_accs:
            hit = db.conn.execute(
                "SELECT 1 FROM transactions WHERE account_id=? AND amount<0 AND lower(counterparty||' '||label) LIKE ? LIMIT 1",
                (a["id"], f"%{cs['match'][0].lower()}%")).fetchone()
            if hit:
                target = a
                break
        out.append(Upcoming(target["id"], target["bank"], due, cs.get("label", "Règlement carte"), round(owed, 2),
                            "card", estimate=True, notice_days=int(cs.get("notice_days", 3)), beyond=due > horizon))

    # 2) prélèvements récurrents détectés
    settle_terms = [m.lower() for cs in cfg.card_settlements for m in cs.get("match", [])]
    txs = db.transactions(since=today - timedelta(days=400), debits_only=True)
    for _key, items in recurring_merchants(txs).items():
        last = items[-1]
        if last.account_id not in accounts or last.category == "epargne_virement":
            continue
        if any(t in f"{last.counterparty} {last.label}".lower() for t in settle_terms):
            continue  # déjà couvert par le règlement de carte
        gaps = [(b.booking_date - a.booking_date).days for a, b in zip(items, items[1:], strict=False)]
        cadence = sorted(gaps)[len(gaps) // 2] if gaps else 30
        if 27 <= cadence <= 32:  # mensuel : même jour du mois que la dernière occurrence
            due = next_due_day(max(today, last.booking_date + timedelta(days=1)), last.booking_date.day)
        else:
            due = last.booking_date + timedelta(days=cadence)
            while due < today:
                due += timedelta(days=cadence)
        if due <= horizon:
            out.append(Upcoming(last.account_id, accounts[last.account_id]["bank"],
                                due, last.counterparty or last.label, round(abs(last.amount), 2), "recurring"))
    out.sort(key=lambda u: (u.due, -u.amount))
    return out


def coverage(db: Database, today: date | None = None, cfg: ForecastConfig | None = None) -> list[Coverage]:
    cfg = cfg or load_config()
    ups = upcoming(db, today, cfg)
    out = []
    from .sync import wallet_banks

    skip = {cs["card_bank"] for cs in cfg.card_settlements} | wallet_banks()
    for a in db.accounts(include_ignored=False):
        if a["bank"] in skip:
            continue  # la carte elle-même, ou un portefeuille (PayPal) financé par la banque, n'a pas à être « couvert »
        mine = [u for u in ups if u.account_id == a["id"]]
        eff = db.effective_balance(a["id"])["estimate"]
        out.append(Coverage(a["id"], a["bank"], a["name"] or "", float(eff if eff is not None else (a["balance"] or 0)),
                            [u for u in mine if not u.beyond], cfg.margin, later=[u for u in mine if u.beyond]))
    return out


def detect_forecast(db: Database, today: date | None = None, cfg: ForecastConfig | None = None) -> list[Alert]:
    """Alertes : manque détecté (une fois par échéance), retour à la normale, et point J-3 pour les cartes."""
    today = today or date.today()
    cfg = cfg or load_config()
    alerts: list[Alert] = []
    for c in coverage(db, today, cfg):
        if not c.items:
            continue  # les règlements hors fenêtre (c.later) n'alertent pas encore
        bank = BANKS.get(c.bank, c.bank)
        due = min(i.due for i in c.items)
        lines = "\n".join(f"• {i.label} — {'≈ ' if i.estimate else ''}{eur(i.amount)} le {i.due:%d/%m}" for i in c.items)
        if not c.ok:
            alerts.append(Alert(
                kind="forecast_short", severity=HIGH,
                title=f"{bank} : il manque {eur(c.shortfall)} avant le {due:%d/%m}",
                message=f"Solde {eur(c.balance)} pour {eur(c.total)} de prélèvements à venir (+ {eur(c.margin)} de marge).\n{lines}\n"
                        f"→ Virer au moins **{eur(c.shortfall)}** sur {bank}.",
                fingerprint=f"forecast:short:{c.account_id}:{due:%Y-%m}", amount=c.shortfall))
        else:
            was_short = db.conn.execute("SELECT 1 FROM alerts WHERE fingerprint=?",
                                        (f"forecast:short:{c.account_id}:{due:%Y-%m}",)).fetchone()
            if was_short:
                alerts.append(Alert(
                    kind="forecast_ok", severity=LOW, title=f"{bank} : prélèvements couverts ✓",
                    message=f"Solde {eur(c.balance)} pour {eur(c.total)} à venir.\n{lines}",
                    fingerprint=f"forecast:ok:{c.account_id}:{due:%Y-%m}", amount=c.total))
        # point d'étape J-n pour les règlements de carte
        for i in c.items:
            if i.kind == "card" and today >= i.due - timedelta(days=i.notice_days):
                status = "✓ OK" if c.ok else f"⚠️ il manque {eur(c.shortfall)}"
                alerts.append(Alert(
                    kind="forecast_card", severity=LOW if c.ok else MEDIUM,
                    title=f"{i.label} ≈ {eur(i.amount)} vers le {i.due:%d/%m} — {status}",
                    message=f"{bank} : solde {eur(c.balance)}, marge {eur(c.margin)}."
                            + ("" if c.ok else f"\n→ Virer **{eur(c.shortfall)}** sur {bank} avant le {i.due:%d/%m}.")
                            + "\n(montant = solde de la carte à ce jour, il peut encore évoluer)",
                    fingerprint=f"forecast:card:{c.account_id}:{i.due:%Y-%m-%d}", amount=i.amount))
    return alerts


def coverage_dicts(db: Database, today: date | None = None) -> list[dict[str, Any]]:
    return [{
        "account_id": c.account_id, "bank": c.bank, "name": c.name, "balance": c.balance,
        "total": c.total, "margin": c.margin, "shortfall": c.shortfall, "ok": c.ok,
        "projected_shortfall": c.projected_shortfall,
        "items": [{"due": i.due.isoformat(), "label": i.label, "amount": i.amount, "kind": i.kind, "estimate": i.estimate}
                  for i in c.items],
        "later": [{"due": i.due.isoformat(), "label": i.label, "amount": i.amount, "kind": i.kind, "estimate": i.estimate}
                  for i in c.later],
    } for c in coverage(db, today)]
