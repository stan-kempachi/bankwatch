from datetime import date

from bankwatch.db import Database, Tx


def _db(tmp_path):
    db = Database(tmp_path / "u.db")
    db.upsert_account(id="pp", bank="paypal", institution_id="X", requisition_id=None, iban=None, name="PayPal", currency="EUR")
    return db


def test_same_reference_opposite_amounts_are_two_transactions(tmp_path):
    """PayPal renvoie l'achat (−X) et son alimentation (+X) sous la même référence : les deux doivent exister,
    et une resynchro ne doit rien dupliquer."""
    db = _db(tmp_path)
    rows = [Tx(id="pp:REF1", account_id="pp", booking_date=date(2026, 11, 8), amount=-25.0,
               counterparty="Vinted", label="Vinted", category="shopping"),
            Tx(id="pp:REF1", account_id="pp", booking_date=date(2026, 11, 8), amount=25.0,
               counterparty="BoursoBank", label="Alimentation depuis la banque", category="epargne_virement")]
    new = db.upsert_transactions(rows)
    assert len(new) == 2
    ids = sorted(r["id"] for r in db.conn.execute("SELECT id FROM transactions"))
    assert ids == ["pp:REF1", "pp:REF1:c"]
    assert db.upsert_transactions(rows) == []  # idempotent
    assert db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2


def test_pending_to_booked_with_changed_amount_updates_in_place(tmp_path):
    """Une pré-autorisation en attente dont le montant change en comptabilisé reste une seule opération."""
    db = _db(tmp_path)
    db.upsert_transactions([Tx(id="pp:REF2", account_id="pp", booking_date=date(2026, 11, 8), amount=-60.0,
                               counterparty="Station", label="Carburant", category="transport", status="pending")])
    new = db.upsert_transactions([Tx(id="pp:REF2", account_id="pp", booking_date=date(2026, 11, 9), amount=-47.3,
                                     counterparty="Station", label="Carburant", category="transport", status="booked")])
    assert new == []
    row = db.conn.execute("SELECT amount, status FROM transactions WHERE id='pp:REF2'").fetchone()
    assert (row["amount"], row["status"]) == (-47.3, "booked")
    assert db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
