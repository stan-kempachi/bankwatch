"""Suivi de marchands particuliers (rules/watch.json).

Exemple :
{
  "mon_saas": {
    "label": "Mon SaaS",
    "match": ["nom-du-marchand"],    # sous-chaînes cherchées dans marchand + libellé (insensible à la casse)
    "since": "2026-01-01",           # date de référence (changement de forfait) : les recharges sont signalées à partir de là
    "small_charge_max": 30,          # débit <= ce montant = « recharge »…
    "subscription_match": ["pro sub"],     # …sauf si le libellé est celui de l'abonnement
    "monthly_cap": 200,              # alerte si le total du mois dépasse ce plafond (avertissement à 80 %)
    "note": "Nouveau forfait : les recharges ne doivent plus apparaître"
  }
}
"""

from __future__ import annotations

import json
import statistics
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .db import Tx
from .rules import HIGH, LOW, MEDIUM, Alert, _month_start, _prev_months, eur

WATCH_FILE = Path("rules/watch.json")


@dataclass
class Watch:
    name: str
    label: str
    match: list[str]
    since: date | None = None
    small_charge_max: float = 0.0
    monthly_cap: float | None = None
    note: str = ""
    subscription_match: list[str] = field(default_factory=list)  # libellés de l'abonnement : jamais une recharge
    expected_amount: float | None = None  # montant attendu de l'abonnement (alerte si écart > 5 %)

    def matches(self, tx: Tx) -> bool:
        text = f"{tx.counterparty} {tx.label}".lower()
        return tx.is_debit and any(m in text for m in self.match)

    def is_subscription(self, tx: Tx) -> bool:
        text = f"{tx.counterparty} {tx.label}".lower()
        return any(m in text for m in self.subscription_match)

    def is_small(self, tx: Tx) -> bool:
        """Recharge : petit débit qui n'est pas l'abonnement."""
        return self.small_charge_max > 0 and abs(tx.amount) <= self.small_charge_max and not self.is_subscription(tx)


def load_watches(path: Path | None = None) -> list[Watch]:
    p = path or WATCH_FILE
    if not p.exists():
        return []
    data = json.loads(p.read_text(encoding="utf-8"))
    out = []
    for name, w in data.items():
        out.append(Watch(
            name=name, label=w.get("label", name),
            match=[m.lower() for m in w.get("match", [name])],
            since=date.fromisoformat(w["since"]) if w.get("since") else None,
            small_charge_max=float(w.get("small_charge_max", 0)),
            monthly_cap=float(w["monthly_cap"]) if w.get("monthly_cap") is not None else None,
            note=w.get("note", ""),
            subscription_match=[m.lower() for m in w.get("subscription_match", [])],
            expected_amount=float(w["expected_amount"]) if w.get("expected_amount") is not None else None,
        ))
    return out


@dataclass
class WatchStats:
    name: str
    label: str
    note: str
    since: date | None
    month_total: float
    month_small: float
    month_small_count: int
    prev_avg_same_day: float | None  # moyenne des 3 mois précédents, à la même date du mois
    months: list[dict[str, Any]] = field(default_factory=list)  # [{month, total, small, big, count}]
    since_small_count: int = 0
    since_small_total: float = 0.0
    monthly_cap: float | None = None


def stats(w: Watch, history: list[Tx], today: date | None = None, n_months: int = 6) -> WatchStats:
    today = today or date.today()
    txs = [t for t in history if w.matches(t)]
    m_start = _month_start(today)

    def agg(start: date, end: date) -> dict[str, Any]:
        sel = [t for t in txs if start <= t.booking_date <= end]
        small = sum(abs(t.amount) for t in sel if w.is_small(t))
        total = sum(abs(t.amount) for t in sel)
        return {"month": f"{start:%Y-%m}", "total": round(total, 2), "small": round(small, 2),
                "big": round(total - small, 2), "count": len(sel),
                "small_count": sum(1 for t in sel if w.is_small(t))}

    cur = agg(m_start, today)
    months = [agg(s, e) for s, e in reversed(_prev_months(today, n_months - 1))] + [cur]
    prev_same = [agg(s, min(e, s + timedelta(days=today.day - 1)))["total"] for s, e in _prev_months(today, 3)]
    prev_same = [v for v in prev_same if v > 0]
    since_small = [t for t in txs if w.since and t.booking_date >= w.since and w.is_small(t)]
    return WatchStats(
        name=w.name, label=w.label, note=w.note, since=w.since,
        month_total=cur["total"], month_small=cur["small"], month_small_count=cur["small_count"],
        prev_avg_same_day=round(statistics.fmean(prev_same), 2) if prev_same else None,
        months=months, since_small_count=len(since_small),
        since_small_total=round(sum(abs(t.amount) for t in since_small), 2), monthly_cap=w.monthly_cap,
    )


