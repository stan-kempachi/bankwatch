"""Synchronisation GoCardless -> SQLite."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from .categorize import Categorizer
from .config import settings
from .db import Database, Tx, tx_from_gocardless
from .gocardless import GoCardlessClient, GoCardlessError


@dataclass
class SyncResult:
    account_id: str
    bank: str
    new_count: int
    error: str | None = None
    skipped: str | None = None      # synchro sautée volontairement (quota du jour atteint)
    rate_limited: bool = False      # 429 côté banque : pas une perte de consentement


def _access_key(acc_id: str, today: date) -> str:
    return f"access:{acc_id}:{today.isoformat()}"


def accesses_today(db: Database, acc_id: str, today: date | None = None) -> int:
    return int(db.get_meta(_access_key(acc_id, today or date.today()), "0") or 0)


def _count_access(db: Database, acc_id: str, today: date) -> None:
    db.set_meta(_access_key(acc_id, today), str(accesses_today(db, acc_id, today) + 1))


def register_accounts(db: Database, gc: GoCardlessClient) -> int:
    """Pour chaque réquisition connue, récupère les comptes liés et les enregistre."""
    added = 0
    for req in db.requisitions():
        try:
            data = gc.requisition(req["id"])
        except GoCardlessError as e:
            print(f"[warn] réquisition {req['id']} ({req['bank']}): {e}")
            continue
        db.save_requisition(req["id"], req["bank"], req["institution_id"], data.get("status"), data.get("link"))
        for acc_id in data.get("accounts", []):
            details = {}
            try:
                details = gc.account_details(acc_id)
            except GoCardlessError as e:
                print(f"[warn] détails compte {acc_id}: {e}")
            name = details.get("name") or details.get("product") or details.get("ownerName") or acc_id[:8]
            db.upsert_account(
                id=acc_id,
                bank=req["bank"],
                institution_id=req["institution_id"],
                requisition_id=req["id"],
                iban=details.get("iban"),
                name=name,
                currency=details.get("currency", "EUR"),
            )
            added += 1
    return added


def sync_all(db: Database, gc: GoCardlessClient, lookback_days: int | None = None) -> tuple[list[SyncResult], list[Tx]]:
    """Tire les transactions de tous les comptes. Retourne les résultats + toutes les nouvelles tx."""
    cat = Categorizer()
    results: list[SyncResult] = []
    all_new: list[Tx] = []
    for acc in db.accounts():
        if acc["institution_id"] in ("DEMO", "CSV"):
            continue
        acc_id = acc["id"]
        if lookback_days is None:
            # première sync : tout l'historique autorisé ; ensuite : 30 jours glissants (pending -> booked)
            lookback = settings.gc_history_days if acc["last_sync"] is None else 30
        else:
            lookback = lookback_days
        date_from = date.today() - timedelta(days=lookback)
        try:
            txs = gc.transactions(acc_id, date_from=date_from)
            parsed = [tx_from_gocardless(acc_id, r, "booked") for r in txs["booked"]]
            parsed += [tx_from_gocardless(acc_id, r, "pending") for r in txs["pending"]]
            for tx in parsed:
                tx.category = cat.categorize(tx.counterparty, tx.label, tx.amount)
            new = db.upsert_transactions(parsed)
            all_new.extend(new)
            try:
                bal = _pick_balance(gc.balances(acc_id))
                if bal is not None:
                    db.set_balance(acc_id, bal[0], bal[1], read=True)
                else:
                    db.set_balance(acc_id, acc["balance"], acc["balance_date"])
            except GoCardlessError as e:
                print(f"[warn] solde {acc_id}: {e}")
                db.set_balance(acc_id, acc["balance"], acc["balance_date"])
            results.append(SyncResult(acc_id, acc["bank"], len(new)))
        except GoCardlessError as e:
            results.append(SyncResult(acc_id, acc["bank"], 0, error=str(e)))
    return results, all_new


def _pick_balance(balances: list[dict]) -> tuple[float, str | None] | None:
    prefer = ("interimAvailable", "expected", "closingBooked", "interimBooked", "openingBooked")
    by_type = {b.get("balanceType"): b for b in balances}
    for t in prefer:
        if t in by_type:
            b = by_type[t]
            return float(b["balanceAmount"]["amount"]), b.get("referenceDate")
    if balances:
        b = balances[0]
        return float(b["balanceAmount"]["amount"]), b.get("referenceDate")
    return None


# ===================================================================== Enable Banking
def eb_start_link(db: Database, eb, bank: str, aspsp_name: str, country: str = "FR") -> str:
    """Démarre le consentement ; retourne l'URL à ouvrir. La réquisition est indexée par `state`."""
    auth = eb.start_auth(aspsp_name, country)
    db.save_requisition(auth["state"], bank, aspsp_name, "PENDING", auth["url"])
    return auth["url"]


