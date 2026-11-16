"""Prévision des prélèvements à venir et couverture par le solde (rules/forecast.json).

{
  "horizon_days": 10,     # fenêtre de prévision (utilisée seulement si "views" est absent)
  "views": [              # horizons multiples ; alerts:false = affichage seul (ni alerte, ni recommandation)
    {"name": "14j", "type": "rolling", "days": 14, "alerts": true},
    {"name": "mois", "type": "month_end", "alerts": true},        # d'aujourd'hui à la fin du mois inclus
    {"name": "mois+1", "type": "next_month", "alerts": false}     # du 1er au dernier jour du mois suivant
  ],
  "margin": 50,           # marge de sécurité à garder sur le compte
  "card_settlements": [   # cartes à débit différé réglées depuis un autre compte
    {"label": "Règlement American Express", "card_bank": "amex", "debit_bank": "boursobank",
     "match": ["american express"], "due_day": 30, "notice_days": 3}
  ]
}

Le montant attendu d'un règlement de carte = solde dû sur la carte (solde négatif).
Les autres prélèvements à venir viennent des récurrents détectés (cadence + dernier montant).
"""

from __future__ import annotations

import calendar
import json
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from .db import Database
from .rules import HIGH, LOW, MEDIUM, Alert, eur, recurring_merchants

FORECAST_FILE = Path("rules/forecast.json")
BANKS = {"boursobank": "BoursoBank", "sg": "Société Générale", "indy": "Indy", "amex": "American Express"}


VIEW_TYPES = ("rolling", "month_end", "next_month")


@dataclass
class View:
    """Un horizon de prévision. name vide = mode historique (horizon_days), sans suffixe dans les empreintes."""
    name: str
    type: str = "rolling"       # rolling (days jours) | month_end (jusqu'à la fin du mois) | next_month (mois suivant)
    days: int = 10
    alerts: bool = True         # False : affichée seulement, ne crée ni alerte ni recommandation


@dataclass
class ForecastConfig:
    horizon_days: int = 10
    margin: float = 50.0
    card_settlements: list[dict[str, Any]] = field(default_factory=list)
    views: list[View] = field(default_factory=list)

    def active_views(self) -> list[View]:
        """Les vues configurées, ou à défaut une vue unique « horizon_days » (rétrocompatible)."""
        return self.views or [View("", "rolling", self.horizon_days, True)]

    def alert_views(self) -> list[View]:
        return [v for v in self.active_views() if v.alerts]


def month_bounds(y: int, m: int) -> tuple[date, date]:
    return date(y, m, 1), date(y, m, calendar.monthrange(y, m)[1])


def view_window(view: View, today: date) -> tuple[date, date]:
    """(début, fin) inclus de la vue."""
    if view.type == "month_end":
        return today, month_bounds(today.year, today.month)[1]
    if view.type == "next_month":
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        return month_bounds(y, m)
    return today, today + timedelta(days=view.days)


def _parse_view(d: dict[str, Any]) -> View:
    t = d.get("type", "rolling")
    if t not in VIEW_TYPES:
        raise ValueError(f"forecast.json : type de vue inconnu « {t} » (attendu : {', '.join(VIEW_TYPES)})")
    return View(str(d.get("name") or t), t, int(d.get("days", 10)), bool(d.get("alerts", True)))


def load_config(path: Path | None = None) -> ForecastConfig:
    p = path or FORECAST_FILE
    if not p.exists():
        return ForecastConfig()
    d = json.loads(p.read_text(encoding="utf-8"))
    return ForecastConfig(horizon_days=int(d.get("horizon_days", 10)), margin=float(d.get("margin", 50)),
                          card_settlements=list(d.get("card_settlements", [])),
                          views=[_parse_view(v) for v in d.get("views", [])])


def next_due_day(today: date, day: int) -> date:
    """Prochaine occurrence du jour `day` (borné à la fin du mois), aujourd'hui inclus."""
    def clamp(y: int, m: int) -> date:
        return date(y, m, min(day, calendar.monthrange(y, m)[1]))
    d = clamp(today.year, today.month)
    if d < today:
        y, m = (today.year + 1, 1) if today.month == 12 else (today.year, today.month + 1)
        d = clamp(y, m)
    return d


