# BankWatch

Surveillance automatique de comptes bancaires (perso et pro) via l'open banking DSP2 — testé avec BoursoBank, Société Générale, Swan (Indy) et American Express, fonctionne avec toute banque du catalogue Enable Banking.
Les transactions sont récupérées via **Enable Banking** (open banking DSP2, mode « restricted » gratuit pour ses propres comptes, **lecture seule**), ou importées depuis des relevés CSV/OFX, et stockées dans SQLite. Des règles de détection tournent dessus, les alertes partent sur **Discord** et un **dashboard web** affiche les graphiques.

```
Enable Banking / relevés CSV ──► sync ──► SQLite ──► règles ──► alertes Discord
                                     │
                                     └──► dashboard FastAPI + Chart.js (:8787)
```

## Ce qui est détecté

| Règle | Déclencheur | Gravité |
|---|---|---|
| **Doublon** | même marchand, même montant, ≤ 3 j d'écart | 🚨 même compte / ⚠️ deux comptes |
| **Prélèvement modifié** | un récurrent varie de plus de 10 % par rapport à sa médiane | ⚠️ hausse / ℹ️ baisse |
| **Prélèvement passé deux fois** | un récurrent passe deux fois dans le mois, ou plus tôt que sa cadence habituelle | ⚠️ |
| **Montant inhabituel** | marchand connu, z-score ≥ 3 et ≥ 2× la moyenne | ⚠️ |
| **Nouveau marchand** | première fois vu, montant ≥ 150 € | ⚠️ |
| **Gros débit** | montant ≥ 500 € | ℹ️ |
| **Surdépense globale** | mois en cours ≥ +30 % par rapport à la moyenne des 3 mois précédents, comparés à la même date | 🚨 |
| **Surconsommation catégorie** | même logique, par catégorie (≥ 100 €) | ⚠️ |
| **Budget** | budget mensuel défini dans `BW_BUDGETS` : avertissement à 80 %, alerte au dépassement | ℹ️ / 🚨 |

Une alerte n'est **jamais envoyée deux fois** : chacune porte une empreinte stable en base. À la **première** exécution, tout l'historique est analysé en silence, pour ne pas inonder Discord de 6 mois d'alertes.

Les récurrents sont **détectés automatiquement** : cadence d'environ 30 jours et montant stable (variation ≤ 25 %), ou catégorie abonnement/logement. Il n'y a rien à déclarer.

## Installation

### 1. Créer les accès

1. **Enable Banking** (GoCardless n'accepte plus d'inscriptions) :
   - Va sur <https://enablebanking.com/cp/applications> (connexion par lien magique), puis *Register a new application*, environnement **Production**.
   - Redirect URL : `https://bankwatch.example.com/callback`, et garde « Generate in the browser » pour la clé : un fichier `<application-id>.pem` est téléchargé.
   - Clique sur **Activate by linking accounts** (pas « Request activation ») et lie tes comptes. L'application passe en *Actif / Limité*.
   - Dépose le `.pem` via **https://bankwatch.example.com/setup** (page protégée, fichier écrit en 600), puis mets `BW_EB_APP_ID` dans le `.env`.
2. **Discord** : par défaut, BankWatch publie **avec le bot existant**, comme les autres projets du Pi. Renseigne `BW_DISCORD_CHANNEL_ID` et `BW_DISCORD_TOKEN_FILE` (le `.env` partagé qui contient `DISCORD_BOT_TOKEN`). Un webhook (`BW_DISCORD_WEBHOOK_URL`) marche aussi, en alternative. Vérifie avec `bankwatch notify-test`.

### 2. Autoriser chaque banque côté API

Lier un compte dans le Control Panel active l'application, mais BankWatch doit aussi obtenir sa propre autorisation, une fois par banque :

```bash
bankwatch institutions bourso                 # nom exact, ex. « Boursorama »
bankwatch link boursobank "Boursorama"        # affiche un lien : valide sur l'appli de la banque
bankwatch link sg "Société Générale"
bankwatch accounts                            # vérifie que les comptes sont arrivés
bankwatch sync                                # 1ère sync : historique analysé en silence
```

Après validation, la banque te renvoie sur `/callback`, qui enregistre les comptes. Pour un compte pro, ajoute `--business`.

**Banque non couverte** (ex. Indy/Swan) : exporte un relevé CSV ou OFX depuis l'espace client, puis lance `bankwatch import releve.csv --bank indy`. Les colonnes sont détectées automatiquement et réimporter ne crée pas de doublon.

### 3. Déploiement (Raspberry Pi / VPS, systemd)

