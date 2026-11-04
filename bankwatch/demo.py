"""Jeu de données synthétiques : 3 banques, 6 mois, anomalies plantées, pour tester sans GoCardless."""

from __future__ import annotations

import random
from datetime import date, timedelta

from .categorize import Categorizer
from .db import Database, Tx
from .pipeline import run_checks

ACCOUNTS = [
    ("acc-bourso", "boursobank", "Compte courant", "FR76 0000 0000 0001"),
    ("acc-sg", "sg", "Compte Société Générale", "FR76 0000 0000 0002"),
    ("acc-indy", "indy", "Compte pro Indy", "FR76 0000 0000 0003"),
]

RECURRING = [  # (compte, marchand, libellé, montant, jour)
    ("acc-bourso", "Netflix", "PRLV SEPA NETFLIX INTERNATIONAL", -13.49, 4),
    ("acc-bourso", "Spotify", "CB SPOTIFY AB", -10.99, 12),
    ("acc-bourso", "Free Mobile", "PRLV SEPA FREE MOBILE", -19.99, 8),
    ("acc-sg", "Loyer SCI Lumière", "VIR LOYER APPARTEMENT", -1150.00, 2),
    ("acc-sg", "EDF", "PRLV SEPA EDF CLIENTS PARTICULIERS", -78.00, 15),
    ("acc-sg", "Basic-Fit", "PRLV SEPA BASIC FIT FRANCE", -29.99, 20),
    ("acc-indy", "OVH", "PRLV SEPA OVH SAS FACTURE", -42.00, 6),
    ("acc-indy", "Amazon Web Services", "AMAZON WEB SERVICES EMEA", -37.50, 3),
    ("acc-indy", "GitHub", "CB GITHUB INC", -9.00, 10),
    ("acc-indy", "Anthropic", "CB ANTHROPIC PBC", -20.00, 17),
]

VARIABLE = [  # (compte, marchand, libellé, min, max, fréquence/mois)
    ("acc-bourso", "Carrefour Market", "CB CARREFOUR MARKET IVRY", 25, 90, 6),
    ("acc-bourso", "Uber Eats", "CB UBER EATS", 18, 38, 3),
    ("acc-bourso", "SNCF", "CB SNCF CONNECT", 12, 60, 2),
    ("acc-bourso", "Amazon", "CB AMAZON EU SARL", 15, 120, 2),
    ("acc-bourso", "Boulangerie", "CB BOULANGERIE DU MARCHE", 3, 9, 8),
    ("acc-bourso", "Pharmacie", "CB PHARMACIE CENTRALE", 8, 35, 1),
    ("acc-sg", "Total Energies", "CB TOTALENERGIES RELAIS", 55, 80, 2),
    ("acc-sg", "Leroy Merlin", "CB LEROY MERLIN", 20, 150, 1),
    ("acc-indy", "Fnac", "CB FNAC", 30, 200, 1),
]


def _rng_amount(rng: random.Random, lo: float, hi: float) -> float:
    return -round(rng.uniform(lo, hi), 2)


def generate(today: date | None = None, seed: int = 42) -> list[Tx]:
    rng = random.Random(seed)
    today = today or date.today()
    start = today - timedelta(days=180)
    cat = Categorizer()
    txs: list[Tx] = []
    n = 0

    def add(acc: str, who: str, label: str, amount: float, d: date, tid: str | None = None) -> Tx | None:
        nonlocal n
        if d > today:
            return None
        n += 1
        tx = Tx(id=tid or f"{acc}:demo{n:05d}", account_id=acc, booking_date=d, amount=amount,
                counterparty=who, label=label, category=cat.categorize(who, label, amount))
        txs.append(tx)
        return tx

    # Revenus
    d = start.replace(day=1)
    while d <= today:
        if d.day == 1:
            add("acc-indy", "Client Mission", "VIR FACTURE MISSION", 6200.00, d + timedelta(days=4))
            add("acc-bourso", "Karakuri Studio", "VIR REMUNERATION", 3200.00, d + timedelta(days=6))
        d += timedelta(days=1)

    # Récurrents
    for acc, who, label, amount, day in RECURRING:
        d = start.replace(day=1)
        while d <= today:
            dd = d.replace(day=day)
            if start <= dd <= today:
                add(acc, who, label, amount, dd)
            d = (d.replace(day=28) + timedelta(days=4)).replace(day=1)

    # Variables
    for acc, who, label, lo, hi, freq in VARIABLE:
        d = start
        while d <= today:
            for _ in range(freq):
                dd = d + timedelta(days=rng.randint(0, 29))
                if dd <= today:
                    add(acc, who, label, _rng_amount(rng, lo, hi), dd)
            d += timedelta(days=30)

    # ----- anomalies plantées (dans les 10 derniers jours) -----
    # 1. doublon Carrefour même compte
    d1 = today - timedelta(days=2)
    add("acc-bourso", "Carrefour Market", "CB CARREFOUR MARKET IVRY", -64.30, d1)
    add("acc-bourso", "Carrefour Market", "CB CARREFOUR MARKET IVRY", -64.30, d1)
    # 2. doublon Netflix (prélevé deux fois, même jour)
    add("acc-bourso", "Netflix", "PRLV SEPA NETFLIX INTERNATIONAL", -13.49, today - timedelta(days=1))
    # 3. EDF qui augmente de 35 %
    add("acc-sg", "EDF", "PRLV SEPA EDF CLIENTS PARTICULIERS", -105.30, today - timedelta(days=3))
    # 4. Nouveau marchand gros montant
    add("acc-bourso", "Apple Store", "CB APPLE STORE OPERA", -1299.00, today - timedelta(days=4))
    # 5. Montant inhabituel chez Uber Eats
    add("acc-bourso", "Uber Eats", "CB UBER EATS", -142.00, today - timedelta(days=5))
    # 6. Basic-Fit passé deux fois à 12 jours d'écart
    add("acc-sg", "Basic-Fit", "PRLV SEPA BASIC FIT FRANCE", -29.99, today - timedelta(days=6))
    # 7. Surconsommation courses ce mois : rafale de courses
    for i in range(5):
        add("acc-bourso", "Carrefour Market", "CB CARREFOUR MARKET IVRY", _rng_amount(rng, 70, 95),
            today.replace(day=1) + timedelta(days=min(i * 3, max(today.day - 1, 0))))

    txs.sort(key=lambda t: t.booking_date)
    return txs


def load_demo(db: Database, reset: bool = True, notify: bool = False) -> tuple[int, list]:
    if reset:
        for table in ("transactions", "alerts", "accounts", "requisitions", "meta"):
            db.conn.execute(f"DELETE FROM {table}")
        db.conn.commit()
    for acc_id, bank, name, iban in ACCOUNTS:
        db.upsert_account(id=acc_id, bank=bank, institution_id="DEMO", requisition_id="demo",
                          iban=iban, name=name, currency="EUR")
    txs = generate()
    new = db.upsert_transactions(txs)
    for acc_id, *_ in ACCOUNTS:
        bal = sum(t.amount for t in txs if t.account_id == acc_id) + (9000 if acc_id == "acc-sg" else 2500)
        db.set_balance(acc_id, round(bal, 2), date.today().isoformat())
    # baseline silencieuse sur l'historique, puis contrôles « réels » sur les 10 derniers jours
    db.set_meta("baseline_done", date.today().isoformat())
    alerts = run_checks(db, new, notify=notify)
    if not notify:  # ne jamais envoyer plus tard des alertes fictives
        db.mark_sent(a["id"] for a in db.unsent_alerts())
    return len(new), alerts
