"""Règles de détection.

Toutes les règles travaillent sur des objets Tx (montant négatif = débit) et renvoient
des Alert. Chaque alerte porte un `fingerprint` stable : la base ne garde qu'une
occurrence, donc relancer les contrôles ne re-notifie jamais la même chose.
"""

from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta

from .config import Settings, settings as default_settings
from .db import Tx

HIGH, MEDIUM, LOW = "high", "medium", "low"


@dataclass
class Alert:
    kind: str
    severity: str
    title: str
    message: str
    fingerprint: str
    amount: float | None = None
    tx_ids: list[str] = field(default_factory=list)


def eur(x: float) -> str:
    return f"{abs(x):,.2f} €".replace(",", " ").replace(".", ",")


def _desc(tx: Tx) -> str:
    who = tx.counterparty or tx.label or "?"
    return f"**{who}** — {eur(tx.amount)} le {tx.booking_date:%d/%m}"


# --------------------------------------------------------------------------- doublons
def detect_duplicates(history: list[Tx], candidates: list[Tx], s: Settings = default_settings) -> list[Alert]:
    """Même marchand + même montant + dates à ≤ N jours -> doublon probable."""
    alerts: list[Alert] = []
    debits = [t for t in history if t.is_debit and abs(t.amount) >= s.dup_min_amount]
    by_key: dict[tuple[str, float], list[Tx]] = defaultdict(list)
    for t in debits:
        by_key[(t.merchant_key, round(abs(t.amount), 2))].append(t)
    seen: set[frozenset[str]] = set()
    for c in candidates:
        if not c.is_debit or abs(c.amount) < s.dup_min_amount:
            continue
        for other in by_key.get((c.merchant_key, round(abs(c.amount), 2)), []):
            if other.id == c.id:
                continue
            if abs((other.booking_date - c.booking_date).days) > s.dup_window_days:
                continue
            pair = frozenset({c.id, other.id})
            if pair in seen:
                continue
            seen.add(pair)
            same_acc = other.account_id == c.account_id
            alerts.append(
                Alert(
                    kind="duplicate",
                    severity=HIGH if same_acc else MEDIUM,
                    title="Paiement en doublon probable",
                    message=(
                        f"{_desc(c)}\n{_desc(other)}\n"
                        + ("Même compte, même montant, même marchand." if same_acc
                           else "Même montant et même marchand sur deux comptes différents.")
                    ),
                    fingerprint="dup:" + ":".join(sorted(pair)),
                    amount=abs(c.amount),
                    tx_ids=sorted(pair),
                )
            )
    return alerts


# ---------------------------------------------------------------- prélèvements récurrents
def recurring_merchants(history: list[Tx], min_occurrences: int = 3) -> dict[str, list[Tx]]:
    """Marchands débités au moins `min_occurrences` fois, à cadence ~mensuelle (ou abonnement/logement)."""
    groups: dict[str, list[Tx]] = defaultdict(list)
    for t in history:
        if t.is_debit and t.merchant_key:
            groups[t.merchant_key].append(t)
    recurring: dict[str, list[Tx]] = {}
    for key, txs in groups.items():
        txs.sort(key=lambda t: t.booking_date)
        if len(txs) < min_occurrences:
            continue
        gaps = [(b.booking_date - a.booking_date).days for a, b in zip(txs, txs[1:])]
        gaps = [g for g in gaps if g > 0]
        monthly = bool(gaps) and 24 <= statistics.median(gaps) <= 37
        subscription_like = all(t.category in ("abonnements", "logement") for t in txs)
        months = {(t.booking_date.year, t.booking_date.month) for t in txs}
        amounts = [abs(t.amount) for t in txs]
        mean = statistics.fmean(amounts)
        stable = mean > 0 and (statistics.pstdev(amounts) / mean) <= 0.25  # prélèvement = montant stable
        if (monthly or subscription_like) and stable and len(months) >= 2:
            recurring[key] = txs
    return recurring


