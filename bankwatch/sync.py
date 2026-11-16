"""Synchronisation GoCardless -> SQLite."""

from __future__ import annotations

import json
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
        window_start = today - timedelta(days=lookback)
        txs = eb.transactions(acc_id, date_from=window_start)
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
        db.fold_drifting_pending(parsed)  # en attente sans référence dont la date a glissé : même opération
        new = db.upsert_transactions(parsed)
        if parsed:  # photo des « en attente » : ce que la banque ne renvoie plus n'est plus en attente
            db.purge_vanished_pending(acc_id, {t.id for t in parsed}, window_start, today)
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


WALLET_BANKS = {"paypal"}  # portefeuilles : leurs débits sont financés par un vrai compte bancaire


def wallet_banks() -> set[str]:
    """PayPal et tout compte marqué « wallet » dans rules/forecast.json (accounts.<banque>.wallet)."""
    from .forecast import FORECAST_FILE

    out = set(WALLET_BANKS)
    try:
        raw = json.loads(FORECAST_FILE.read_text(encoding="utf-8")) if FORECAST_FILE.exists() else {}
    except json.JSONDecodeError:
        raw = {}
    out |= {b for b, c in (raw.get("accounts") or {}).items() if isinstance(c, dict) and c.get("wallet")}
    return out


WALLET_DEBIT_LAG_DAYS = 5    # un prélèvement SEPA arrive jusqu'à 5 jours après l'achat PayPal (week-end compris)
WALLET_EARLY_DAYS = 3        # la ligne bancaire peut aussi précéder la ligne PayPal de quelques jours
WALLET_CREDIT_LAG_DAYS = 14  # un remboursement reversé sur le compte bancaire met jusqu'à deux semaines
WALLET_SPLITS = (3, 4)       # paiement en 3 ou 4 fois
WALLET_SPLIT_SPAN_DAYS = 120  # durée maximale d'un échéancier


def _is_wallet_loan(t: Tx) -> bool:
    """Échéance d'un paiement en plusieurs fois (PayPal « 4X ») : code PAYMENT/LOAN chez Enable Banking,
    créancier « …repayment@paypal.com »."""
    eb = (t.raw or {}).get("_eb") or {}
    if ((eb.get("bank_transaction_code") or {}).get("sub_code") or "").upper() == "LOAN":
        return True
    mail = ((eb.get("creditor") or {}).get("contact_details") or {}).get("email_address") or ""
    return "repayment@" in mail.lower()


@dataclass
class WalletPlan:
    """Achat payé en plusieurs fois depuis un portefeuille : l'achat, ses échéances déjà prélevées, ce qu'il reste."""
    purchase: Tx
    splits: int        # 3 ou 4
    installment: float  # montant d'une échéance
    category: str      # catégorie naturelle de l'achat
    paid: list[Tx]     # échéances déjà prélevées (lignes LOAN)

    @property
    def remaining(self) -> float:
        return round(abs(self.purchase.amount) - sum(abs(t.amount) for t in self.paid), 2)


def wallet_plans(wallet_txs: list[Tx]) -> list[WalletPlan]:
    """Reconstitue les échéanciers : un achat se reconnaît à sa première échéance, prélevée le même jour
    (montant / 3 ou / 4) ; les échéances suivantes s'y rattachent, y compris plusieurs soldées d'un coup."""
    loans = sorted((w for w in wallet_txs if w.amount < 0 and _is_wallet_loan(w)), key=lambda t: t.booking_date)
    if not loans:
        return []
    cat = Categorizer()
    taken: set[str] = set()
    plans: list[WalletPlan] = []
    for p in sorted(wallet_txs, key=lambda t: t.booking_date):
        if p.amount >= 0 or _is_wallet_loan(p):
            continue
        for x in loans:
            if x.id in taken or x.account_id != p.account_id or x.booking_date != p.booking_date:
                continue
            k = next((k for k in WALLET_SPLITS if abs(abs(p.amount) / k - abs(x.amount)) <= 0.02), None)
            if k:
                taken.add(x.id)
                plans.append(WalletPlan(p, k, abs(x.amount), cat.categorize(p.counterparty, p.label, p.amount), [x]))
                break
    for x in loans:
        if x.id in taken:
            continue
        for plan in plans:
            days = (x.booking_date - plan.purchase.booking_date).days
            n = round(abs(x.amount) / plan.installment)
            if (x.account_id == plan.purchase.account_id and 0 <= days <= WALLET_SPLIT_SPAN_DAYS and n >= 1
                    and abs(abs(x.amount) - n * plan.installment) <= 0.02 * n
                    and abs(x.amount) <= plan.remaining + 0.05):
                plan.paid.append(x)
                taken.add(x.id)
                break
    return plans