```
reverse proxy (HTTPS) ─► bankwatch.service (uvicorn 127.0.0.1:8117)
                         bankwatch-run.timer (07h20, 13h20, 20h20)
```

- Code dans `/home/pi/apps/bankwatch`, venv `.venv/`, base `data/bankwatch.db`, secrets `.env` (chmod 600, hors git).
- Unités systemd : `deploy/*.service|timer`, à copier dans `/etc/systemd/system/` (adapter utilisateur et chemins).
- Le dashboard n'écoute qu'en local : exposez-le derrière un reverse proxy HTTPS (Caddy, nginx, Cloudflare Tunnel…), jamais en direct.
- Mettre à jour après un push : `deploy/deploy.sh` · Logs : `journalctl -u bankwatch -u bankwatch-run -f`

### 3 bis. Alternative Docker

```bash
docker compose up -d --build
```

- `worker` : `bankwatch run` 3 fois par jour (sync, contrôles, alertes, plus le **rapport hebdo le lundi**).
- `dashboard` : <http://localhost:8787>, exposé sur **127.0.0.1 uniquement**. Pour y accéder à distance, passe par Tailscale ou un reverse proxy **authentifié**. Ce sont tes données bancaires : ne publie jamais ce port en clair.

## Commandes

| Commande | Rôle |
|---|---|
| `bankwatch run` | tâche planifiée : sync, contrôles et rapport hebdo si c'est le jour |
| `bankwatch sync [--no-notify]` | synchronise puis contrôle |
| `bankwatch check` | relance les règles sans resynchroniser |
| `bankwatch report --days 30 [--dry-run]` | rapport Discord sur N jours |
| `bankwatch serve` | dashboard |
| `bankwatch recategorize` | réapplique les catégories après une modif de `rules/categories.json` |
| `bankwatch demo` | charge 6 mois de données fictives avec anomalies, pour tester sans banque |
| `bankwatch user add <nom> [--role viewer\|admin]` | crée un accès au dashboard, mot de passe affiché une fois (`user list`, `user remove`) |
| `bankwatch notify-test` | message de test sur Discord |
| `bankwatch ignore <id/nom> [--undo]` | exclut (ou réintègre) un compte de l'analyse |
| `bankwatch reset --yes` | vide transactions, alertes et comptes (à faire après la démo) |

## Suivi d'un marchand (`rules/watch.json`)

Pour surveiller de près un fournisseur (un SaaS facturé à l'usage, un abonnement qui ne doit plus générer de recharges…) :

```json
{
  "exemple_saas": {
    "label": "Mon SaaS (exemple)",
    "match": [
      "nom-du-marchand"
    ],
    "since": "2026-01-01",
    "small_charge_max": 30,
    "subscription_match": [
      "sub"
    ],
    "expected_amount": 20,
    "monthly_cap": 60,
    "note": "Abonnement seul attendu : toute recharge est signalée."
  }
}
```

- Un débit ≤ `small_charge_max` qui n'est pas l'abonnement (`subscription_match`) compte comme **recharge**. Toute recharge à partir de `since` déclenche une alerte Discord.
- Le total du mois est comparé à `monthly_cap` (avertissement à 80 %, alerte au dépassement).
- Le rapport hebdo et le dashboard affichent le mois en cours, le mois précédent, l'historique abonnement / recharges sur 6 mois et le nombre de recharges depuis `since`.
- `bankwatch watch` affiche le même suivi en ligne de commande.

## Prélèvements à venir et couverture (`rules/forecast.json`)

BankWatch ne peut **pas** faire de virement : l'accès open banking est en lecture seule. Il prévient à la place, avec le montant exact à virer.

- Les **récurrents détectés** (loyer, abonnements…) sont projetés à leur prochaine échéance (même jour du mois).
- Les **cartes à débit différé** (`card_settlements`) : le montant attendu = solde dû sur la carte, prélevé sur `debit_bank` autour de `due_day`.
- Pour chaque compte, sur `horizon_days` : total à venir + `margin` comparé au solde.
  - **Manque** → alerte Discord immédiate avec le montant à virer (une fois par échéance).
  - Retour à la normale après un virement → « couvert ✓ ».
  - Règlement de carte : point d'étape à J-`notice_days` quoi qu'il arrive (OK ou manque).
- Dashboard : carte « Prélèvements à venir » ; CLI : `bankwatch forecast` ; rapport hebdo : section « 📅 ».

## Découverts (`rules/overdraft.json`)

L'historique des soldes est reconstitué depuis les opérations (solde à J = solde actuel − opérations postérieures), ce qui donne la date de passage en négatif. Dès qu'un compte est à découvert : alerte Discord avec le montant à virer, puis rappel tous les `remind_every_days` jours tant que ça dure ; au-delà de `grace_hours` (24 h), les frais sont signalés comme probables. Les cartes à débit différé (`exclude_banks`) sont ignorées. Carte « Découverts » sur le dashboard avec la courbe des 30 derniers jours, **l'estimation des frais** (agios = solde négatif × jours × taux ; commissions d'intervention = opérations passées à découvert × 8 €, plafonnées 80 €/mois) et les **frais déjà facturés** retrouvés dans l'historique (agios, commissions, impayés). Le taux vient du tarif déclaré par banque dans `rules/overdraft.json` (`rate`, `op_fee`, `op_fee_cap_month`, `rejection_fee`) et se **recalibre automatiquement** sur les agios réellement facturés quand ils apparaissent. Un récapitulatif des alertes émises / écartées sur 30 jours complète la carte.