def detect_watch(history: list[Tx], candidates: list[Tx], today: date | None = None,
                 watches: list[Watch] | None = None) -> list[Alert]:
    today = today or date.today()
    watches = load_watches() if watches is None else watches
    alerts: list[Alert] = []
    for w in watches:
        # 1) recharge après la date de référence
        for c in candidates:
            if w.matches(c) and w.is_small(c) and (w.since is None or c.booking_date >= w.since):
                s = stats(w, history, today)
                alerts.append(Alert(
                    kind="watch_extra", severity=MEDIUM,
                    title=f"{w.label} : recharge détectée",
                    message=(f"**{c.counterparty or c.label}** — {eur(c.amount)} le {c.booking_date:%d/%m}\n"
                             f"{s.since_small_count} recharge(s) pour {eur(s.since_small_total)} depuis le "
                             f"{w.since:%d/%m}" if w.since else "") + (f"\n{w.note}" if w.note else ""),
                    fingerprint=f"watch:extra:{c.id}", amount=abs(c.amount), tx_ids=[c.id],
                ))
        # 2) abonnement : montant différent de l'attendu (ou du précédent)
        for c in candidates:
            if not (w.matches(c) and not w.is_small(c)):
                continue
            amt = abs(c.amount)
            if w.expected_amount:
                ref, ref_label = w.expected_amount, "attendu"
            else:
                prev = [t for t in history if w.matches(t) and not w.is_small(t) and t.booking_date < c.booking_date]
                if not prev:
                    continue
                ref, ref_label = abs(prev[-1].amount), "précédent"
            if ref and abs(amt - ref) / ref > 0.05:
                alerts.append(Alert(
                    kind="watch_subscription", severity=LOW if amt < ref else MEDIUM,
                    title=f"{w.label} : abonnement {eur(amt)} ({ref_label} {eur(ref)})",
                    message=f"**{c.counterparty or c.label}** — {eur(c.amount)} le {c.booking_date:%d/%m}"
                            + (f"\n{w.note}" if w.note else ""),
                    fingerprint=f"watch:sub:{c.id}", amount=amt, tx_ids=[c.id]))
        # 3) plafond mensuel
        if w.monthly_cap:
            s = stats(w, history, today)
            tag = f"{today:%Y-%m}"
            if s.month_total >= w.monthly_cap:
                alerts.append(Alert(
                    kind="watch_cap", severity=HIGH, title=f"{w.label} : plafond mensuel dépassé",
                    message=f"{eur(s.month_total)} en {tag} pour un plafond de {eur(w.monthly_cap)}.",
                    fingerprint=f"watch:cap:{w.name}:{tag}", amount=s.month_total))
            elif s.month_total >= w.monthly_cap * 0.8:
                alerts.append(Alert(
                    kind="watch_cap80", severity=LOW,
                    title=f"{w.label} : {s.month_total / w.monthly_cap * 100:.0f} % du plafond",
                    message=f"{eur(s.month_total)} sur {eur(w.monthly_cap)} en {tag}.",
                    fingerprint=f"watch:cap80:{w.name}:{tag}", amount=s.month_total))
    return alerts


def claimed_ids(candidates: list[Tx], watches: list[Watch] | None = None) -> set[str]:
    """Transactions d'un marchand suivi : les règles génériques « inhabituel » et « récurrent » les laissent au suivi."""
    watches = load_watches() if watches is None else watches
    return {c.id for c in candidates if any(w.matches(c) for w in watches)}


def all_stats(history: list[Tx], today: date | None = None, watches: list[Watch] | None = None) -> list[WatchStats]:
    watches = load_watches() if watches is None else watches
    return [stats(w, history, today) for w in watches]
