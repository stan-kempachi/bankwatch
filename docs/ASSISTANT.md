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
