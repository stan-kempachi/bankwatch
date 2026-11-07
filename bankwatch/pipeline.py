"""Orchestration : sync -> règles -> alertes Discord ; rapports périodiques."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta
from typing import Any

from .config import settings
from .db import Database, Tx
from .discord import DiscordNotifier
from .rules import Alert, run_all

CANDIDATE_WINDOW_DAYS = 10  # on ne juge que les transactions récentes, pas tout l'historique importé


def store_alerts(db: Database, alerts: list[Alert]) -> list[Alert]:
    new: list[Alert] = []
    for a in alerts:
        if db.add_alert(a.fingerprint, a.kind, a.severity, a.title, a.message, a.amount, a.tx_ids):
            new.append(a)
    return new


def run_checks(db: Database, new_txs: list[Tx] | None = None, today: date | None = None,
               notify: bool = True) -> list[Alert]:
    """Évalue les règles sur les transactions récentes et notifie les alertes nouvelles."""
    today = today or date.today()
    from .sync import mark_internal_pairs, mark_wallet_pairs

    mark_internal_pairs(db, since=today - timedelta(days=45))
    mark_wallet_pairs(db, since=today - timedelta(days=45))
    history = db.transactions(since=today - timedelta(days=400))
    cutoff = today - timedelta(days=CANDIDATE_WINDOW_DAYS)
    if new_txs is None:
        candidates = [t for t in history if t.booking_date >= cutoff]
    else:
        candidates = [t for t in new_txs if t.booking_date >= cutoff]
    from .forecast import detect_forecast
    from .watch import claimed_ids, detect_watch

    alerts = run_all(history, candidates, today=today, skip_ids=claimed_ids(candidates))
    from .overdraft import detect_overdraft

    alerts += detect_watch(history, candidates, today)
    from .recommend import detect_recommendations

    alerts += detect_forecast(db, today)
    alerts += detect_overdraft(db, today)
    alerts += detect_recommendations(db, today)
    new_alerts = store_alerts(db, alerts)

    baseline = db.get_meta("baseline_done") is None
    if baseline:
        # Première exécution : on enregistre tout mais on n'arrose pas Discord avec l'historique.
        db.mark_sent(a["id"] for a in db.unsent_alerts())
        db.set_meta("baseline_done", today.isoformat())
        return new_alerts

    if notify:
        pending = db.unsent_alerts()
        if pending:
            DiscordNotifier().send_alerts([dict(r) for r in pending])
            db.mark_sent(r["id"] for r in pending)
    return new_alerts


def build_report(db: Database, days: int = 7, today: date | None = None) -> dict[str, Any]:
    today = today or date.today()
    start = today - timedelta(days=days - 1)
    prev_start, prev_end = start - timedelta(days=days), start - timedelta(days=1)
    txs = db.transactions(since=prev_start, until=today)
    cur = [t for t in txs if t.booking_date >= start]
    prev = [t for t in txs if t.booking_date <= prev_end]

    def spend(ts: list[Tx]) -> float:
        return sum(abs(t.amount) for t in ts if t.is_debit and t.category != "epargne_virement")

    by_cat: dict[str, float] = defaultdict(float)
    by_merchant: dict[str, float] = defaultdict(float)
    for t in cur:
        if t.is_debit and t.category != "epargne_virement":
            by_cat[t.category or "autre"] += abs(t.amount)
            by_merchant[t.counterparty or t.label or "?"] += abs(t.amount)
    total, prev_total = spend(cur), spend(prev)
    delta = None if prev_total == 0 else (total / prev_total - 1) * 100
    label = "Rapport hebdo" if days == 7 else f"Rapport {days} jours"
    return {
        "title": f"{label} · {start:%d/%m} → {today:%d/%m}",
        "accounts": [dict(a) for a in db.accounts(include_ignored=False)],
        "total_spend": total,
        "total_income": sum(t.amount for t in cur if not t.is_debit and t.category != "epargne_virement"),
        "delta_vs_prev_pct": delta,
        "by_category": dict(by_cat),
        "top_merchants": sorted(by_merchant.items(), key=lambda kv: -kv[1]),
        "open_alerts": sum(1 for a in db.alerts(500) if a["created_at"][:10] >= start.isoformat()),
        "watch": [_watch_dict(s) for s in _watch_stats(db, today)],
        "forecast": __import__("bankwatch.forecast", fromlist=["coverage_dicts"]).coverage_dicts(db, today),
    }


def _watch_stats(db: Database, today: date):
    from .watch import all_stats

    return all_stats(db.transactions(since=today - timedelta(days=400)), today)


def _watch_dict(s) -> dict[str, Any]:
    return {"name": s.name, "label": s.label, "note": s.note, "since": s.since.isoformat() if s.since else None,
            "month_total": s.month_total, "month_small": s.month_small, "month_small_count": s.month_small_count,
            "prev_avg_same_day": s.prev_avg_same_day, "months": s.months, "since_small_count": s.since_small_count,
            "since_small_total": s.since_small_total, "monthly_cap": s.monthly_cap}


def send_report(db: Database, days: int = 7) -> dict[str, Any]:
    report = build_report(db, days)
    DiscordNotifier().send_report(report)
    return report


def should_send_weekly(today: date | None = None) -> bool:
    return (today or date.today()).weekday() == settings.weekly_report_weekday
