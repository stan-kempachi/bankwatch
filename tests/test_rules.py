from datetime import date, timedelta

import pytest

from bankwatch.categorize import Categorizer
from bankwatch.config import Settings
from bankwatch.db import Database, Tx, normalize_merchant, tx_from_gocardless
from bankwatch.demo import generate, load_demo
from bankwatch.rules import (
    detect_duplicates,
    detect_overspend,
    detect_recurring_anomalies,
    detect_unusual,
    recurring_merchants,
)

S = Settings(_env_file=None)
TODAY = date(2026, 11, 15)


def tx(i, d, amount, who, acc="a1", cat=""):
    return Tx(id=f"t{i}", account_id=acc, booking_date=d, amount=amount, counterparty=who, label=who, category=cat)


def monthly(who, amount, n=5, day=5, acc="a1", start_id=0, cat="abonnements"):
    out, d = [], TODAY.replace(day=day)
    for k in range(n, 0, -1):
        m = (d.month - k - 1) % 12 + 1
        y = d.year + (d.month - k - 1) // 12
        out.append(tx(start_id + k, date(y, m, day), amount, who, acc, cat))
    return out


# ---------------------------------------------------------------- normalisation
def test_normalize_merchant_strips_noise():
    assert normalize_merchant("CB CARREFOUR MARKET 12/10 IVRY") == normalize_merchant("PAIEMENT CB CARREFOUR MARKET 03/11")


def test_tx_from_gocardless_debit():
    raw = {"transactionId": "X1", "bookingDate": "2026-10-02", "transactionAmount": {"amount": "-12.50", "currency": "EUR"},
           "creditorName": "NETFLIX", "remittanceInformationUnstructured": "PRLV NETFLIX"}
    t = tx_from_gocardless("acc", raw)
    assert t.amount == -12.5 and t.counterparty == "NETFLIX" and t.id == "acc:X1"


def test_tx_without_id_gets_stable_hash():
    raw = {"bookingDate": "2026-10-02", "transactionAmount": {"amount": "-5", "currency": "EUR"}, "creditorName": "BOULANGERIE"}
    assert tx_from_gocardless("acc", raw).id == tx_from_gocardless("acc", dict(raw)).id


# --------------------------------------------------------------------- doublons
def test_duplicate_same_account():
    a, b = tx(1, TODAY, -64.3, "CARREFOUR"), tx(2, TODAY - timedelta(days=1), -64.3, "CARREFOUR")
    alerts = detect_duplicates([a, b], [a, b], S)
    assert len(alerts) == 1 and alerts[0].severity == "high"


def test_duplicate_ignores_far_dates_and_small_amounts():
    a, b = tx(1, TODAY, -64.3, "CARREFOUR"), tx(2, TODAY - timedelta(days=10), -64.3, "CARREFOUR")
    c, d = tx(3, TODAY, -1.2, "CAFE"), tx(4, TODAY, -1.2, "CAFE")
    assert detect_duplicates([a, b, c, d], [a, b, c, d], S) == []


def test_duplicate_cross_account_is_medium():
    a, b = tx(1, TODAY, -99, "AMAZON", "a1"), tx(2, TODAY, -99, "AMAZON", "a2")
    assert detect_duplicates([a, b], [a], S)[0].severity == "medium"


# ------------------------------------------------------------- récurrents
def test_recurring_detects_stable_monthly_only():
    hist = monthly("NETFLIX", -13.49) + [tx(100 + i, TODAY - timedelta(days=30 * i), -(20 + 40 * (i % 3)), "LEROY MERLIN", cat="shopping") for i in range(5)]
    rec = recurring_merchants(hist)
    assert "netflix" in rec and "leroy merlin" not in rec


def test_recurring_amount_change():
    hist = monthly("EDF", -78.0, cat="logement")
    new = tx(99, TODAY, -105.3, "EDF", cat="logement")
    alerts = detect_recurring_anomalies(hist + [new], [new], S)
    assert any(a.kind == "recurring_amount" for a in alerts)


def test_recurring_charged_twice():
    hist = monthly("BASIC FIT", -29.99)
    new = tx(99, hist[-1].booking_date + timedelta(days=6), -29.99, "BASIC FIT")
    alerts = detect_recurring_anomalies(hist + [new], [new], S)
    assert [a.kind for a in alerts] == ["recurring_double"]


def test_recurring_normal_month_no_alert():
    hist = monthly("SPOTIFY", -10.99, n=6)
    last = hist[-1]
    assert detect_recurring_anomalies(hist, [last], S) == []


