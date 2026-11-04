"""Import de relevés CSV / OFX exportés depuis l'espace client des banques.

- CSV : détection auto du séparateur, de l'encodage et des colonnes
  (date, libellé, montant — ou débit/crédit séparés), formats de nombres français.
- OFX/QFX : blocs <STMTTRN> (DTPOSTED, TRNAMT, NAME, MEMO, FITID).

Identifiants stables : réimporter le même fichier (ou un export qui chevauche) n'ajoute
rien, mais deux opérations identiques le même jour restent bien DEUX transactions
(c'est justement ce qu'on veut détecter comme doublon).
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import unicodedata
from collections import Counter
from datetime import date, datetime
from pathlib import Path

from .categorize import Categorizer
from .db import Database, Tx


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z]+", " ", s).strip()


def parse_amount(s: str) -> float | None:
    s = (s or "").strip().replace(" ", "").replace(" ", "").replace(" ", "").replace("€", "").replace("EUR", "")
    if not s:
        return None
    neg = s.startswith("(") and s.endswith(")")
    s = s.strip("()+")
    if "," in s and "." in s:  # 1.234,56 ou 1,234.56
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    else:
        s = s.replace(",", ".")
    try:
        v = float(s)
    except ValueError:
        return None
    return -v if neg else v


def parse_date(s: str) -> date | None:
    s = (s or "").strip()[:10]
    for fmt in ("%d/%m/%Y", "%Y-%m-%d", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%Y%m%d"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", "replace")


COLS = {
    "date": ("date operation", "date de l operation", "dateop", "date op", "date comptable", "date", "booking date"),
    "vdate": ("date de valeur", "date valeur", "value date"),
    "label": ("libelle", "label", "libelle operation", "description", "detail", "intitule", "nom", "operation"),
    "label2": ("detail de l ecriture", "informations complementaires", "memo", "commentaire", "supplierfound", "merchant"),
    "amount": ("montant", "amount", "montant eur", "montant euros", "valeur"),
    "debit": ("debit", "debit eur", "debit euros"),
    "credit": ("credit", "credit eur", "credit euros"),
    "category": ("categorie", "category", "categoryparent"),
}


def _map_columns(header: list[str]) -> dict[str, int]:
    norm = [_norm(h) for h in header]
    found: dict[str, int] = {}
    for key, names in COLS.items():
        for name in names:  # ordre de préférence
            for i, h in enumerate(norm):
                if i in found.values():
                    continue
                if h == name or (key not in ("date", "amount") and h.startswith(name)):
                    found.setdefault(key, i)
            if key in found:
                break
    return found


def parse_csv(text: str) -> list[dict]:
    lines = text.splitlines()
    sample = "\n".join(lines[:30])
    delim = max(";,\t|", key=lambda d: sample.count(d))
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    # en-tête = première ligne qui contient une colonne date et une colonne montant/débit
    for hi, row in enumerate(rows[:40]):  # noqa: B007 — hi est réutilisé après la boucle (ligne d'en-tête)
        cols = _map_columns(row)
        if "date" in cols and ("amount" in cols or "debit" in cols or "credit" in cols):
            break
    else:
        raise ValueError("En-tête introuvable : il faut au moins une colonne date et une colonne montant (ou débit/crédit).")
    out = []
    for row in rows[hi + 1:]:
        if len(row) <= cols["date"]:
            continue
        d = parse_date(row[cols["date"]])
        if not d:
            continue

        def cell(k: str, row: list[str] = row) -> str:
            i = cols.get(k)
            return row[i].strip() if i is not None and i < len(row) else ""

        if "amount" in cols:
            amount = parse_amount(cell("amount"))
        else:
            deb, cre = parse_amount(cell("debit")), parse_amount(cell("credit"))
            amount = (cre or 0) - abs(deb or 0) if (deb or cre) else None
        if amount is None:
            continue
        label = " ".join(x for x in (cell("label"), cell("label2")) if x)
        out.append({"date": d, "value_date": parse_date(cell("vdate")), "amount": amount,
                    "label": re.sub(r"\s+", " ", label), "category": cell("category")})
    return out


def parse_ofx(text: str) -> list[dict]:
    out = []
    for block in re.findall(r"<STMTTRN>(.*?)(?:</STMTTRN>|(?=<STMTTRN>)|(?=</BANKTRANLIST>))", text, flags=re.S | re.I):
        def tag(name: str, block: str = block) -> str:
            m = re.search(rf"<{name}>([^<\r\n]*)", block, flags=re.I)
            return m.group(1).strip() if m else ""

        d = parse_date(tag("DTPOSTED")[:8])
        amount = parse_amount(tag("TRNAMT"))
        if not d or amount is None:
            continue
        label = " ".join(x for x in (tag("NAME"), tag("MEMO")) if x)
        out.append({"date": d, "value_date": None, "amount": amount, "label": label,
                    "category": "", "fitid": tag("FITID")})
    return out


def counterparty_from_label(label: str) -> str:
    """Extrait un nom de marchand lisible d'un libellé brut ('CARTE X1234 12/10 CARREFOUR MARKET' -> 'CARREFOUR MARKET')."""
    t = re.sub(r"\b(CARTE|CB|PAIEMENT|PAIEMT|PRLV|PRELEVEMENT|SEPA|VIR|VIREMENT|RECU|EMIS|INST|FACTURE)\b", " ", label, flags=re.I)
    t = re.sub(r"\bX\d{3,4}\b|\b\d{1,2}/\d{1,2}(/\d{2,4})?\b|\b\d{4,}\b|CB\*\d+", " ", t)
    t = re.sub(r"\s+", " ", t).strip(" -:/*")
    return t[:60] or label[:60]


def import_file(db: Database, path: Path, bank: str, account_name: str | None = None) -> tuple[int, int]:
    """Importe un relevé. Retourne (lignes lues, nouvelles transactions)."""
    text = _decode(Path(path).read_bytes())
    rows = parse_ofx(text) if "<OFX>" in text.upper() or "<STMTTRN>" in text.upper() else parse_csv(text)
    acc_id = f"csv-{bank}"
    db.upsert_account(id=acc_id, bank=bank, institution_id="CSV", requisition_id=None,
                      iban=None, name=account_name or f"{bank} (relevés)", currency="EUR")
    cat = Categorizer()
    seen: Counter = Counter()
    txs = []
    for r in rows:
        if r.get("fitid"):
            tid = f"{acc_id}:{r['fitid']}"
        else:
            key = f"{r['date']}|{r['amount']:.2f}|{r['label']}"
            seen[key] += 1  # n-ième occurrence identique du jour : garde les vrais doublons
            tid = f"{acc_id}:c" + hashlib.sha1(f"{key}|{seen[key]}".encode()).hexdigest()[:20]
        who = counterparty_from_label(r["label"])
        txs.append(Tx(id=tid, account_id=acc_id, booking_date=r["date"], value_date=r["value_date"],
                      amount=r["amount"], counterparty=who, label=r["label"],
                      category=cat.categorize(who, r["label"], r["amount"]), raw={"source": "import"}))
    new = db.upsert_transactions(txs)
    return len(rows), len(new)