@dataclass
class Upcoming:
    account_id: str
    bank: str
    due: date
    label: str
    amount: float          # montant positif attendu
    kind: str              # "card" | "recurring" | "installment" (échéance d'un paiement en plusieurs fois)
    estimate: bool = False # montant estimé (solde carte en cours, peut encore bouger)
    notice_days: int = 3
    beyond: bool = False   # au-delà de l'horizon : affiché « à prévoir », sans alerte


@dataclass
class Coverage:
    account_id: str
    bank: str
    name: str
    balance: float
    items: list[Upcoming]          # dans l'horizon : comptent pour le manque et les alertes
    margin: float
    later: list[Upcoming] = field(default_factory=list)  # règlements de carte au-delà de l'horizon : « à prévoir »

    @property
    def projected_shortfall(self) -> float:
        """Manque projeté si rien ne bouge d'ici le prochain règlement de carte hors fenêtre."""
        if not self.later:
            return 0.0
        return round(max(0.0, self.total + sum(i.amount for i in self.later) + self.margin - self.balance), 2)

    @property
    def total(self) -> float:
        return round(sum(i.amount for i in self.items), 2)

    @property
    def shortfall(self) -> float:
        """Ce qu'il manque pour couvrir les prélèvements + la marge (0 si tout est couvert ou rien à venir)."""
        if not self.items:
            return 0.0
        return round(max(0.0, self.total + self.margin - self.balance), 2)

    @property
    def ok(self) -> bool:
        return self.shortfall == 0

    @property
    def first_uncovered(self) -> Upcoming | None:
        """Premier prélèvement que le solde, marge déduite, ne couvre plus (dans l'ordre des échéances)."""
        left = self.balance - self.margin
        for i in sorted(self.items, key=lambda x: x.due):
            left -= i.amount
            if left < -0.005:
                return i
        return None