## Recommandations de virements

Quand un compte a un besoin (découvert + marge, ou prélèvements à venir non couverts) et qu'un autre a du disponible (solde − ses propres échéances − marge − règlement de carte à prévoir), BankWatch propose le virement : montant arrondi à 5 €, comptes personnels avant les comptes pro (`accounts.<bank>.pro` dans `rules/forecast.json`), note comptable pour un compte pro. Alerte Discord « 💡 Virer X € de A vers B » une fois par semaine et par besoin, carte « Recommandations » en tête du dashboard. BankWatch ne fait **jamais** le virement lui-même.

## Synchronisation manuelle

La carte « Synchronisation » montre, par compte, les accès consommés sur les 4 autorisés par jour, la dernière synchro et le prochain passage automatique. Le bouton **Synchroniser** (admin) lance la synchro d'un compte : il consomme 1 accès, est grisé quand le quota est épuisé, et déclenche les contrôles et alertes sur les nouvelles opérations.

## Réglages

- **Seuils** : toutes les variables `BW_*` de `.env.example`.
- **Budgets** : `BW_BUDGETS={"courses": 400, "restos": 150}`.
- **Catégories** : ajoute des mots-clés dans `rules/categories.json`, puis lance `bankwatch recategorize`.

## Limites à connaître

- **Consentement DSP2** : il expire après 90 à 180 jours selon la banque. Quand une sync échoue, BankWatch envoie un message Discord : relance alors `bankwatch link` pour la banque concernée.
- **Quota des banques** : la DSP2 limite les accès sans ta présence à **4 par jour et par compte** (Swan/Indy l'applique strictement). BankWatch compte ses accès : transactions à chaque passage, solde une fois par jour, et saute un compte plutôt que dépasser. Le timer tourne 3 fois par jour (07h20, 13h20, 20h20) = 3 + 1 = 4 accès. Un `sync` manuel consomme du quota : s'il n'en reste plus, le compte est sauté jusqu'au lendemain (message « quota du jour atteint »). Un 429 de la banque est journalisé, sans alerte Discord.
- **Virements entre tes propres comptes** : renseigne `BW_OWNER_NAMES="prénom nom"` (entre guillemets, plusieurs noms séparés par des virgules). Tout libellé qui contient les deux, dans n'importe quel ordre, est classé virement interne : il est exclu des dépenses et ne déclenche pas d'alerte « inhabituel ».
- **Carte qui reprend le compte courant** : certaines banques (ex. Visa Ultim chez BoursoBank) exposent la carte comme un compte à part, alors que chaque paiement est déjà débité sur le compte courant. Exclus-la avec `bankwatch ignore <id ou nom>`, sinon les dépenses carte sont comptées deux fois et signalées comme doublons.
- **Lecture seule** : l'outil ne peut ni payer ni bloquer quoi que ce soit. Il alerte, et c'est toi qui fais opposition ou réclamation auprès de la banque.

## Version publique

Ce dépôt est privé (il contient la configuration réelle : règles perso, domaine, unités systemd). La version publiée sur GitHub est générée par `scripts/export_public.py`, qui copie les fichiers suivis, remplace `rules/*.json`, `deploy/` et `.env.example` par des exemples génériques, et **refuse d'exporter** si un motif personnel subsiste (liste `BLOCKLIST` dans le script). L'historique n'est jamais exporté : le dépôt public avance par commits de synchronisation.

```bash
python scripts/export_public.py ../bankwatch-public && cd ../bankwatch-public && git add -A && git commit -m "sync" && git push
```

## Tests

```bash
pip install -e ".[dev]" && pytest
```

---
© Karakuri Studio — Stan <stan@karakuri.studio>
