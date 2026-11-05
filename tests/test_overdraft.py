from datetime import date

from bankwatch.db import Database, Tx
from bankwatch.overdraft import balance_history, current_overdrafts, detect_overdraft

CFG = {"grace_hours": 24, "remind_every_days": 3, "exclude_banks": ["amex"], "banks": {"sg": {"note": "agios"}}}


def tx(i, acc, d, amount):
    return Tx(id=f"{acc}:{i}", account_id=acc, booking_date=d, amount=amount, counterparty="X", label="X", status="booked")


def setup(tmp_path):
    db = Database(tmp_path / "o.db")
    for acc, bank in (("s1", "sg"), ("a1", "amex"), ("b1", "boursobank")):
        db.upsert_account(id=acc, bank=bank, institution_id="X", requisition_id=None, iban=None, name=acc, currency="EUR")
    # SG : +300 le 20/10, -500 le 28/10 (passe négatif), -25.81 le 2/11 -> solde -125.81 aujourd'hui (4/11)
    db.upsert_transactions([tx(1, "s1", date(2026, 10, 20), 300), tx(2, "s1", date(2026, 10, 28), -500), tx(3, "s1", date(2026, 11, 2), -25.81)])
    db.set_balance("s1", -125.81, None)
    db.set_balance("a1", -149.68, None)   # carte : négatif normal, ignoré
    db.set_balance("b1", 121.54, None)
    return db


def test_history_reconstruction(tmp_path):
    db = setup(tmp_path)
    h = dict(balance_history(db, "s1", -125.81, date(2026, 11, 4)))
    assert h[date(2026, 11, 4)] == -125.81 and h[date(2026, 11, 1)] == -100.0
    assert h[date(2026, 10, 28)] == -100.0 and h[date(2026, 10, 27)] == 400.0


def test_overdraft_since_and_exclusions(tmp_path):
    db = setup(tmp_path)
    ods = current_overdrafts(db, date(2026, 11, 4), CFG)
    assert [o.bank for o in ods] == ["sg"]  # amex exclue, bourso positif
    o = ods[0]
    assert o.since == date(2026, 10, 28) and o.days == 7 and o.amount == 125.81 and o.lowest == -125.81 and o.note == "agios"


def test_alert_escalation(tmp_path):
    db = setup(tmp_path)
    # le jour même du passage en négatif : alerte « début », frais pas encore
    a0 = detect_overdraft(db, date(2026, 10, 28), CFG)
    assert [a.kind for a in a0] == ["overdraft_start"] and "au-delà de 24 h" in a0[0].message
    # 7 jours plus tard : « qui dure », rappel tous les 3 j (empreinte par palier)
    a7 = detect_overdraft(db, date(2026, 11, 4), CFG)
    assert a7[0].kind == "overdraft_ongoing" and a7[0].fingerprint.endswith(":2") and "7 j" in a7[0].title
    a8 = detect_overdraft(db, date(2026, 11, 5), CFG)
    assert a8[0].fingerprint == a7[0].fingerprint  # même palier : pas de nouvelle alerte
    a9 = detect_overdraft(db, date(2026, 11, 6), CFG)
    assert a9[0].fingerprint != a7[0].fingerprint  # palier suivant : rappel
    # retour en positif : plus rien
    db.set_balance("s1", 50, None)
    assert detect_overdraft(db, date(2026, 11, 7), CFG) == []


def test_fee_estimate_and_observed(tmp_path):
    from bankwatch.overdraft import estimate_fees, is_fee

    db = setup(tmp_path)
    cfg = {**CFG, "default_tariff": {"rate": 0.16, "op_fee": 8.0, "op_fee_cap_month": 80.0, "rejection_fee": 20.0}}
    # un frais d'impayé facturé pendant l'épisode + 2 débits passés à découvert
    db.upsert_transactions([Tx(id="s1:f", account_id="s1", booking_date=date(2026, 11, 3), amount=-20,
                               counterparty="FRAIS IMPAYE DE 172,89 EUR DU", label="FRAIS PRELEVEMENT IMPAYE", status="booked")])
    assert is_fee("FRAIS IMPAYE DE 172,89 EUR DU", "") and not is_fee("CARREFOUR", "CB")
    od = current_overdrafts(db, date(2026, 11, 4), cfg)[0]
    f = estimate_fees(db, od, date(2026, 11, 4), cfg)
    assert f["rate_source"].startswith("hypothèse") and f["rate"] == 0.16
    assert f["observed_total"] == 20.0 and f["observed"][0]["amount"] == 20.0
    assert f["ops_in_overdraft"] == 2 and f["commissions"] == 16.0  # le -500 qui fait basculer + le -25.81 ; le frais lui-même n'est pas compté
    assert 0 < f["agios"] < 5 and f["estimated_total"] == round(f["agios"] + f["commissions"], 2)


def test_rate_calibrated_from_past_agios(tmp_path):
    from bankwatch.overdraft import calibrate_rate

    db = setup(tmp_path)
    # découvert de ~100 € pendant ~30 j avant des agios de 1,32 € -> taux ≈ 16 %
    db.upsert_transactions([Tx(id="s1:ag", account_id="s1", booking_date=date(2026, 11, 3), amount=-1.0,
                               counterparty="INTERETS DEBITEURS", label="AGIOS 3E TRIMESTRE", status="booked")])
    r = calibrate_rate(db, "s1", -125.81, date(2026, 11, 4))
    assert r is not None and 0.3 < r < 0.8  # ~1 € d'agios sur ~6 j × 100 € : taux élevé mais cohérent avec les données


def test_effective_balance_adds_ops_seen_since_read(tmp_path):
    db = Database(tmp_path / "e.db")
    db.upsert_account(id="s1", bank="sg", institution_id="X", requisition_id=None, iban=None, name="c", currency="EUR")
    db.set_balance("s1", -166.80, "2026-11-04", read=True)
    db.set_meta("balread:s1", "2026-11-05T06:20:00+00:00")  # lecture du matin ; les virements arrivent plus tard
    db.upsert_transactions([tx(1, "s1", date(2026, 11, 5), 170), Tx(id="s1:p", account_id="s1", booking_date=date(2026, 11, 5), amount=50,
                                                                    counterparty="VIR", label="VIR", status="pending")])
    e = db.effective_balance("s1")
    assert e["balance"] == -166.80 and e["estimate"] == 53.2 and e["delta"] == 220 and e["since_count"] == 2
    # le découvert disparaît avec l'estimation, et la prévision utilise le solde estimé
    assert current_overdrafts(db, date(2026, 11, 5), CFG) == []
    from bankwatch.forecast import ForecastConfig, coverage

    assert {c.bank: c.balance for c in coverage(db, date(2026, 11, 5), ForecastConfig())}["sg"] == 53.2
    # une nouvelle lecture remet le compteur à zéro
    db.set_balance("s1", 53.2, "2026-11-05", read=True)
    assert db.effective_balance("s1")["delta"] == 0
