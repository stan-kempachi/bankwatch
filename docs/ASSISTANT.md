# Assistant Discord (#budget)

Un seul bot, dans le salon **#budget**, pour toutes les instances BankWatch ; les salons `#bankwatch*`
ne servent qu'aux alertes.

## Deux moteurs

- `BW_CHAT_BACKEND=api` : appels directs à l'API Claude (clé `ANTHROPIC_API_KEY`), outils en Python
  dans le processus du bot (`bankwatch/chat.py`).
- `BW_CHAT_BACKEND=claude-code` (défaut en production depuis le 8 nov. 2026) : le bot héberge un
  **serveur MCP HTTP local** (`127.0.0.1:BW_CHAT_MCP_PORT`, défaut 8121) et lance **`claude -p`**
  pour chaque message (`bankwatch/chat_cc.py`). Claude Code est authentifié avec l'abonnement du
  propriétaire (`claude /login` une fois sur le serveur) : la variable `ANTHROPIC_API_KEY` est retirée
  de l'environnement du sous-processus, sinon elle primerait sur l'abonnement.

## Garde-fous du mode Claude Code

- Aucun outil intégré de Claude Code (`--tools ""`, `Bash`/`Edit`/`Write`/… interdits, `--strict-mcp-config`,
  `--setting-sources ""`) : seuls les outils du serveur MCP `bankwatch` existent.
- Outils de lecture pré-autorisés : `etat`, `bankwatch_lire`, `bankwatch_sql`, `budget_sql`,
  `fichier_lire`, `memoire_ajouter`.
- Outils d'action (`bankwatch_action`, `regle_ecrire`) : Claude Code appelle l'outil de permission
  `approuver` (`--permission-prompt-tool`), qui pose la question ✅ dans Discord (bouton, 5 min) et
  répond `allow`/`deny`. Le propriétaire du bot peut agir sur toutes les instances ; une autre personne
  seulement sur la sienne. Les règles ne sont modifiables que par le propriétaire.
- Chaque invocation porte un en-tête `X-BW-Req` : le serveur MCP y retrouve l'auteur (instance par
  défaut, droits) et le salon où valider.
- Historique : une session Claude Code par salon/fil (`cc_sessions` dans `data/chat.db`, `--resume`) ;
  `reset` l'oublie.

## Instances

`data/chat_instances.json` (hors git) : `{"<id>": {"root": "/chemin/du/clone", "label": "…", "users": ["<id Discord>"]}}`.
Sans ce fichier, le bot ne voit que l'instance de son propre dépôt. Le CLI d'une autre instance est lancé
dans son dossier, avec son venv et un environnement purgé des variables `BW_*`.

## Réglages (`.env`)

`BW_CHAT_BACKEND`, `BW_CHAT_CC_BIN` (`claude`), `BW_CHAT_CC_MODEL` (`sonnet`), `BW_CHAT_CC_MAX_TURNS` (12),
`BW_CHAT_CC_TIMEOUT` (600 s, inclut l'attente d'une validation), `BW_CHAT_MCP_PORT` (8121),
`BW_CHAT_INSTANCES_FILE` (`data/chat_instances.json`). Test sans Discord :
`python -m bankwatch.chat --test "question"` (validations refusées).

## Délégation de code : `agent_code` et `agent_deployer`

Deux outils du serveur MCP du bot (port `BW_CHAT_MCP_PORT`), réservés au **propriétaire** (`BW_CHAT_OWNER_ID`) et aux
comptes listés dans `BW_CHAT_AGENT_USER_IDS` (les autres droits du propriétaire ne sont pas étendus), et
soumis à une validation ✅ **à chaque appel** via `--permission-prompt-tool` ; les outils revérifient eux-mêmes
qu'un ✅ a bien été donné pour ces arguments précis. Code : `bankwatch/agent.py`, `deploy/agent_deploy.py`.

### `agent_code(depot, prompt)`

- `depot` est une clé de la liste blanche `data/agent_depots.json` (hors git), jamais un chemin. Le prompt
  (1 500 caractères max) est affiché **en entier** dans la demande de validation, avec le dépôt.
- Le bot clone la branche principale dans `~/agent-work/<depot>` et y crée `agent/<date>-<slug>`. **Le dépôt en
  service n'est pas touché pendant la session** : un service ou un timer qui redémarre ne peut pas exécuter du
  code non relu.
- Une session `claude -p` séparée de celle de l'assistant tourne dans un bac à sable bubblewrap : elle ne voit
  que le clone (ni `.env`, ni `data/`, ni le reste de `$HOME`, ni `~/.claude` — elle reçoit seulement le jeton
  d'accès court de l'abonnement). Outils intégrés : `Read`, `Grep`, `Glob`, `Edit`, `Write`, rien d'autre.
- Le « Bash restreint » est remplacé par trois outils MCP (second serveur local, `BW_CHAT_AGENT_MCP_PORT`) que
  le bot exécute sans shell, dans un second bac à sable **sans réseau** : `git` (status, diff, log, show, add,
  commit, rm, mv, restore), `pytest`, `ruff`. Pourquoi pas le Bash de Claude Code : `pytest` exécute du code
  arbitraire, donc un filtrage par motif de commande ne garantit rien ; ici c'est le noyau qui interdit le
  réseau, sudo (no_new_privs), systemctl (pas de D-Bus) et toute écriture hors du clone.
- Durée maximale `BW_CHAT_AGENT_TIMEOUT` (600 s). À la fin, le bot commite un éventuel reliquat, relance pytest
  et ruff lui-même, puis rapatrie la branche dans le dépôt en service par un bundle git (des données, aucun
  hook ni configuration du clone n'est exécuté). **Ni fusion, ni push.** La réponse est tronquée : commits,
  fichiers modifiés, statistiques du diff, résultat des tests, compte rendu de l'agent.

### `agent_deployer(depot)`

- Déploie la dernière branche d'agent du dépôt, avec son propre ✅ (branche, commits, services affichés).
  Refus si la session n'est pas « ok », si les tests échouent, si la branche a bougé depuis, si le dépôt en
  service n'est pas sur la branche principale ou si un fichier de dépendances est modifié.
- Le travail est fait par `deploy/agent_deploy.py` dans une unité systemd transitoire (le bot peut faire partie
  des services redémarrés) : fusion `--no-ff`, alignement des clones « suivants », redémarrage, tests de santé
  (`url`, `unit` actif et non relancé par systemd, `port`). Un test en échec → `git reset --keep` vers le commit
  précédent (les modifications locales non commitées sont préservées), redémarrage, nouvelle vérification.
  Le compte rendu est posté dans le salon.

### Journal

Table `agent_journal` de `data/chat.db` : une ligne par appel, refus compris (qui, quand, dépôt, prompt, statut,
branche, commits de base et de tête, fichiers touchés, tests, durée, date de déploiement). La configuration
Claude isolée et la transcription de chaque session sont gardées 30 jours dans `data/agent-runs/`.

```json
{"mon-depot": {"repo": "/chemin/du/depot", "venv": "/chemin/du/depot/.venv", "branche": "main",
               "services": ["mon-service.service"],
               "health": [{"url": "http://127.0.0.1:8000/healthz"}, {"unit": "mon-service.service"}],
               "suivants": [], "deps": ["pyproject.toml"]}}
```

Les réponses du serveur MCP sont en flux SSE (`json_response=False`) avec un signe de vie périodique : Claude
Code abandonne au bout de 60 s une réponse JSON simple, ce qui coupait aussi les validations ✅ tardives.