def upcoming(db: Database, today: date | None = None, cfg: ForecastConfig | None = None,
             view: View | None = None) -> list[Upcoming]:
    today = today or date.today()
    cfg = cfg or load_config()
    view = view or cfg.active_views()[0]
    start, horizon = view_window(view, today)  # start > today pour « mois suivant » : on cherche l'échéance à partir de là
    accounts = {a["id"]: a for a in db.accounts(include_ignored=False)}
    out: list[Upcoming] = []

    # 1) règlements de cartes à débit différé
    for cs in cfg.card_settlements:
        debit_accs = [a for a in accounts.values() if a["bank"] == cs["debit_bank"]]
        card_accs = [a for a in db.accounts() if a["bank"] == cs["card_bank"]]
        if not debit_accs or not card_accs:
            continue
        owed = sum(max(0.0, -(db.effective_balance(a["id"])["estimate"] or 0)) for a in card_accs)
        due = next_due_day(start, int(cs.get("due_day", 1)))
        if owed <= 0:
            continue
        # plusieurs comptes chez la banque débitrice : on prend celui qui a déjà payé la carte, sinon le 1er
        target = debit_accs[0]
        for a in debit_accs:
            hit = db.conn.execute(
                "SELECT 1 FROM transactions WHERE account_id=? AND amount<0 AND lower(counterparty||' '||label) LIKE ? LIMIT 1",
                (a["id"], f"%{cs['match'][0].lower()}%")).fetchone()
            if hit:
                target = a
                break
        out.append(Upcoming(target["id"], target["bank"], due, cs.get("label", "Règlement carte"), round(owed, 2),
                            "card", estimate=True, notice_days=int(cs.get("notice_days", 3)), beyond=due > horizon))

    # 2) prélèvements récurrents détectés
    settle_terms = [m.lower() for cs in cfg.card_settlements for m in cs.get("match", [])]
    txs = db.transactions(since=today - timedelta(days=400), debits_only=True)
    for _key, items in recurring_merchants(txs).items():
        last = items[-1]
        if last.account_id not in accounts or last.category == "epargne_virement":
            continue
        if any(t in f"{last.counterparty} {last.label}".lower() for t in settle_terms):
            continue  # déjà couvert par le règlement de carte
        # plusieurs lignes le même jour (ex. deux forfaits Bouygues) : un seul prélèvement, montant cumulé ;
        # les écarts nuls sont ignorés pour la cadence, sinon cadence 0 → boucle infinie
        gaps = [(b.booking_date - a.booking_date).days for a, b in zip(items, items[1:], strict=False)]
        gaps = [g for g in gaps if g > 0]
        cadence = max(1, sorted(gaps)[len(gaps) // 2]) if gaps else 30
        amount = sum(abs(t.amount) for t in items if t.booking_date == last.booking_date) or abs(last.amount)
        if 27 <= cadence <= 32:  # mensuel : même jour du mois que la dernière occurrence
            due = next_due_day(max(start, last.booking_date + timedelta(days=1)), last.booking_date.day)
        else:
            due = last.booking_date + timedelta(days=cadence)
            if due < start:
                due += timedelta(days=cadence * (((start - due).days // cadence) + 1))
        if due <= horizon:
            out.append(Upcoming(last.account_id, accounts[last.account_id]["bank"],
                                due, last.counterparty or last.label, round(amount, 2), "recurring"))
    # 3) échéances restantes des paiements en plusieurs fois (PayPal 4X), sur le compte bancaire qui les paie
    from .sync import wallet_installments_due

    for d in wallet_installments_due(db, today):
        if d["account_id"] in accounts and start <= d["due"] <= horizon:
            out.append(Upcoming(d["account_id"], accounts[d["account_id"]]["bank"], d["due"], d["label"],
                                d["amount"], "installment"))
    out.sort(key=lambda u: (u.due, -u.amount))
    return out


def coverage(db: Database, today: date | None = None, cfg: ForecastConfig | None = None,
             view: View | None = None) -> list[Coverage]:
    """Couverture par compte pour une vue (par défaut : la première vue à alertes, ou la première)."""
    cfg = cfg or load_config()
    view = view or (cfg.alert_views() or cfg.active_views())[0]
    ups = upcoming(db, today, cfg, view)
    out = []
    from .sync import wallet_banks

    skip = {cs["card_bank"] for cs in cfg.card_settlements} | wallet_banks()
    for a in db.accounts(include_ignored=False):
        if a["bank"] in skip:
            continue  # la carte elle-même, ou un portefeuille (PayPal) financé par la banque, n'a pas à être « couvert »
        mine = [u for u in ups if u.account_id == a["id"]]
        eff = db.effective_balance(a["id"])["estimate"]
        out.append(Coverage(a["id"], a["bank"], a["name"] or "", float(eff if eff is not None else (a["balance"] or 0)),
                            [u for u in mine if not u.beyond], cfg.margin, later=[u for u in mine if u.beyond]))
    return out


def detect_forecast(db: Database, today: date | None = None, cfg: ForecastConfig | None = None) -> list[Alert]:
    """Alertes : manque détecté (une fois par échéance), retour à la normale, et point J-3 pour les cartes.

    Avec plusieurs vues à alertes, un compte ne donne qu'UNE alerte de manque : celle de la vue la plus
    exigeante (à manque égal, la première déclarée), nommée dans le titre. L'empreinte ne dépend pas de la
    vue : la même alerte suit le compte quand la vue la plus exigeante change. La date du titre est celle
    du premier prélèvement réellement non couvert, pas celle de la première échéance."""
    today = today or date.today()
    cfg = cfg or load_config()
    alerts: list[Alert] = []
    per_view = [(v, {c.account_id: c for c in coverage(db, today, cfg, v)}) for v in cfg.alert_views()]
    order: list[str] = []
    for _, covs in per_view:
        order += [acc for acc in covs if acc not in order]
    seen_cards: set[str] = set()  # le point carte est identique d'une vue à l'autre : une seule fois
    for acc_id in order:
        mine = [(v, covs[acc_id]) for v, covs in per_view if acc_id in covs and covs[acc_id].items]
        if not mine:
            continue  # les règlements hors fenêtre (c.later) n'alertent pas encore
        view, c = max(mine, key=lambda vc: vc[1].shortfall)  # max() garde la première vue en cas d'égalité
        bank = BANKS.get(c.bank, c.bank)
        due = min(i.due for _, x in mine for i in x.items)  # échéance la plus proche : mois de l'empreinte
        lines = "\n".join(f"• {i.label} — {'≈ ' if i.estimate else ''}{eur(i.amount)} le {i.due:%d/%m}" for i in c.items)
        short_fp = f"forecast:short:{acc_id}:{due:%Y-%m}"
        if not c.ok:
            first = c.first_uncovered or min(c.items, key=lambda i: i.due)
            in_title = f" (vue {view.name})" if view.name else ""
            alerts.append(Alert(
                kind="forecast_short", severity=HIGH,
                title=f"{bank} : il manque {eur(c.shortfall)} avant le {first.due:%d/%m}{in_title}",
                message=f"Solde {eur(c.balance)} pour {eur(c.total)} de prélèvements à venir (+ {eur(c.margin)} de marge).\n{lines}\n"
                        f"→ Virer au moins **{eur(c.shortfall)}** sur {bank} avant le {first.due:%d/%m}"
                        f" (premier prélèvement non couvert : {eur(first.amount)}).",
                fingerprint=short_fp, amount=c.shortfall))
        else:
            was_short = db.conn.execute("SELECT 1 FROM alerts WHERE fingerprint=?", (short_fp,)).fetchone()
            if was_short:
                alerts.append(Alert(
                    kind="forecast_ok", severity=LOW, title=f"{bank} : prélèvements couverts ✓",
                    message=f"Solde {eur(c.balance)} pour {eur(c.total)} à venir.\n{lines}",
                    fingerprint=f"forecast:ok:{acc_id}:{due:%Y-%m}", amount=c.total))
        # point d'étape J-n pour les règlements de carte (vue la plus exigeante d'abord)
        for x in [c] + [x for _, x in mine if x is not c]:
            for i in x.items:
                fp = f"forecast:card:{acc_id}:{i.due:%Y-%m-%d}"
                if i.kind == "card" and today >= i.due - timedelta(days=i.notice_days) and fp not in seen_cards:
                    seen_cards.add(fp)
                    status = "✓ OK" if x.ok else f"⚠️ il manque {eur(x.shortfall)}"
                    alerts.append(Alert(
                        kind="forecast_card", severity=LOW if x.ok else MEDIUM,
                        title=f"{i.label} ≈ {eur(i.amount)} vers le {i.due:%d/%m} — {status}",
                        message=f"{bank} : solde {eur(x.balance)}, marge {eur(x.margin)}."
                                + ("" if x.ok else f"\n→ Virer **{eur(x.shortfall)}** sur {bank} avant le {i.due:%d/%m}.")
                                + "\n(montant = solde de la carte à ce jour, il peut encore évoluer)",
                        fingerprint=fp, amount=i.amount))
    return alerts


def coverage_dicts(db: Database, today: date | None = None, view: View | None = None,
                   cfg: ForecastConfig | None = None) -> list[dict[str, Any]]:
    return [{
        "account_id": c.account_id, "bank": c.bank, "name": c.name, "balance": c.balance,
        "total": c.total, "margin": c.margin, "shortfall": c.shortfall, "ok": c.ok,
        "projected_shortfall": c.projected_shortfall,
        "uncovered_from": c.first_uncovered.due.isoformat() if c.first_uncovered else None,
        "items": [{"due": i.due.isoformat(), "label": i.label, "amount": i.amount, "kind": i.kind, "estimate": i.estimate}
                  for i in c.items],
        "later": [{"due": i.due.isoformat(), "label": i.label, "amount": i.amount, "kind": i.kind, "estimate": i.estimate}
                  for i in c.later],
    } for c in coverage(db, today, cfg, view)]


def views_dicts(db: Database, today: date | None = None, cfg: ForecastConfig | None = None) -> list[dict[str, Any]]:
    """Toutes les vues, pour le dashboard : fenêtre, avec ou sans alerte, couverture par compte."""
    today = today or date.today()
    cfg = cfg or load_config()
    views = cfg.active_views()
    default = next((v for v in views if v.alerts), views[0])  # la vue affichée d'abord : la première à alertes
    out = []
    for v in views:
        start, end = view_window(v, today)
        out.append({"name": v.name or f"{v.days} j", "type": v.type, "start": start.isoformat(), "end": end.isoformat(),
                    "alerts": v.alerts, "default": v is default, "accounts": coverage_dicts(db, today, v, cfg)})
    return out