# ------------------------------------------------------------- inhabituel
def test_unusual_amount_known_merchant():
    hist = [tx(i, TODAY - timedelta(days=7 * i), -(22 + i % 5), "UBER EATS") for i in range(1, 10)]
    new = tx(99, TODAY, -142, "UBER EATS")
    alerts = detect_unusual(hist + [new], [new], S)
    assert alerts and alerts[0].kind == "unusual_amount"


def test_new_merchant_high_amount():
    new = tx(99, TODAY, -1299, "APPLE STORE")
    assert detect_unusual([new], [new], S)[0].kind == "new_merchant"


def test_new_merchant_small_amount_ignored():
    new = tx(99, TODAY, -25, "NOUVEAU KEBAB")
    assert detect_unusual([new], [new], S) == []


# ------------------------------------------------------------- surdépense
def test_overspend_category_and_budget():
    hist = []
    i = 0
    for k in (1, 2, 3):
        m_start = date(2026, 11 - k, 1)
        for d in range(0, 14, 3):
            i += 1
            hist.append(tx(i, m_start + timedelta(days=d), -40, "CARREFOUR", cat="courses"))
    for d in range(0, 14, 2):
        i += 1
        hist.append(tx(i, date(2026, 11, 1) + timedelta(days=d), -60, "CARREFOUR", cat="courses"))
    s = Settings(_env_file=None, budgets={"courses": 300})
    kinds = {a.kind for a in detect_overspend(hist, TODAY, s)}
    assert {"overspend_category", "budget_exceeded"} <= kinds


def test_no_overspend_before_5th():
    hist = [tx(1, date(2026, 11, 1), -2000, "X", cat="shopping")]
    assert all(a.kind.startswith("budget") for a in detect_overspend(hist, date(2026, 11, 2), S))


# ------------------------------------------------------------- catégorisation
@pytest.mark.parametrize("who,amount,expected", [
    ("CB CARREFOUR MARKET", -40, "courses"),
    ("PRLV SEPA NETFLIX", -13.49, "abonnements"),
    ("VIR FACTURE MISSION", 6200, "revenus"),
    ("VIR LIVRET A", -500, "epargne_virement"),
    ("TRUC INCONNU", -12, "autre"),
])
def test_categorize(who, amount, expected):
    assert Categorizer().categorize(who, who, amount) == expected


# ------------------------------------------------------------- bout en bout
def test_demo_end_to_end_catches_all_planted_anomalies(tmp_path):
    db = Database(tmp_path / "t.db")
    _, alerts = load_demo(db, notify=False)
    kinds = [a.kind for a in alerts]
    assert kinds.count("duplicate") >= 1
    assert "new_merchant" in kinds
    assert "unusual_amount" in kinds
    assert "recurring_amount" in kinds
    assert kinds.count("recurring_double") >= 2  # Netflix + Basic-Fit
    # idempotence : relancer ne recrée rien
    from bankwatch.pipeline import run_checks
    assert run_checks(db, None, notify=False) == []


def test_demo_has_no_future_transactions():
    assert max(t.booking_date for t in generate()) <= date.today()


# ------------------------------------------------------------- virements internes & comptes ignorés
@pytest.mark.parametrize("cp,label", [
    ("Alice martin", "n5fe7798 Amex"),
    ("", "VIR INST ALICE MARTIN EI"),
    ("", "VIR INST MARTIN ALICE Virement depuis BoursoB"),
    ("M. Alice Martin", "VIR"),
])
def test_owner_transfer_is_internal(cp, label):
    c = Categorizer(owner_names=["Alice Martin"])
    assert c.categorize(cp, label, -500) == "epargne_virement"
    assert c.categorize(cp, label, 500) == "epargne_virement"


def test_owner_name_needs_all_tokens():
    assert Categorizer(owner_names=["alice martin"]).categorize("STAN SMITH SHOES", "CB", -80) != "epargne_virement"


def test_internal_transfer_not_unusual():
    t = tx(1, TODAY, -500, "Alice martin", cat="epargne_virement")
    assert detect_unusual([t], [t], S) == []


def test_ignored_account_excluded(tmp_path):
    db = Database(tmp_path / "t.db")
    for acc in ("main", "card"):
        db.upsert_account(id=acc, bank="b", institution_id="X", requisition_id=None, iban=None, name=acc, currency="EUR")
    db.upsert_transactions([tx(1, TODAY, -10, "A", acc="main"), tx(2, TODAY, -10, "A", acc="card")])
    db.set_ignored("card", True)
    assert [t.account_id for t in db.transactions()] == ["main"]
    assert len(db.transactions(include_ignored=True)) == 2
    assert [a["id"] for a in db.accounts(include_ignored=False)] == ["main"]


def test_owner_names_env_format():
    s = Settings(_env_file=None, owner_names="alice martin, Karakuri Studio ,")
    assert s.owner_list == ["alice martin", "Karakuri Studio"]


