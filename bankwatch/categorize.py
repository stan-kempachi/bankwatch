"""Catégorisation par mots-clés (fichier rules/categories.json surchargeable)."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

DEFAULT_RULES: dict[str, list[str]] = {
    "courses": ["carrefour", "leclerc", "auchan", "monoprix", "franprix", "lidl", "intermarche",
                "casino", "picard", "grand frais", "biocoop", "naturalia", "g20", "supermarch", "superette",
                "boucherie", "primeur", "epicerie", "marche "],
    "restos": ["uber eats", "uber one", "deliveroo", "eat salad", "trattoria", "horeto", "mcdonald", "burger", "kfc", "sushi", "pizza", "restaurant",
               "brasserie", "bistro", "cafe", "starbucks", "boulangerie", "kebab", "just eat"],
    "transport": ["sncf", "ratp", "navigo", "uber", "bolt", "effia", "viparis", "q-park", "saemes", "heetch", "total", "esso", "bp ", "shell",
                  "autoroute", "vinci", "sanef", "aprr", "parking", "indigo", "blablacar", "lime"],
    "logement": ["loyer", "edf", "engie", "totalenergies", "veolia", "suez", "free", "orange", "sfr",
                 "bouygues", "sosh", "red by", "assurance habitation", "syndic"],
    "abonnements": ["netflix", "spotify", "disney", "amazon prime", "canal", "youtube", "apple.com",
                    "google storage", "icloud", "adobe", "notion", "github", "openai", "anthropic",
                    "ovh", "scaleway", "hetzner", "aws", "amazon web services", "cloudflare",
                    "microsoft", "dropbox", "basic-fit", "fitness", "salle de sport"],
    "sante": ["pharmacie", "docteur", "dr ", "medecin", "mutuelle", "cpam", "laboratoire", "dentiste",
              "opticien", "hopital"],
    "shopping": ["amazon", "fnac", "darty", "boulanger", "cdiscount", "zara", "h&m", "uniqlo",
                 "decathlon", "ikea", "leroy merlin", "castorama", "apple store", "aliexpress"],
    "banque_frais": ["frais", "commission", "cotisation", "agios", "interets debiteurs"],
    "impots_taxes": ["dgfip", "direction generale des finance", "impot", "urssaf", "tresor public", "amende", "antai", "sie paris"],
    "voyages": ["booking.com", "airbnb", "hotel", "ryanair", "easyjet", "air france", "transavia", "vueling", "hostel"],
    "epargne_virement": ["livret", "pea", "assurance vie", "bourse", "epargne"],
    "revenus": ["salaire", "paie", "remuneration", "facture", "honoraires", "malt", "upwork",
                "caf", "remboursement"],
    "retrait": ["retrait", "dab", "atm"],
}


CUSTOM_FILE = Path("rules/categories.json")


def load_custom(path: Path | None = None) -> dict[str, list[str]]:
    p = path or CUSTOM_FILE
    if not p.exists():
        return {}
    data = json.loads(p.read_text(encoding="utf-8"))
    # clés de commentaire (« _comment ») et valeurs non-liste ignorées
    return {k: [str(x) for x in v] for k, v in data.items() if not k.startswith("_") and isinstance(v, list)}


def load_rules(path: Path | None = None) -> dict[str, list[str]]:
    """Règles par défaut (ordre conservé) complétées par les mots-clés perso."""
    rules = {k: list(v) for k, v in DEFAULT_RULES.items()}
    for cat, kws in load_custom(path).items():
        rules.setdefault(cat, []).extend(k for k in kws if k not in rules.get(cat, []))
    return rules


def _pattern(keyword: str) -> str:
    """Un espace dans un mot-clé tolère espaces, astérisques, points et tirets dans le libellé
    (« uber eats » reconnaît « UBER   *EATS », « UBER*EATS », « Uber-Eats »)."""
    parts = [re.escape(p) for p in keyword.lower().split()]
    return r"[\s*.\-_]*".join(parts)


TRANSFER_RX = re.compile(r"\bvir(ement)?\b|\binstantan|\btransfer")
GENERIC_WORDS = {"vir", "virement", "en", "ligne", "ope", "de", "compte", "motif", "ref", "remise", "instantane",
                 "recu", "emis", "sepa", "inst", "cb", "carte", "date", "re", "le", "la", "du", "des", "et"}


def counterparty_is_generic(counterparty: str | None) -> bool:
    """Vrai si la contrepartie ne contient aucun mot porteur (« EN LIGNE OPE: DE: Compte MOTIF »)."""
    return not (_tokens(counterparty or "") - GENERIC_WORDS)


def _tokens(text: str) -> set[str]:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return set(re.findall(r"[a-z]+", t))


class Categorizer:
    def __init__(self, rules: dict[str, list[str]] | None = None, owner_names: list[str] | None = None,
                 custom: dict[str, list[str]] | None = None, custom_path: Path | None = None) -> None:
        from .config import settings

        self.rules = rules or load_rules(custom_path)
        names = owner_names if owner_names is not None else settings.owner_list
        if isinstance(names, str):  # garde-fou : jamais itérer une chaîne lettre par lettre
            names = [n for n in names.split(",")]
        self._owners = [t for t in (_tokens(n) for n in names) if len(t) >= 2]  # prénom + nom minimum
        custom = load_custom(custom_path) if custom is None else custom
        def compile_(kws: list[str]) -> re.Pattern[str]:
            return re.compile("|".join(_pattern(k) for k in kws))

        # 1) mots-clés perso (prioritaires), 2) règles par défaut dans leur ordre
        self._compiled = [(cat, compile_(kws)) for cat, kws in custom.items() if kws]
        self._compiled += [(cat, compile_(kws)) for cat, kws in self.rules.items() if kws]

    def categorize(self, counterparty: str, label: str, amount: float) -> str:
        text = f"{counterparty} {label}".lower()
        if self._owners:
            # virement entre tes propres comptes : c'est la CONTREPARTIE qui porte ton nom.
            # Si la banque ne la fournit pas (libellé seul), on regarde le libellé ; sinon on ignore
            # le texte libre, où ton nom apparaît aussi sur les factures payées par tes clients.
            toks = _tokens(counterparty) if not counterparty_is_generic(counterparty) else _tokens(label)
            if any(o <= toks for o in self._owners):
                return "epargne_virement"
        compiled = self._compiled
        if amount > 0:  # pour un crédit, « revenus » passe avant « virement »
            compiled = sorted(compiled, key=lambda cr: cr[0] != "revenus")
        for cat, rx in compiled:
            if rx.search(text):
                if cat == "revenus" and amount < 0:
                    continue
                return cat
        # « virement » / « vir » sans mot-clé plus précis : interne seulement si la contrepartie est inconnue ;
        # un virement reçu d'un tiers nommé est une entrée, un virement émis vers un tiers une dépense.
        if TRANSFER_RX.search(text) and counterparty_is_generic(counterparty):
            return "epargne_virement"
        if amount > 0:
            return "revenus"
        return "autre"