WALLET_LABELS = {"paypal": "PayPal"}
WALLET_OVERDUE_DAYS = 7  # une échéance en retard de plus d'une semaine n'est plus attendue


def wallet_installments_due(db: Database, today: date | None = None) -> list[dict]:
    """Échéances restant à prélever des paiements en plusieurs fois, sur le compte bancaire qui paie l'échéancier
    (celui où une échéance précédente est passée). Une échéance tombe tous les 30 jours à partir de l'achat.
    Une échéance déjà notée côté portefeuille mais pas encore passée en banque reste attendue."""
    today = today or date.today()
    wallets = {a["id"]: a["bank"] for a in db.accounts(include_ignored=False) if a["bank"] in wallet_banks()}
    if not wallets:
        return []
    hist = db.transactions(since=today - timedelta(days=WALLET_SPLIT_SPAN_DAYS + 30))
    names = set(wallets.values())
    bank_debits = [t for t in hist if t.account_id not in wallets and t.amount < 0
                   and any(w in f"{t.counterparty} {t.label}".lower() for w in names)]
    out: list[dict] = []
    for plan in wallet_plans([t for t in hist if t.account_id in wallets]):
        payer, not_yet = None, []
        for x in sorted(plan.paid, key=lambda t: t.booking_date, reverse=True):
            twin = next((b for b in bank_debits if abs(abs(b.amount) - abs(x.amount)) <= 0.005
                         and -WALLET_EARLY_DAYS <= (b.booking_date - x.booking_date).days <= WALLET_DEBIT_LAG_DAYS), None)
            if twin:
                payer = payer or twin.account_id
            elif (today - x.booking_date).days <= WALLET_DEBIT_LAG_DAYS:
                not_yet.append(x)
        if not payer:
            continue  # échéancier payé depuis un compte que BankWatch ne suit pas
        wallet = wallets[plan.purchase.account_id]
        name = f"{WALLET_LABELS.get(wallet, wallet.capitalize())} {plan.splits}X · {plan.purchase.counterparty or plan.purchase.label}"
        for x in not_yet:
            out.append({"account_id": payer, "due": today, "amount": round(abs(x.amount), 2), "label": name})
        left = min(plan.splits - 1, round(plan.remaining / plan.installment)) if plan.remaining > 0.05 else 0
        for i in range(plan.splits - left, plan.splits):
            due = plan.purchase.booking_date + timedelta(days=30 * i)
            last = i == plan.splits - 1
            amount = round(plan.remaining - plan.installment * (left - 1), 2) if last else plan.installment
            if due < today:
                if (today - due).days > WALLET_OVERDUE_DAYS:
                    continue
                due = today
            out.append({"account_id": payer, "due": due, "amount": round(amount, 2),
                        "label": f"{name} ({i + 1}/{plan.splits})"})
    return out