def test_dismissed_alert_hidden_and_not_recreated(tmp_path):
    db = Database(tmp_path / "t.db")
    assert db.add_alert("fp1", "duplicate", "high", "t", "m")
    [a] = db.alerts()
    assert db.dismiss_alert(a["id"])
    assert db.alerts() == [] and len(db.alerts(include_dismissed=True)) == 1
    assert db.add_alert("fp1", "duplicate", "high", "t", "m") is False  # même empreinte : pas recréée
    assert db.unsent_alerts() == []


@pytest.mark.parametrize("who,expected", [
    ("UBER   *EATS", "restos"),
    ("UBER*EATS", "restos"),
    ("UBER   *ONE MEMBERSHIP", "restos"),
    ("UBER *TRIP", "transport"),
    ("EFFIA CONCESSIONS", "transport"),
    ("DIRECTION GENERALE DES FINANCES PUBLIQUES", "impots_taxes"),
    ("Hotel at Booking.com", "voyages"),
    ("NISHAN SUPERETTE", "courses"),
])
def test_categorize_bank_labels(who, expected):
    assert Categorizer(owner_names=[], custom={}).categorize(who, who, -20) == expected


def test_custom_rules_take_precedence(tmp_path):
    from bankwatch.categorize import load_rules

    p = tmp_path / "c.json"
    p.write_text('{"perso": ["mon-coiffeur"], "transport": ["dott"], "restos": ["les delices de a"]}')
    c = Categorizer(owner_names=[], custom_path=p)
    assert c.categorize("MON-COIFFEUR", "CARTE 02/08/26 MON-COIFFEUR", -30) == "perso"
    assert c.categorize("Dott scooter ride", "Dott scooter ride", -3) == "transport"
    assert c.categorize("LES DELICES DE A", "LES DELICES DE A", -18) == "restos"
    assert c.categorize("UBER   *EATS", "", -20) == "restos"  # défauts conservés


def test_owner_names_from_settings_not_split_by_letter(monkeypatch):
    import bankwatch.categorize as cz
    from bankwatch.config import settings as cfg

    monkeypatch.setattr(cfg, "owner_names", "alice martin")
    c = cz.Categorizer(custom={})
    assert c._owners == [{"alice", "martin"}]
    assert c.categorize("MON ASSUREUR", "D-006GZCT4RJSB REF-00B19600166CD24D08", -22.99) != "epargne_virement"
    assert c.categorize("", "VIR INST ALICE MARTIN EI", 500) == "epargne_virement"


def test_client_invoice_with_owner_name_in_label_is_income():
    c = Categorizer(owner_names=["Alice Martin"], custom={})
    assert c.categorize("ACME SARL", "FACTURE ALICE MARTIN VIREMENT 10/2026", 13200) == "revenus"
    assert c.categorize("Alice MARTIN", "n893966dad532706182c31afa83bbb0c", -7000) == "epargne_virement"
    assert c.categorize("", "VIR INST ALICE MARTIN EI", 500) == "epargne_virement"  # BoursoBank : pas de contrepartie


def test_transfer_word_only_internal_without_counterparty():
    c = Categorizer(owner_names=["Alice Martin"], custom={})
    assert c.categorize("ACME SARL", "VIREMENT ALICE MARTIN VIR", 12600) == "revenus"
    assert c.categorize("ACME SARL", "PRESTA ALICE MARTINS VIR", 13302.44) == "revenus"
    assert c.categorize("", "VIR SEPA LOYER OCTOBRE", -1150) == "logement"  # mot-clé explicite d'abord
    assert c.categorize("", "VIR INST M. DUPONT", -200) == "epargne_virement"  # contrepartie inconnue
    assert c.categorize("Jean Dupont", "VIR remboursement resto", 40) == "revenus"


def test_amex_settlement_both_sides_internal():
    c = Categorizer(owner_names=["Alice Martin"],
                    custom={"epargne_virement": ["american express -france", "automatique enregistre-merci"]})
    assert c.categorize("", "American Express -France 4826300DO57Y2SDT RUM E202", -474.46) == "epargne_virement"
    assert c.categorize("AUTOMATIQUE ENREGISTRE-MERCI", "PRELEVEMENT AUTOMATIQUE ENREGISTRE-MERCI", 474.46) == "epargne_virement"
    assert c.categorize("WWW.ZALANDO.FR", "WWW.ZALANDO.FR BERLIN", -58) != "epargne_virement"