def detect_recurring_anomalies(history: list[Tx], candidates: list[Tx], s: Settings = default_settings) -> list[Alert]:
    alerts: list[Alert] = []
    rec = recurring_merchants(history)
    for c in candidates:
        if not c.is_debit or c.merchant_key not in rec:
            continue
        prev = [t for t in rec[c.merchant_key] if t.id != c.id and t.booking_date <= c.booking_date]
        if not prev:
            continue
        # 1) montant qui change
        ref = statistics.median(abs(t.amount) for t in prev[-6:])
        if ref > 0:
            delta_pct = (abs(c.amount) - ref) / ref * 100
            if abs(delta_pct) > s.recurring_tolerance_pct:
                sign = "+" if delta_pct > 0 else ""
                alerts.append(
                    Alert(
                        kind="recurring_amount",
                        severity=MEDIUM if delta_pct > 0 else LOW,
                        title="Prélèvement récurrent : montant modifié",
                        message=f"{_desc(c)}\nHabituellement {eur(ref)} → {sign}{delta_pct:.0f} %.",
                        fingerprint=f"recamt:{c.id}",
                        amount=abs(c.amount),
                        tx_ids=[c.id],
                    )
                )
        # 2) deux fois dans la même fenêtre mensuelle : écart nettement plus court que la cadence
        #    habituelle, ou deux passages dans le même mois calendaire
        gaps = [(b.booking_date - a.booking_date).days for a, b in zip(prev, prev[1:])]
        cadence = statistics.median(gaps) if gaps else 30
        last = prev[-1]
        gap = (c.booking_date - last.booking_date).days
        same_month = (last.booking_date.year, last.booking_date.month) == (c.booking_date.year, c.booking_date.month)
        if 0 <= gap < max(cadence - 7, 7) or (same_month and cadence >= 24):
            if abs(last.amount) == abs(c.amount) and (c.booking_date - last.booking_date).days <= s.dup_window_days:
                continue  # déjà géré par la règle doublon
            alerts.append(
                Alert(
                    kind="recurring_double",
                    severity=MEDIUM,
                    title="Prélèvement récurrent passé deux fois ce mois",
                    message=f"{_desc(c)}\nPrécédent : {_desc(last)}",
                    fingerprint=f"recdbl:{c.id}:{last.id}",
                    amount=abs(c.amount),
                    tx_ids=[c.id, last.id],
                )
            )
    return alerts


# ------------------------------------------------------------- transactions inhabituelles
def detect_unusual(history: list[Tx], candidates: list[Tx], s: Settings = default_settings) -> list[Alert]:
    alerts: list[Alert] = []
    by_merchant: dict[str, list[float]] = defaultdict(list)
    first_seen: dict[str, date] = {}
    for t in history:
        if t.is_debit and t.merchant_key:
            by_merchant[t.merchant_key].append(abs(t.amount))
            first_seen[t.merchant_key] = min(first_seen.get(t.merchant_key, t.booking_date), t.booking_date)
    cand_ids = {c.id for c in candidates}
    for c in candidates:
        if not c.is_debit or c.category == "epargne_virement":  # tes propres virements ne sont pas « inhabituels »
            continue
        amt = abs(c.amount)
        key = c.merchant_key
        others = [a for t in history if t.merchant_key == key and t.id != c.id and t.id not in cand_ids
                  for a in [abs(t.amount)]] if key else []
        if key and len(others) >= s.unusual_min_history:
            mean = statistics.fmean(others)
            std = statistics.pstdev(others) or max(mean * 0.1, 1.0)
            z = (amt - mean) / std
            if z >= s.unusual_zscore and amt >= mean * 2:
                alerts.append(
                    Alert(
                        kind="unusual_amount",
                        severity=MEDIUM,
                        title="Montant inhabituel chez un marchand connu",
                        message=f"{_desc(c)}\nMoyenne habituelle {eur(mean)} sur {len(others)} opérations (z = {z:.1f}).",
                        fingerprint=f"unusual:{c.id}",
                        amount=amt,
                        tx_ids=[c.id],
                    )
                )
                continue
        elif key and not others and amt >= s.new_merchant_amount:
            alerts.append(
                Alert(
                    kind="new_merchant",
                    severity=MEDIUM,
                    title="Nouveau marchand, montant élevé",
                    message=f"{_desc(c)}\nPremière opération vue chez ce marchand.",
                    fingerprint=f"newm:{c.id}",
                    amount=amt,
                    tx_ids=[c.id],
                )
            )
            continue
        if amt >= s.big_amount:
            alerts.append(
                Alert(
                    kind="big_amount",
                    severity=LOW,
                    title="Débit important",
                    message=_desc(c),
                    fingerprint=f"big:{c.id}",
                    amount=amt,
                    tx_ids=[c.id],
                )
            )
    return alerts


# ---------------------------------------------------------------------- surdépense
def _month_start(d: date) -> date:
    return d.replace(day=1)


def _prev_months(d: date, n: int) -> list[tuple[date, date]]:
    out = []
    cur = _month_start(d)
    for _ in range(n):
        end = cur - timedelta(days=1)
        start = _month_start(end)
        out.append((start, end))
        cur = start
    return out


