from datetime import date

from bankwatch.db import Database
from bankwatch.importer import counterparty_from_label, import_file, parse_amount, parse_csv, parse_date, parse_ofx


def test_parse_amount_formats():
    assert parse_amount("-1 234,56") == -1234.56
    assert parse_amount("1.234,56") == 1234.56
    assert parse_amount("1,234.56") == 1234.56
    assert parse_amount("-12.5 €") == -12.5
    assert parse_amount("(10,00)") == -10.0
    assert parse_amount("") is None


def test_parse_date_formats():
    assert parse_date("03/11/2026") == date(2026, 11, 3)
    assert parse_date("2026-11-03") == date(2026, 11, 3)
    assert parse_date("20261103") == date(2026, 11, 3)


def test_csv_amount_column_semicolon():
    text = "Compte pro\n\nDate;Libellé;Montant;Catégorie\n01/11/2026;CB CARREFOUR MARKET;-64,30;Courses\n02/11/2026;VIR FACTURE CLIENT;3 200,00;\n"
    rows = parse_csv(text)
    assert [r["amount"] for r in rows] == [-64.3, 3200.0]
    assert rows[0]["label"] == "CB CARREFOUR MARKET"


def test_csv_debit_credit_columns():
    text = "Date opération,Libellé,Débit,Crédit\n2026-11-01,PRLV EDF,78.00,\n2026-11-02,VIR SALAIRE,,2500.00\n"
    assert [r["amount"] for r in parse_csv(text)] == [-78.0, 2500.0]


def test_ofx():
    text = """<OFX><BANKTRANLIST><STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261101<TRNAMT>-13.49<FITID>F1<NAME>NETFLIX<MEMO>PRLV
</STMTTRN><STMTTRN><TRNTYPE>DEBIT<DTPOSTED>20261102<TRNAMT>-9.00<FITID>F2<NAME>GITHUB</STMTTRN></BANKTRANLIST></OFX>"""
    rows = parse_ofx(text)
    assert [(r["fitid"], r["amount"]) for r in rows] == [("F1", -13.49), ("F2", -9.0)]


def test_counterparty_cleanup():
    assert counterparty_from_label("CARTE X1234 12/10 CARREFOUR MARKET") == "CARREFOUR MARKET"


def test_import_idempotent_and_keeps_real_duplicates(tmp_path):
    f = tmp_path / "r.csv"
    f.write_text("Date;Libellé;Montant\n01/11/2026;CB CARREFOUR;-64,30\n01/11/2026;CB CARREFOUR;-64,30\n", encoding="cp1252")
    db = Database(tmp_path / "t.db")
    assert import_file(db, f, "indy") == (2, 2)  # deux lignes identiques = deux transactions
    assert import_file(db, f, "indy") == (2, 0)  # réimport : rien de nouveau
    assert db.accounts()[0]["institution_id"] == "CSV"