def test_booked_replaces_pending_with_other_reference(tmp_path):
    db = Database(tmp_path / "t.db")
    db.upsert_account(id="indy", bank="indy", institution_id="X", requisition_id=None, iban=None, name="pro", currency="EUR")
    pend = Tx(id="indy:aucao_1", account_id="indy", booking_date=date(2026, 11, 3), amount=-172.9,
              counterparty="SAASCO* PRO SUB", label="PRO SUB", status="pending", category="abonnements")
    assert len(db.upsert_transactions([pend])) == 1
    db.add_alert("x", "watch_subscription", "low", "t", "m", tx_ids=["indy:aucao_1"])
    booked = Tx(id="indy:decao_2", account_id="indy", booking_date=date(2026, 11, 4), amount=-172.9,
                counterparty="SAASCO* PRO SUB", label="PRO SUB", status="booked", category="abonnements")
    assert db.upsert_transactions([booked]) == []  # remplacement, pas d'ajout
    rows = db.transactions(include_ignored=True)
    assert len(rows) == 1 and rows[0].id == "indy:decao_2" and rows[0].status == "booked" and rows[0].booking_date == date(2026, 11, 4)
    assert '"indy:decao_2"' in db.alerts()[0]["tx_ids"]  # l'alerte suit la nouvelle référence
    # un vrai second paiement identique, une fois le premier comptabilisé, reste une 2e transaction
    again = Tx(id="indy:decao_3", account_id="indy", booking_date=date(2026, 11, 4), amount=-172.9,
               counterparty="SAASCO* PRO SUB", label="PRO SUB", status="booked", category="abonnements")
    assert len(db.upsert_transactions([again])) == 1


def test_pending_without_merchant_matches_by_amount(tmp_path):
    db = Database(tmp_path / "t.db")
    db.upsert_account(id="sg", bank="sg", institution_id="X", requisition_id=None, iban=None, name="c", currency="EUR")
    db.upsert_transactions([Tx(id="sg:h1", account_id="sg", booking_date=date(2026, 11, 3), amount=-7.8,
                               counterparty="EUROPEEN DE: PayPal Europe", label="PRLV", status="pending")])
    assert db.upsert_transactions([Tx(id="sg:2026-11-03T00:0013362876", account_id="sg", booking_date=date(2026, 11, 3), amount=-7.8,
                                      counterparty="EUROPEEN DE: PayPal Europe", label="PRLV", status="booked")]) == []
    assert len(db.transactions(include_ignored=True)) == 1


def test_dedupe_pending_cleans_existing_twins(tmp_path):
    db = Database(tmp_path / "t.db")
    db.upsert_account(id="indy", bank="indy", institution_id="X", requisition_id=None, iban=None, name="pro", currency="EUR")
    # insertion « ancienne » : les deux existent déjà en base (avant le correctif)
    for i, (st, d) in enumerate((("pending", 3), ("booked", 4))):
        db.conn.execute("INSERT INTO transactions(id,account_id,booking_date,amount,counterparty,label,status,first_seen) VALUES(?,?,?,?,?,?,?,?)",
                        (f"indy:{i}", "indy", f"2026-11-0{d}", -172.9, "SAASCO* PRO SUB", "x", st, "now"))
    db.conn.commit()
    db.add_alert("dup:indy:0:indy:1", "duplicate", "high", "Doublon", "m", tx_ids=["indy:0", "indy:1"])
    assert db.dedupe_pending() == (1, 1)
    assert [t.id for t in db.transactions(include_ignored=True)] == ["indy:1"]
    assert db.alerts() == [] and len(db.alerts(include_dismissed=True)) == 1


def test_generic_pending_label_matches_booked(tmp_path):
    db = Database(tmp_path / "t.db")
    db.upsert_account(id="sg", bank="sg", institution_id="X", requisition_id=None, iban=None, name="c", currency="EUR")
    db.upsert_transactions([Tx(id="sg:p", account_id="sg", booking_date=date(2026, 11, 2), amount=-20,
                               counterparty="OPERATION EN COURS DE TRAITEME", label="OPERATION EN COURS DE TRAITEMENT FRAIS PRELEVEMENT IMPAYE DE", status="pending")])
    new = db.upsert_transactions([Tx(id="sg:b", account_id="sg", booking_date=date(2026, 11, 3), amount=-20,
                                     counterparty="FRAIS IMPAYE DE 172,89 EUR DU", label="FRAIS PRELEVEMENT IMPAYE DE 172,89 EUR DU 02/10/2026", status="booked")])
    assert new == [] and [t.id for t in db.transactions(include_ignored=True)] == ["sg:b"]


def test_custom_rules_ignore_comments(tmp_path):
    from bankwatch.categorize import load_custom

    p = tmp_path / "c.json"
    p.write_text('{"_comment": "ceci est un commentaire", "perso": ["x"], "bidon": "pas une liste"}')
    assert load_custom(p) == {"perso": ["x"]}
