"""Stockage SQLite : comptes, transactions normalisées, alertes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .config import settings

SCHEMA = """
CREATE TABLE IF NOT EXISTS requisitions (
    id TEXT PRIMARY KEY,
    bank TEXT NOT NULL,
    institution_id TEXT NOT NULL,
    status TEXT,
    link TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id TEXT PRIMARY KEY,
    bank TEXT NOT NULL,
    institution_id TEXT,
    requisition_id TEXT,
    iban TEXT,
    name TEXT,
    currency TEXT DEFAULT 'EUR',
    balance REAL,
    balance_date TEXT,
    last_sync TEXT
);
CREATE TABLE IF NOT EXISTS transactions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    booking_date TEXT NOT NULL,
    value_date TEXT,
    amount REAL NOT NULL,
    currency TEXT DEFAULT 'EUR',
    counterparty TEXT,
    label TEXT,
    category TEXT,
    status TEXT NOT NULL DEFAULT 'booked',
    raw TEXT,
    first_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tx_account_date ON transactions(account_id, booking_date);
CREATE INDEX IF NOT EXISTS ix_tx_counterparty ON transactions(counterparty);
CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,
    severity TEXT NOT NULL,
    title TEXT NOT NULL,
    message TEXT NOT NULL,
    amount REAL,
    tx_ids TEXT,
    created_at TEXT NOT NULL,
    sent_at TEXT
);
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Tx:
    id: str
    account_id: str
    booking_date: date
    amount: float  # négatif = débit
    counterparty: str = ""
    label: str = ""
    category: str = ""
    status: str = "booked"
    currency: str = "EUR"
    value_date: date | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_debit(self) -> bool:
        return self.amount < 0

    @property
    def merchant_key(self) -> str:
        return normalize_merchant(self.counterparty or self.label)


def normalize_merchant(text: str) -> str:
    """Clé de regroupement d'un marchand : minuscules, sans chiffres/dates/ponctuation."""
    import re

    t = (text or "").lower()
    t = re.sub(r"\b\d{1,2}[/.-]\d{1,2}([/.-]\d{2,4})?\b", " ", t)  # dates
    t = re.sub(r"\bcb\b|\bcarte\b|\bpaiement\b|\bprlv\b|\bsepa\b|\bvir\b|\bfacture\b", " ", t)
    t = re.sub(r"[^a-zà-ÿ ]+", " ", t)
    words = [w for w in t.split() if len(w) > 1]  # vire les restes de n° de carte (« x1234 » -> « x »)
    return " ".join(words[:2])  # 2 premiers mots : « carrefour market », sans la ville qui varie


GENERIC_PENDING = ("operation en cours", "en cours de traitement", "autorisation", "paiement en attente")


def is_generic_pending(counterparty: str | None, label: str | None) -> bool:
    """Libellé d'attente sans nom de marchand (SG : « OPERATION EN COURS DE TRAITEMENT … »)."""
    t = f"{counterparty or ''} {label or ''}".lower()
    return any(g in t for g in GENERIC_PENDING)


def fingerprint_tx(account_id: str, raw: dict[str, Any]) -> str:
    tid = raw.get("transactionId") or raw.get("internalTransactionId")
    if tid:
        return f"{account_id}:{tid}"
    amt = raw.get("transactionAmount", {}).get("amount", "")
    key = "|".join(
        [
            account_id,
            raw.get("bookingDate", ""),
            str(amt),
            raw.get("remittanceInformationUnstructured", "")
            or " ".join(raw.get("remittanceInformationUnstructuredArray", [])),
            raw.get("creditorName", "") or raw.get("debtorName", ""),
        ]
    )
    return account_id + ":h" + hashlib.sha1(key.encode()).hexdigest()[:20]


def tx_from_gocardless(account_id: str, raw: dict[str, Any], status: str = "booked") -> Tx:
    amount = float(raw["transactionAmount"]["amount"])
    currency = raw["transactionAmount"].get("currency", "EUR")
    counterparty = (raw.get("creditorName") if amount < 0 else raw.get("debtorName")) or ""
    label = raw.get("remittanceInformationUnstructured") or " ".join(
        raw.get("remittanceInformationUnstructuredArray", [])
    )
    if not label:
        label = raw.get("additionalInformation", "") or counterparty
    if not counterparty:
        counterparty = raw.get("creditorName") or raw.get("debtorName") or ""
    bdate = raw.get("bookingDate") or raw.get("valueDate")
    vdate = raw.get("valueDate")
    return Tx(
        id=fingerprint_tx(account_id, raw),
        account_id=account_id,
        booking_date=date.fromisoformat(bdate),
        value_date=date.fromisoformat(vdate) if vdate else None,
        amount=amount,
        currency=currency,
        counterparty=counterparty.strip(),
        label=label.strip(),
        status=status,
        raw=raw,
    )