def eb_finalize(db: Database, eb, code: str, state: str) -> tuple[str, int]:
    """Appelé par /callback : échange le code contre une session et enregistre les comptes."""
    row = db.conn.execute("SELECT * FROM requisitions WHERE id=?", (state,)).fetchone()
    if row is None:
        raise ValueError("state inconnu : relance `bankwatch link`")
    from .enablebanking import account_label, session_accounts

    sess = eb.create_session(code)
    session_id = sess.get("session_id") or sess.get("id") or ""
    # un compte sans uid n'est pas interrogeable (ex : produit non couvert par le consentement)
    accounts = [a for a in session_accounts(sess) if a.get("uid")]
    for acc in accounts:
        iban, name, currency = account_label(acc)
        db.upsert_account(id=acc["uid"], bank=row["bank"], institution_id=row["institution_id"],
                          requisition_id=session_id, iban=iban, name=name or row["bank"], currency=currency)
    db.save_requisition(state, row["bank"], row["institution_id"], "AUTHORIZED", session_id)
    return row["bank"], len(accounts)


def eb_sync_account(db: Database, eb, acc, today: date | None = None, lookback_days: int | None = None,
                    force_balance: bool = False) -> tuple[SyncResult, list[Tx]]:
    """Synchro d'UN compte Enable Banking dans le quota DSP2 : transactions à chaque passage,
    solde une fois par jour ; on s'arrête AVANT de dépasser les accès autorisés."""
    from .enablebanking import EnableBankingError, is_pending, pick_balance, to_raw
    from .importer import counterparty_from_label

    today = today or date.today()
    limit = settings.eb_daily_access_limit
    acc_id = acc["id"]
    used = accesses_today(db, acc_id, today)
    if used >= limit:
        return SyncResult(acc_id, acc["bank"], 0, skipped=f"quota du jour atteint ({used}/{limit})"), []
    if lookback_days is None:
        lookback = settings.gc_history_days if acc["last_sync"] is None else 30
    else:
        lookback = lookback_days
    try:
        _count_access(db, acc_id, today)
        txs = eb.transactions(acc_id, date_from=today - timedelta(days=lookback))
        cat = Categorizer()
        parsed = []
        for t in txs:
            raw = to_raw(t)
            if not raw["bookingDate"]:
                continue
            parsed.append(tx_from_gocardless(acc_id, raw, "pending" if is_pending(t) else "booked"))
        for tx in parsed:
            if not tx.counterparty and tx.label:  # BoursoBank : pas de nom de marchand fourni
                tx.counterparty = counterparty_from_label(tx.label)
            tx.category = cat.categorize(tx.counterparty, tx.label, tx.amount)
        new = db.upsert_transactions(parsed)
        # solde : une fois par jour (ou si jamais lu), et seulement s'il reste du quota
        bal_today = db.get_meta(f"bal:{acc_id}") == today.isoformat()
        if (not bal_today or acc["balance"] is None or force_balance) and accesses_today(db, acc_id, today) < limit:
            try:
                _count_access(db, acc_id, today)
                bal = pick_balance(eb.balances(acc_id))
                if bal:
                    db.set_balance(acc_id, bal[0], bal[1], read=True)
                    db.set_meta(f"bal:{acc_id}", today.isoformat())
                else:
                    db.set_balance(acc_id, acc["balance"], acc["balance_date"])
            except EnableBankingError as e:
                print(f"[warn] solde {acc_id}: {e}")
                db.set_balance(acc_id, acc["balance"], acc["balance_date"])
        else:
            db.set_balance(acc_id, acc["balance"], acc["balance_date"])
        return SyncResult(acc_id, acc["bank"], len(new)), new
    except EnableBankingError as e:
        msg = str(e)
        rl = "RATE_LIMIT" in msg or "HTTP 429" in msg
        if rl:  # la banque a compté plus que nous : on bloque le reste de la journée
            db.set_meta(_access_key(acc_id, today), str(limit))
        return SyncResult(acc_id, acc["bank"], 0, error=msg, rate_limited=rl), []


