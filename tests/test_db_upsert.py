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


# ----------------------------------------------------------- en attente sans référence (SG)
def _eb_pending(value_date, amount="50.00", label="CARTE RETRAIT DAB  XXXX 12/11"):
    """Ligne telle que la renvoie la banque tant que c'est en attente : ni référence, ni date d'opération ;
    la date de valeur est celle du jour de l'interrogation."""
    return {"entry_reference": None, "transaction_amount": {"currency": "EUR", "amount": amount}, "creditor": None, "debtor": None,
            "credit_debit_indicator": "DBIT", "status": "PDNG", "booking_date": None, "value_date": value_date,
            "transaction_date": None, "remittance_information": [label]}


def _parse(acc, items):
    from bankwatch.db import tx_from_gocardless
    from bankwatch.enablebanking import is_pending, to_raw

    return [tx_from_gocardless(acc, to_raw(t), "pending" if is_pending(t) else "booked") for t in items]


def _sg(tmp_path):
    db = Database(tmp_path / "s.db")
    db.upsert_account(id="sg", bank="sg", institution_id="X", requisition_id=None, iban=None, name="c", currency="EUR")
    return db


def test_pending_without_reference_is_not_duplicated_when_its_date_drifts(tmp_path):
    """Le même retrait en attente, relu le lendemain avec la date du jour, reste UNE opération."""
    db = _sg(tmp_path)
    day1 = _parse("sg", [_eb_pending("2026-11-12")])
    assert len(db.upsert_transactions(db.fold_drifting_pending(day1))) == 1
    day2 = _parse("sg", [_eb_pending("2026-11-13")])
    assert day2[0].id != day1[0].id  # l'empreinte change avec la date : c'était la cause du faux doublon
    assert db.upsert_transactions(db.fold_drifting_pending(day2)) == []
    day3 = _parse("sg", [_eb_pending("2026-11-14")])
    assert db.upsert_transactions(db.fold_drifting_pending(day3)) == []
    rows = db.conn.execute("SELECT id, booking_date FROM transactions").fetchall()
    assert [(r["id"], r["booking_date"]) for r in rows] == [(day1[0].id, "2026-11-12")]  # date de la première vue
    # comptabilisée ensuite sous une vraie référence : elle remplace la ligne en attente
    booked = {**_eb_pending("2026-11-13"), "entry_reference": "REF9", "status": "BOOK", "booking_date": "2026-11-13"}
    assert db.upsert_transactions(db.fold_drifting_pending(_parse("sg", [booked]))) == []
    rows = db.conn.execute("SELECT id, status FROM transactions").fetchall()
    assert [(r["id"], r["status"]) for r in rows] == [("sg:REF9", "booked")]


def test_two_real_pending_withdrawals_are_kept(tmp_path):
    """Si la banque renvoie en même temps l'ancienne et la nouvelle ligne, ce sont deux retraits."""
    db = _sg(tmp_path)
    db.upsert_transactions(db.fold_drifting_pending(_parse("sg", [_eb_pending("2026-11-12")])))
    both = _parse("sg", [_eb_pending("2026-11-12"), _eb_pending("2026-11-13")])
    assert len(db.upsert_transactions(db.fold_drifting_pending(both))) == 1
    assert db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 2
    # autre libellé ou autre montant : jamais rattaché
    other = _parse("sg", [_eb_pending("2026-11-14", label="CARTE RETRAIT DAB  YYYY 14/11"), _eb_pending("2026-11-14", amount="20.00")])
    assert len(db.upsert_transactions(db.fold_drifting_pending(other))) == 2


def test_vanished_pending_is_removed_and_its_alerts_settled(tmp_path):
    db = _sg(tmp_path)
    mk = lambda i, d, st="pending": Tx(id=f"sg:{i}", account_id="sg", booking_date=d, amount=-14.2, counterparty="VTC", label=f"VTC {i}", status=st)  # noqa: E731
    db.upsert_transactions([mk("old", date(2026, 10, 8)), mk("gone", date(2026, 11, 10)), mk("still", date(2026, 11, 12)),
                            mk("outside", date(2026, 11, 1)),
                            Tx(id="sg:booked", account_id="sg", booking_date=date(2026, 11, 10), amount=-30, counterparty="EPICERIE", label="CB")])
    db.add_alert("big:sg:gone", "big_amount", "low", "Débit important", "m", tx_ids=["sg:gone"])
    db.add_alert("big:sg:still", "big_amount", "low", "Débit important", "m", tx_ids=["sg:still"])
    # lecture du 13/11 sur 7 jours : la banque ne renvoie plus que « still » en attente
    removed, settled = db.purge_vanished_pending("sg", {"sg:still", "sg:booked"}, window_start=date(2026, 11, 6), today=date(2026, 11, 13))
    assert (removed, settled) == (2, 1)
    # « old » (> 30 j) et « gone » (dans la fenêtre, plus renvoyée) partent ; « outside » (hors fenêtre lue) reste
    assert sorted(r["id"] for r in db.conn.execute("SELECT id FROM transactions")) == ["sg:booked", "sg:outside", "sg:still"]
    assert [a["fingerprint"] for a in db.alerts(active_only=True)] == ["big:sg:still"]


def test_eb_sync_folds_and_purges(tmp_path, monkeypatch):
    from bankwatch import sync as sync_mod

    class FakeEB:
        def __init__(self):
            self.feed = []

        def transactions(self, account_id, date_from=None):
            return self.feed

        def balances(self, account_id):
            return []

    db, eb = _sg(tmp_path), FakeEB()
    acc = lambda: db.conn.execute("SELECT * FROM accounts WHERE id='sg'").fetchone()  # noqa: E731
    eb.feed = [_eb_pending("2026-11-12")]
    r, new = sync_mod.eb_sync_account(db, eb, acc(), today=date(2026, 11, 12))
    assert r.error is None and len(new) == 1
    eb.feed = [_eb_pending("2026-11-13")]
    r, new = sync_mod.eb_sync_account(db, eb, acc(), today=date(2026, 11, 13))
    assert r.error is None and new == [] and db.conn.execute("SELECT COUNT(*) FROM transactions").fetchone()[0] == 1
    # l'autorisation disparaît sans être comptabilisée (annulée) : la ligne est retirée
    eb.feed = [{**_eb_pending("2026-11-13", amount="9.00", label="AUTRE"), "entry_reference": "R1", "status": "BOOK", "booking_date": "2026-11-13"}]
    sync_mod.eb_sync_account(db, eb, acc(), today=date(2026, 11, 14))
    assert [r["id"] for r in db.conn.execute("SELECT id FROM transactions")] == ["sg:R1"]
