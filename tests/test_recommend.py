import json
from datetime import date

import pytest

from bankwatch import forecast as fc
from bankwatch.db import Database, Tx
from bankwatch.recommend import analyse, detect_recommendations


@pytest.fixture
def cfg_file(tmp_path, monkeypatch):
    p = tmp_path / "forecast.json"
    p.write_text(json.dumps({
        "horizon_days": 14, "margin": 50, "transfer_round": 5,
        "card_settlements": [{"label": "Règlement American Express", "card_bank": "amex", "debit_bank": "boursobank",
                              "match": ["american express"], "due_day": 30}],
        "accounts": {"indy": {"pro": True, "note": "compte pro"}, "boursobank": {}, "sg": {}},
    }))
    monkeypatch.setattr(fc, "FORECAST_FILE", p)
    return p


def setup(tmp_path, sg=-125.81, bourso=121.54, indy=1234.54, amex=-149.68):
    db = Database(tmp_path / "r.db")
    for acc, bank in (("s1", "sg"), ("b1", "boursobank"), ("i1", "indy"), ("a1", "amex")):
        db.upsert_account(id=acc, bank=bank, institution_id="X", requisition_id=None, iban=None, name=bank, currency="EUR")
    for acc, bal in (("s1", sg), ("b1", bourso), ("i1", indy), ("a1", amex)):
        db.set_balance(acc, bal, None)
    db.upsert_transactions([Tx(id="s1:1", account_id="s1", booking_date=date(2026, 10, 28), amount=-500, counterparty="X", label="X"),
                            Tx(id="b1:1", account_id="b1", booking_date=date(2026, 10, 30), amount=-474.46, counterparty="American Express -France", label="PRLV", category="epargne_virement")])
    return db


def test_recommend_from_pro_when_personal_is_short(cfg_file, tmp_path):
    db = setup(tmp_path)
    needs, sources, recos = analyse(db, date(2026, 11, 4))
    assert [n.bank for n in needs] == ["sg"] and needs[0].amount == 180  # 125,81 + 50 de marge, arrondi à 5
    by_bank = {s.bank: s for s in sources}
    assert by_bank["boursobank"].available == 0  # 121,54 − (149,68 Amex à prévoir + 50) < 0
    assert by_bank["indy"].available == 1234.54 - 50
    assert len(recos) == 1 and recos[0].from_account.bank == "indy" and recos[0].amount == 180 and "pro" in recos[0].note
    a = detect_recommendations(db, date(2026, 11, 4))
    assert len(a) == 1 and a[0].kind == "recommendation" and "180,00 €" in a[0].title and "Indy" in a[0].title and "Société Générale" in a[0].title


def test_personal_source_preferred(cfg_file, tmp_path):
    db = setup(tmp_path, bourso=1000)
    _, _, recos = analyse(db, date(2026, 11, 4))
    assert recos[0].from_account.bank == "boursobank"  # perso avant pro


def test_split_across_sources_and_impossible(cfg_file, tmp_path):
    db = setup(tmp_path, sg=-2000, bourso=600, indy=300)
    _, _, recos = analyse(db, date(2026, 11, 4))
    assert [(r.from_account.bank, r.amount) for r in recos if r.from_account.account_id] == [("boursobank", 400), ("indy", 250)]
    assert recos[-1].note.startswith("aucun compte") and recos[-1].amount == 2050 - 650


def test_no_need_no_reco(cfg_file, tmp_path):
    db = setup(tmp_path, sg=200)
    assert analyse(db, date(2026, 11, 4))[2] == []


def test_same_week_same_fingerprint(cfg_file, tmp_path):
    db = setup(tmp_path)
    a1 = detect_recommendations(db, date(2026, 11, 4))[0].fingerprint
    a2 = detect_recommendations(db, date(2026, 11, 6))[0].fingerprint
    a3 = detect_recommendations(db, date(2026, 11, 12))[0].fingerprint
    assert a1 == a2 != a3


def test_two_accounts_same_bank_are_named(cfg_file, tmp_path):
    db = setup(tmp_path)
    db.upsert_account(id="s2", bank="sg", institution_id="X", requisition_id=None, iban="FR7600000000000000000042", name="sg2", currency="EUR")
    db.set_balance("s2", 500, None)
    a = detect_recommendations(db, date(2026, 11, 4))
    assert any("Société Générale · …0042" in x.title and "Société Générale · sg" in x.title for x in a)