class Database:
    def __init__(self, path: Path | str | None = None) -> None:
        self.path = Path(path or settings.db_path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.path))
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        cols = {r[1] for r in self.conn.execute("PRAGMA table_info(accounts)")}
        if "ignored" not in cols:  # migration : comptes exclus de l'analyse (ex : carte miroir)
            self.conn.execute("ALTER TABLE accounts ADD COLUMN ignored INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()
        acols = {r[1] for r in self.conn.execute("PRAGMA table_info(alerts)")}
        if "dismissed" not in acols:  # migration : alertes écartées (fausses alertes, gardées pour ne pas renaître)
            self.conn.execute("ALTER TABLE alerts ADD COLUMN dismissed INTEGER NOT NULL DEFAULT 0")
            self.conn.commit()

    # ----------------------------------------------------------------- meta
    def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_meta(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )
        self.conn.commit()

    # ---------------------------------------------------------- requisitions
    def save_requisition(self, rid: str, bank: str, institution_id: str, status: str, link: str) -> None:
        self.conn.execute(
            """INSERT INTO requisitions(id,bank,institution_id,status,link,created_at)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET status=excluded.status, link=excluded.link""",
            (rid, bank, institution_id, status, link, _now()),
        )
        self.conn.commit()

    def requisitions(self) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM requisitions ORDER BY created_at").fetchall()

    # -------------------------------------------------------------- accounts
    def upsert_account(self, **fields: Any) -> None:
        if not fields.get("id"):
            raise ValueError("compte sans identifiant")
        cols = ["id", "bank", "institution_id", "requisition_id", "iban", "name", "currency"]
        values = [fields.get(c) for c in cols]
        self.conn.execute(
            f"""INSERT INTO accounts({','.join(cols)}) VALUES({','.join('?' * len(cols))})
                ON CONFLICT(id) DO UPDATE SET
                  bank=excluded.bank, iban=COALESCE(excluded.iban, accounts.iban),
                  name=COALESCE(excluded.name, accounts.name),
                  requisition_id=COALESCE(excluded.requisition_id, accounts.requisition_id)""",
            values,
        )
        self.conn.commit()

    def set_balance(self, account_id: str, balance: float, balance_date: str | None, read: bool = False) -> None:
        """`read=True` quand le solde vient d'être LU à la banque (et pas simplement recopié)."""
        self.conn.execute(
            "UPDATE accounts SET balance=?, balance_date=?, last_sync=? WHERE id=?",
            (balance, balance_date, _now(), account_id),
        )
        if read:
            self.conn.execute(
                "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (f"balread:{account_id}", _now()))
        self.conn.commit()

    def balance_read_at(self, account_id: str) -> str | None:
        return self.get_meta(f"balread:{account_id}")

    def effective_balance(self, account_id: str) -> dict[str, Any]:
        """Solde lu à la banque + opérations (y compris en attente) vues DEPUIS cette lecture.
        Entre deux lectures (une par jour, quota DSP2), c'est le meilleur estimateur du solde réel."""
        acc = self.conn.execute("SELECT balance, balance_date FROM accounts WHERE id=?", (account_id,)).fetchone()
        base = float(acc["balance"]) if acc and acc["balance"] is not None else None
        read_at = self.balance_read_at(account_id)
        if base is None:
            return {"balance": None, "estimate": None, "delta": 0.0, "read_at": read_at, "since_count": 0}
        if not read_at:
            return {"balance": base, "estimate": base, "delta": 0.0, "read_at": None, "since_count": 0}
        row = self.conn.execute(
            "SELECT COALESCE(SUM(amount),0) s, COUNT(*) n FROM transactions WHERE account_id=? AND first_seen > ?",
            (account_id, read_at)).fetchone()
        return {"balance": base, "estimate": round(base + row["s"], 2), "delta": round(row["s"], 2),
                "read_at": read_at, "since_count": row["n"]}

    def accounts(self, include_ignored: bool = True) -> list[sqlite3.Row]:
        q = "SELECT * FROM accounts" + ("" if include_ignored else " WHERE ignored = 0")
        return self.conn.execute(q + " ORDER BY bank, name").fetchall()

    def set_ignored(self, account_id: str, ignored: bool) -> int:
        cur = self.conn.execute("UPDATE accounts SET ignored=? WHERE id=?", (int(ignored), account_id))
        self.conn.commit()
        return cur.rowcount

    # ---------------------------------------------------------- transactions
    def upsert_transactions(self, txs: Iterable[Tx]) -> list[Tx]:
        """Insère les nouvelles transactions. Retourne celles qui n'existaient pas encore."""
        new: list[Tx] = []
        now = _now()
        for tx in txs:
            exists = self.conn.execute(
                "SELECT status, amount FROM transactions WHERE id=?", (tx.id,)
            ).fetchone()
            if exists and abs((exists["amount"] or 0) - tx.amount) > 0.005 and not (
                    exists["status"] == "pending" and tx.status == "booked"):
                # Même référence, montant différent : deux opérations distinctes (PayPal renvoie l'achat −X et
                # son alimentation +X sous le même identifiant). On garde la première telle quelle et on
                # suffixe la seconde par son sens, de façon stable d'une synchro à l'autre.
                tx.id = f"{tx.id}:{'c' if tx.amount > 0 else 'd'}"
                exists = self.conn.execute(
                    "SELECT status, amount FROM transactions WHERE id=?", (tx.id,)
                ).fetchone()
            if exists:
                if exists["status"] != tx.status or abs((exists["amount"] or 0) - tx.amount) > 0.005:
                    # passage en attente → comptabilisé : le montant peut bouger (pourboire, pré-autorisation)
                    self.conn.execute(
                        "UPDATE transactions SET status=?, booking_date=?, value_date=?, amount=? WHERE id=?",
                        (tx.status, tx.booking_date.isoformat(),
                         tx.value_date.isoformat() if tx.value_date else None, tx.amount, tx.id),
                    )
                continue
            if tx.status == "booked":
                # La même opération a souvent été vue « en attente » sous une AUTRE référence
                # (Indy : aucao_… puis decao_… ; SG : sans référence). On la remplace au lieu d'ajouter.
                pend = self._matching_pending(tx)
                if pend is not None:
                    self.conn.execute(
                        """UPDATE transactions SET id=?, booking_date=?, value_date=?, status='booked',
                           counterparty=?, label=?, category=?, raw=? WHERE id=?""",
                        (tx.id, tx.booking_date.isoformat(), tx.value_date.isoformat() if tx.value_date else None,
                         tx.counterparty or pend["counterparty"], tx.label or pend["label"],
                         tx.category or pend["category"], json.dumps(tx.raw, ensure_ascii=False), pend["id"]),
                    )
                    self.conn.execute("UPDATE alerts SET tx_ids=REPLACE(tx_ids, ?, ?) WHERE tx_ids LIKE ?",
                                      (json.dumps(pend["id"]), json.dumps(tx.id), f'%{pend["id"]}%'))
                    continue
            self.conn.execute(
                """INSERT INTO transactions(id,account_id,booking_date,value_date,amount,currency,
                   counterparty,label,category,status,raw,first_seen)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    tx.id, tx.account_id, tx.booking_date.isoformat(),
                    tx.value_date.isoformat() if tx.value_date else None,
                    tx.amount, tx.currency, tx.counterparty, tx.label, tx.category, tx.status,
                    json.dumps(tx.raw, ensure_ascii=False), now,
                ),
            )
            new.append(tx)
        self.conn.commit()
        return new

    def dedupe_pending(self) -> tuple[int, int]:
        """Supprime les « en attente » qui ont un jumeau comptabilisé ; écarte les alertes doublon qui les citaient.
        Retourne (transactions supprimées, alertes écartées)."""
        removed, dismissed = 0, 0
        for r in self.conn.execute("SELECT * FROM transactions WHERE status='pending'").fetchall():
            pend = self._row_to_tx(r)
            twin = self.conn.execute(
                """SELECT id FROM transactions WHERE account_id=? AND status='booked' AND id<>?
                   AND abs(amount - ?) < 0.005 AND booking_date BETWEEN ? AND ?""",
                (pend.account_id, pend.id, pend.amount, (pend.booking_date - timedelta(days=3)).isoformat(),
                 (pend.booking_date + timedelta(days=3)).isoformat())).fetchall()
            if not is_generic_pending(pend.counterparty, pend.label):
                twin = [t for t in twin if normalize_merchant(
                    self.conn.execute("SELECT counterparty, label FROM transactions WHERE id=?", (t["id"],)).fetchone()[0] or "") == pend.merchant_key]
            if not twin:
                continue
            self.conn.execute("DELETE FROM transactions WHERE id=?", (pend.id,))
            removed += 1
            for a in self.conn.execute("SELECT id FROM alerts WHERE dismissed=0 AND tx_ids LIKE ?", (f'%{pend.id}%',)).fetchall():
                self.conn.execute("UPDATE alerts SET dismissed=1 WHERE id=?", (a["id"],))
                dismissed += 1
        self.conn.commit()
        return removed, dismissed

    def _matching_pending(self, tx: Tx) -> sqlite3.Row | None:
        """Opération « en attente » du même compte, même montant, même marchand, à ≤ 3 jours."""
        lo = (tx.booking_date - timedelta(days=3)).isoformat()
        hi = (tx.booking_date + timedelta(days=3)).isoformat()
        rows = self.conn.execute(
            """SELECT * FROM transactions WHERE account_id=? AND status='pending'
               AND abs(amount - ?) < 0.005 AND booking_date BETWEEN ? AND ? ORDER BY booking_date""",
            (tx.account_id, tx.amount, lo, hi)).fetchall()
        for r in rows:
            if normalize_merchant(r["counterparty"] or r["label"]) == tx.merchant_key or not tx.merchant_key \
                    or is_generic_pending(r["counterparty"], r["label"]):
                return r
        return rows[0] if rows and len(rows) == 1 else None

    def transactions(
        self,
        since: date | None = None,
        until: date | None = None,
        account_id: str | None = None,
        debits_only: bool = False,
        include_ignored: bool = False,
    ) -> list[Tx]:
        q = "SELECT * FROM transactions WHERE 1=1"
        args: list[Any] = []
        if not include_ignored and not account_id:
            q += " AND account_id NOT IN (SELECT id FROM accounts WHERE ignored = 1)"
        if since:
            q += " AND booking_date >= ?"
            args.append(since.isoformat())
        if until:
            q += " AND booking_date <= ?"
            args.append(until.isoformat())
        if account_id:
            q += " AND account_id = ?"
            args.append(account_id)
        if debits_only:
            q += " AND amount < 0"
        q += " ORDER BY booking_date, id"
        return [self._row_to_tx(r) for r in self.conn.execute(q, args)]

    @staticmethod
    def _row_to_tx(r: sqlite3.Row) -> Tx:
        return Tx(
            id=r["id"],
            account_id=r["account_id"],
            booking_date=date.fromisoformat(r["booking_date"]),
            value_date=date.fromisoformat(r["value_date"]) if r["value_date"] else None,
            amount=r["amount"],
            currency=r["currency"],
            counterparty=r["counterparty"] or "",
            label=r["label"] or "",
            category=r["category"] or "",
            status=r["status"],
            raw=json.loads(r["raw"]) if r["raw"] else {},
        )

    def set_category(self, tx_id: str, category: str) -> None:
        self.conn.execute("UPDATE transactions SET category=? WHERE id=?", (category, tx_id))

    def commit(self) -> None:
        self.conn.commit()

    # ---------------------------------------------------------------- alerts
    def add_alert(
        self, fingerprint: str, kind: str, severity: str, title: str, message: str,
        amount: float | None = None, tx_ids: list[str] | None = None,
    ) -> bool:
        """Retourne True si l'alerte est nouvelle (pas déjà émise)."""
        cur = self.conn.execute(
            """INSERT OR IGNORE INTO alerts(fingerprint,kind,severity,title,message,amount,tx_ids,created_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (fingerprint, kind, severity, title, message, amount,
             json.dumps(tx_ids or []), _now()),
        )
        self.conn.commit()
        return cur.rowcount == 1

    def unsent_alerts(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM alerts WHERE sent_at IS NULL ORDER BY created_at"
        ).fetchall()

    def mark_sent(self, alert_ids: Iterable[int]) -> None:
        ids = list(alert_ids)
        if not ids:
            return
        self.conn.executemany(
            "UPDATE alerts SET sent_at=? WHERE id=?", [(_now(), i) for i in ids]
        )
        self.conn.commit()

    def alerts(self, limit: int = 100, include_dismissed: bool = False) -> list[sqlite3.Row]:
        where = "" if include_dismissed else "WHERE dismissed = 0 "
        return self.conn.execute(
            f"SELECT * FROM alerts {where}ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()

    def dismiss_alert(self, alert_id: int) -> bool:
        cur = self.conn.execute("UPDATE alerts SET dismissed=1, sent_at=COALESCE(sent_at, ?) WHERE id=?", (_now(), alert_id))
        self.conn.commit()
        return cur.rowcount == 1

    def close(self) -> None:
        self.conn.close()
