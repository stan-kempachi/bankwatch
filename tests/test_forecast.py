from datetime import date

from bankwatch.db import Database, Tx
from bankwatch.forecast import ForecastConfig, coverage, detect_forecast, next_due_day, upcoming
from bankwatch.pipeline import store_alerts

CFG = ForecastConfig(horizon_days=10, margin=50, card_settlements=[
    {"label": "Règlement American Express", "card_bank": "amex", "debit_bank": "boursobank",
     "match": ["american express"], "due_day": 30, "notice_days": 3}])


def setup(tmp_path, bourso_balance=121.54, amex_balance=-149.68):
    db = Database(tmp_path / "f.db")
    for acc, bank, name in (("b1", "boursobank", "Courant"), ("a1", "amex", "Carte"), ("s1", "sg", "SG")):
        db.upsert_account(id=acc, bank=bank, institution_id="X", requisition_id=None, iban=None, name=name, currency="EUR")
    db.set_balance("b1", bourso_balance, None)
    db.set_balance("a1", amex_balance, None)
    db.set_balance("s1", 2000, None)
    # règlement Amex déjà vu une fois sur bourso (sert à choisir le compte débité)
    db.upsert_transactions([Tx(id="t1", account_id="b1", booking_date=date(2026, 10, 30), amount=-474.46,
                               counterparty="American Express -France", label="PRLV", category="epargne_virement")])
    # loyer récurrent sur SG le 2 de chaque mois
    db.upsert_transactions([Tx(id=f"l{m}", account_id="s1", booking_date=date(2026, m, 2), amount=-1150,
                               counterparty="Loyer SCI", label="VIR LOYER", category="logement") for m in (8, 9, 10, 11)])
    return db


def test_next_due_day():
    assert next_due_day(date(2026, 11, 3), 30) == date(2026, 11, 30)
    assert next_due_day(date(2026, 11, 30), 30) == date(2026, 11, 30)
    assert next_due_day(date(2026, 12, 1), 30) == date(2026, 12, 30)
    assert next_due_day(date(2026, 2, 10), 30) == date(2026, 2, 28)


def test_upcoming_card_and_recurring(tmp_path):
    db = setup(tmp_path)
    ups = upcoming(db, date(2026, 11, 25), CFG)
    card = [u for u in ups if u.kind == "card"]
    assert len(card) == 1 and card[0].account_id == "b1" and card[0].amount == 149.68 and card[0].due == date(2026, 11, 30)
    rent = [u for u in ups if u.kind == "recurring"]
    assert rent and rent[0].account_id == "s1" and rent[0].due == date(2026, 12, 2) and rent[0].amount == 1150


def test_outside_horizon_only_card_flagged_beyond(tmp_path):
    db = setup(tmp_path)
    ups = upcoming(db, date(2026, 11, 3), CFG)  # 30/11 est à 27 jours
    assert [u.kind for u in ups] == ["card"] and ups[0].beyond  # le loyer (2/12) n'est pas listé, la carte oui


def test_shortfall_then_covered(tmp_path):
    db = setup(tmp_path)
    today = date(2026, 11, 22)  # J-8 : dans l'horizon, pas encore J-3
    cov = {c.bank: c for c in coverage(db, today, CFG)}
    assert "amex" not in cov  # la carte n'est pas un compte à couvrir
    b = cov["boursobank"]
    assert b.total == 149.68 and b.shortfall == round(149.68 + 50 - 121.54, 2) and not b.ok
    alerts = detect_forecast(db, today, CFG)
    kinds = [a.kind for a in alerts]
    assert kinds == ["forecast_short"] and "78,14 €" in alerts[0].title
    store_alerts(db, alerts)
    # relance le même jour : rien de nouveau (même empreinte)
    assert all(not db.add_alert(a.fingerprint, a.kind, a.severity, a.title, a.message) for a in detect_forecast(db, today, CFG))
    # J-3 toujours à découvert : point d'étape « il manque »
    a3 = detect_forecast(db, date(2026, 11, 27), CFG)
    assert any(a.kind == "forecast_card" and "il manque" in a.title for a in a3)
    # virement fait : couvert -> alerte « OK » une fois
    db.set_balance("b1", 400, None)
    ok = detect_forecast(db, date(2026, 11, 28), CFG)
    assert [a.kind for a in ok] == ["forecast_ok", "forecast_card"]
    assert "✓ OK" in ok[1].title


def test_sg_rent_short(tmp_path):
    db = setup(tmp_path)
    db.set_balance("s1", 500, None)
    db.set_balance("b1", 1000, None)
    alerts = [a for a in detect_forecast(db, date(2026, 11, 28), CFG) if a.kind == "forecast_short"]
    assert len(alerts) == 1 and "Société Générale" in alerts[0].title and "700,00 €" in alerts[0].title


def test_repo_config_loads():
    from bankwatch.forecast import load_config

    cfg = load_config()
    assert cfg.card_settlements and cfg.card_settlements[0]["card_bank"] == "amex"


def test_no_items_no_shortfall(tmp_path):
    db = setup(tmp_path)
    db.set_balance("s1", -125.81, None)
    cov = {c.bank: c for c in coverage(db, date(2026, 11, 3), CFG)}
    assert cov["sg"].items == [] and cov["sg"].shortfall == 0 and cov["sg"].ok


def test_card_beyond_horizon_shown_as_later_without_alert(tmp_path):
    db = setup(tmp_path)
    today = date(2026, 11, 3)  # Amex le 30/11 : hors fenêtre de 10 j
    cov = {c.bank: c for c in coverage(db, today, CFG)}
    b = cov["boursobank"]
    assert b.items == [] and len(b.later) == 1 and b.later[0].beyond and b.later[0].amount == 149.68
    assert b.ok and b.shortfall == 0
    assert b.projected_shortfall == round(149.68 + 50 - 121.54, 2)
    assert detect_forecast(db, today, CFG) == []  # pas d'alerte avant la fenêtre


def test_two_lines_same_day_do_not_loop_forever(tmp_path):
    """Deux prélèvements du même marchand le même jour chaque mois (ex. deux forfaits Bouygues) :
    écarts 0/30/0/30 → la cadence médiane valait 0 et la boucle de report ne finissait jamais."""
    db = setup(tmp_path)
    rows = []
    for i, m in enumerate((6, 7, 8, 9, 10)):
        for j, amt in enumerate((-20.0, -23.99)):
            rows.append(Tx(id=f"bt{i}{j}", account_id="s1", booking_date=date(2026, m, 19), amount=amt,
                           counterparty="Bouygues Telecom", label="PRLV BOUYGUES", category="abonnements"))
    db.upsert_transactions(rows)
    ups = upcoming(db, date(2026, 11, 8), ForecastConfig(horizon_days=14, margin=50, card_settlements=[]))
    bt = [u for u in ups if "Bouygues" in u.label]
    assert len(bt) == 1 and bt[0].due == date(2026, 11, 19) and bt[0].amount == 43.99