def mark_wallet_pairs(db: Database, since: date | None = None, window_days: int = WALLET_DEBIT_LAG_DAYS) -> int:
    """Un achat PayPal existe deux fois : côté PayPal (avec le marchand) et côté banque (« PRÉLÈVEMENT PayPal »
    ou « CARTE PAYPAL *X »). La ligne bancaire de même montant, de 3 jours avant à `window_days` jours après,
    devient un virement interne vers le portefeuille ; la ligne PayPal garde la dépense et sa catégorie. Si PayPal
    remonte aussi l'alimentation (crédit du même montant), elle est interne elle aussi. Sans jumeau côté PayPal,
    la ligne banque reste comptée.

    Remboursement reversé à la banque (« VIR PayPal » en crédit) : le crédit PayPal de même montant, jusqu'à
    14 jours plus tôt, garde le remboursement ; la ligne bancaire devient interne.

    Paiement en plusieurs fois (4X) : PayPal remonte l'achat entier ET chaque échéance (code LOAN). Ce sont les
    échéances qui sortent de la banque : l'achat devient interne (financé par PayPal) et chaque échéance porte la
    dépense, avec la catégorie de l'achat. L'achat se reconnaît à sa première échéance, prélevée le même jour
    (montant / 3 ou / 4)."""
    wallets = {a["id"]: a["bank"] for a in db.accounts(include_ignored=False) if a["bank"] in wallet_banks()}
    if not wallets:
        return 0
    since = since or (date.today() - timedelta(days=45))
    hist = db.transactions(since=since - timedelta(days=WALLET_SPLIT_SPAN_DAYS))
    txs = [t for t in hist if t.booking_date >= since]
    wallet_txs = [t for t in hist if t.account_id in wallets]
    names = set(wallets.values())
    n = 0

    def set_cat(t: Tx, category: str) -> None:
        nonlocal n
        if t.category != category:
            db.set_category(t.id, category)
            t.category = category
            n += 1

    # 1) paiements en plusieurs fois
    plans = wallet_plans(wallet_txs)
    for plan in plans:
        set_cat(plan.purchase, "epargne_virement")
        if plan.category != "epargne_virement":
            for x in plan.paid:
                set_cat(x, plan.category)
    financed_ids = {plan.purchase.id for plan in plans}

    # 2) débits bancaires vers le portefeuille
    def mentions(t: Tx) -> bool:
        return any(w in f"{t.counterparty} {t.label}".lower() for w in names)

    bank_debits = [t for t in txs if t.account_id not in wallets and t.amount < 0 and mentions(t)]
    used: set[str] = set()
    for b in sorted(bank_debits, key=lambda t: t.booking_date):
        cands = [w for w in wallet_txs if w.id not in used and w.id not in financed_ids
                 and abs(abs(w.amount) - abs(b.amount)) <= 0.005
                 and -WALLET_EARLY_DAYS <= (b.booking_date - w.booking_date).days <= window_days]
        if not cands:
            continue
        # alimentation (crédit) d'abord, sinon l'achat lui-même ; au plus proche en date
        cands.sort(key=lambda w: (w.amount < 0, abs((w.booking_date - b.booking_date).days)))
        w = cands[0]
        used.add(w.id)
        set_cat(b, "epargne_virement")
        if w.amount > 0:
            set_cat(w, "epargne_virement")

    # 3) remboursements et argent reçu reversés sur le compte bancaire
    bank_credits = [t for t in txs if t.account_id not in wallets and t.amount > 0 and mentions(t)]
    for b in sorted(bank_credits, key=lambda t: t.booking_date):
        cands = [w for w in wallet_txs if w.id not in used and w.amount > 0 and abs(w.amount - b.amount) <= 0.005
                 and -1 <= (b.booking_date - w.booking_date).days <= WALLET_CREDIT_LAG_DAYS]
        if not cands:
            continue
        cands.sort(key=lambda w: abs((w.booking_date - b.booking_date).days))
        used.add(cands[0].id)
        set_cat(b, "epargne_virement")
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
    since = date.today() - timedelta(days=400)
    return n + mark_internal_pairs(db, since=since) + mark_wallet_pairs(db, since=since)