def eb_sync_all(db: Database, eb, lookback_days: int | None = None, today: date | None = None) -> tuple[list[SyncResult], list[Tx]]:
    results: list[SyncResult] = []
    all_new: list[Tx] = []
    for acc in db.accounts(include_ignored=False):
        if acc["institution_id"] in ("DEMO", "CSV") or not acc["id"]:
            continue
        r, new = eb_sync_account(db, eb, acc, today, lookback_days)
        results.append(r)
        all_new.extend(new)
    return results, all_new


def sync_status(db: Database, today: date | None = None) -> list[dict]:
    """État de synchro par compte : accès consommés / autorisés aujourd'hui, dernier passage."""
    today = today or date.today()
    limit = settings.eb_daily_access_limit
    out = []
    for a in db.accounts():
        if a["institution_id"] in ("DEMO", "CSV") or not a["id"]:
            continue
        used = accesses_today(db, a["id"], today)
        eff = db.effective_balance(a["id"])
        out.append({"account_id": a["id"], "bank": a["bank"], "name": a["name"], "ignored": bool(a["ignored"]),
                    "last_sync": a["last_sync"], "balance": a["balance"], "balance_date": a["balance_date"],
                    "balance_read_at": eff["read_at"], "balance_estimate": eff["estimate"], "balance_delta": eff["delta"],
                    "balance_read_today": db.get_meta(f"bal:{a['id']}") == today.isoformat(),
                    "used": used, "limit": limit, "remaining": max(0, limit - used)})
    return out


def provider_client():
    if settings.provider == "gocardless":
        return GoCardlessClient()
    from .enablebanking import EnableBankingClient

    return EnableBankingClient()


def sync_accounts(db: Database, lookback_days: int | None = None) -> tuple[list[SyncResult], list[Tx]]:
    client = provider_client()
    if settings.provider == "gocardless":
        return sync_all(db, client, lookback_days)
    return eb_sync_all(db, client, lookback_days)


def mark_internal_pairs(db: Database, since: date | None = None, window_days: int = 2) -> int:
    """Un crédit sur un compte et un débit du même montant sur un AUTRE de tes comptes, à ≤ 2 jours,
    tous deux libellés comme des virements → virement interne des deux côtés, quel que soit le libellé."""
    from .categorize import TRANSFER_RX

    since = since or (date.today() - timedelta(days=45))
    txs = [t for t in db.transactions(since=since) if TRANSFER_RX.search(f"{t.counterparty} {t.label}".lower())]
    credits = [t for t in txs if t.amount > 0]
    debits = [t for t in txs if t.amount < 0]
    used: set[str] = set()
    n = 0
    for c in credits:
        for d in debits:
            if d.id in used or d.account_id == c.account_id or abs(abs(d.amount) - c.amount) > 0.005:
                continue
            if abs((d.booking_date - c.booking_date).days) > window_days:
                continue
            used.add(d.id)
            for t in (c, d):
                if t.category != "epargne_virement":
                    db.set_category(t.id, "epargne_virement")
                    n += 1
            break
    db.commit()
    return n


def recategorize_all(db: Database) -> int:
    from .importer import counterparty_from_label

    cat = Categorizer()
    n = 0
    for tx in db.transactions(include_ignored=True):
        if not tx.counterparty and tx.label:
            tx.counterparty = counterparty_from_label(tx.label)
            db.conn.execute("UPDATE transactions SET counterparty=? WHERE id=?", (tx.counterparty, tx.id))
        c = cat.categorize(tx.counterparty, tx.label, tx.amount)
        if c != tx.category:
            db.set_category(tx.id, c)
            n += 1
    db.commit()
    return n + mark_internal_pairs(db, since=date.today() - timedelta(days=400))