def spend_by_category(txs: list[Tx], start: date, end: date, exclude: tuple[str, ...] = ("epargne_virement",)) -> dict[str, float]:
    out: dict[str, float] = defaultdict(float)
    for t in txs:
        if t.is_debit and start <= t.booking_date <= end and t.category not in exclude:
            out[t.category or "autre"] += abs(t.amount)
    return dict(out)


def detect_overspend(history: list[Tx], today: date | None = None, s: Settings = default_settings) -> list[Alert]:
    """Compare le mois en cours (à date) à la moyenne des 3 mois précédents sur la même période du mois."""
    today = today or date.today()
    alerts: list[Alert] = []
    m_start = _month_start(today)
    current = spend_by_category(history, m_start, today)
    month_tag = f"{today:%Y-%m}"

    # moyenne des 3 mois précédents, mêmes jours (1 -> today.day)
    prev_totals: dict[str, list[float]] = defaultdict(list)
    prev_global: list[float] = []
    for start, end in _prev_months(today, 3):
        cut = min(end, start + timedelta(days=today.day - 1))
        spend = spend_by_category(history, start, cut)
        if not spend:
            continue
        for cat, v in spend.items():
            prev_totals[cat].append(v)
        prev_global.append(sum(spend.values()))

    # budgets explicites
    for cat, budget in s.budgets.items():
        cur = current.get(cat, 0.0)
        if cur >= budget:
            alerts.append(Alert(
                kind="budget_exceeded", severity=HIGH,
                title=f"Budget « {cat} » dépassé",
                message=f"{eur(cur)} dépensés en {month_tag} pour un budget de {eur(budget)}.",
                fingerprint=f"budget:{cat}:{month_tag}", amount=cur,
            ))
        elif cur >= budget * 0.8:
            alerts.append(Alert(
                kind="budget_warning", severity=LOW,
                title=f"Budget « {cat} » à {cur / budget * 100:.0f} %",
                message=f"{eur(cur)} sur {eur(budget)} en {month_tag}.",
                fingerprint=f"budget80:{cat}:{month_tag}", amount=cur,
            ))

    # surconsommation vs historique (pas avant le 5 du mois : trop peu de données, trop de bruit)
    if today.day < 5:
        return alerts
    if prev_global:
        avg_global = statistics.fmean(prev_global)
        cur_global = sum(current.values())
        if avg_global > 0 and cur_global >= avg_global * (1 + s.overspend_pct / 100) and cur_global >= s.overspend_min_amount * 3:
            alerts.append(Alert(
                kind="overspend_total", severity=HIGH,
                title="Surdépense globale ce mois",
                message=f"{eur(cur_global)} au {today:%d/%m} contre {eur(avg_global)} en moyenne à la même date sur les 3 derniers mois (+{(cur_global / avg_global - 1) * 100:.0f} %).",
                fingerprint=f"overspend:total:{month_tag}", amount=cur_global,
            ))
    for cat, cur in current.items():
        hist = prev_totals.get(cat)
        if not hist or len(hist) < 2 or cur < s.overspend_min_amount:
            continue
        avg = statistics.fmean(hist)
        if avg > 0 and cur >= avg * (1 + s.overspend_pct / 100):
            alerts.append(Alert(
                kind="overspend_category", severity=MEDIUM,
                title=f"Surconsommation « {cat} »",
                message=f"{eur(cur)} au {today:%d/%m} contre {eur(avg)} en moyenne à la même date (+{(cur / avg - 1) * 100:.0f} %).",
                fingerprint=f"overspend:{cat}:{month_tag}", amount=cur,
            ))
    return alerts


# --------------------------------------------------------------------------- pipeline
def run_all(history: list[Tx], candidates: list[Tx], today: date | None = None, s: Settings = default_settings,
            skip_ids: set[str] | None = None) -> list[Alert]:
    alerts: list[Alert] = []
    generic = [c for c in candidates if c.id not in (skip_ids or set())]
    alerts += detect_duplicates(history, candidates, s)
    alerts += detect_recurring_anomalies(history, generic, s)
    alerts += detect_unusual(history, generic, s)
    alerts += detect_overspend(history, today, s)
    # dédoublonnage par fingerprint (sécurité)
    seen: set[str] = set()
    out: list[Alert] = []
    for a in alerts:
        if a.fingerprint not in seen:
            seen.add(a.fingerprint)
            out.append(a)
    order = {HIGH: 0, MEDIUM: 1, LOW: 2}
    out.sort(key=lambda a: (order[a.severity], -(a.amount or 0)))
    return out
