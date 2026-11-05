# Brancher une autre application sur BankWatch

BankWatch est la **seule** application qui parle aux banques : le quota DSP2 (4 accès par compte et par
jour) ne supporterait pas deux clients. Les autres applications (budget, facturation, tableaux de bord…)
se servent chez BankWatch, en local, avec deux mécanismes complémentaires :

1. **API REST de lecture** (`/api/export/…`, jeton Bearer) — « donne-moi les opérations depuis telle date ».
2. **Webhooks CloudEvents signés** — « une synchro vient d'avoir lieu, N nouvelles opérations ».

Le schéma est volontairement minimal : *événement léger, puis lecture complète* (« thin event, fat fetch »).
L'abonné ne dépend jamais du contenu du webhook pour être à jour ; il peut aussi tirer périodiquement.

## 1. Jeton d'application

```bash
bankwatch token create budget      # affiche bw_… une seule fois
bankwatch token list
bankwatch token revoke budget
```

Le jeton donne le rôle `viewer` : toute l'API de lecture, jamais `/setup`, `/callback`, ni la synchro
manuelle. Il est stocké haché (`data/tokens.json`, droits 600).

```http
GET /api/export/accounts
GET /api/export/transactions?since=2026-10-01[&until=…][&account=<id>][&status=booked|pending]
Authorization: Bearer bw_…
```

### `GET /api/export/accounts`

```json
{"accounts": [{"id": "…", "bank": "indy", "name": "Compte courant", "iban": "FR76…", "currency": "EUR",
               "pro": true, "ignored": false,
               "balance": 1234.5, "balance_estimate": 1180.5, "balance_read_at": "2026-11-05T07:20:11",
               "last_sync": "2026-11-05T13:20:03"}],
 "generated_at": "2026-11-05T14:00:00"}
```

`pro` vient de `rules/forecast.json` (`accounts.<banque>.pro`). `balance_estimate` = dernier solde lu
+ opérations vues depuis.

### `GET /api/export/transactions`

Par défaut les 90 derniers jours, comptes ignorés exclus, 5 000 lignes max (`limit` jusqu'à 20 000).

```json
{"items": [{"id": "…", "account_id": "…", "bank": "indy", "booking_date": "2026-11-04", "value_date": null,
            "amount": -172.9, "currency": "EUR", "counterparty": "SAASCO", "label": "SAASCO ABONNEMENT",
            "category": "abonnements", "status": "booked", "internal": false,
            "merchant_key": "saasco", "counter_iban": ""}],
 "count": 1, "since": "2026-08-07", "categories": ["abonnements"]}
```

- `amount` négatif = débit ; `status` `pending` = en attente (n'apparaît que si demandé ou sans filtre).
- `category` : catégorie BankWatch (`courses`, `restos`, `transport`, `logement`, `abonnements`, `sante`,
  `shopping`, `banque_frais`, `impots_taxes`, `voyages`, `epargne_virement`, `revenus`, `retrait`, `autre`
  + catégories perso de `rules/categories.json`).
- `internal: true` = virement entre vos propres comptes (déjà apparié par BankWatch) : à ignorer dans un budget.
- `id` est stable : une opération en attente puis comptabilisée garde l'identifiant de la version comptabilisée.

## 2. Webhooks (CloudEvents 1.0)

Abonnés dans `rules/webhooks.json` :

```json
{"subscribers": [
  {"name": "budget", "url": "http://127.0.0.1:8118/hooks/bankwatch",
   "secret_env": "BW_WEBHOOK_SECRET_BUDGET", "events": ["sync.completed"]}
]}
```

Le secret partagé est lu dans la variable d'environnement indiquée (donc dans `.env`), jamais dans le
fichier de règles. `openssl rand -hex 32` fait un bon secret.

Après chaque `bankwatch run` / `bankwatch sync` (et après une synchro manuelle depuis le dashboard
quand elle apporte du nouveau), BankWatch envoie :

```http
POST /hooks/bankwatch
Content-Type: application/cloudevents+json
X-BankWatch-Event: sync.completed
X-BankWatch-Delivery: <id unique>
X-BankWatch-Signature: t=1762350000,v1=<hex HMAC-SHA256(secret, "<t>.<corps brut>")>

{"specversion": "1.0", "id": "…", "source": "urn:bankwatch", "type": "dev.bankwatch.sync.completed",
 "time": "2026-11-05T13:20:05Z", "datacontenttype": "application/json",
 "data": {"accounts": [{"id": "…", "bank": "indy", "new": 3, "error": null}], "new_transactions": 3, "errors": 0}}
```

Côté récepteur : recalculer `v1` sur le **corps brut**, comparer en temps constant, rejeter si `t` a plus
de 5 minutes. Une réponse 2xx acquitte ; sinon BankWatch réessaie 3 fois (sauf 4xx autre que 429).
`bankwatch webhook-test` envoie un événement `ping` à tous les abonnés.

Vérification en Python (reprise dans l'appli budget) :

```python
import hmac, hashlib, time

def verify(secret: str, body: bytes, header: str, tolerance: int = 300) -> bool:
    parts = dict(p.split("=", 1) for p in header.split(",") if "=" in p)
    ts = int(parts.get("t", 0))
    if abs(time.time() - ts) > tolerance:
        return False
    expected = hmac.new(secret.encode(), f"{ts}.".encode() + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, parts.get("v1", ""))
```

## 3. Pourquoi pas STOMP / MQTT / syslog ?

- **STOMP, AMQP** : imposent un broker (RabbitMQ, ActiveMQ) pour quelques messages par jour.
- **MQTT** : pertinent si de nombreux consommateurs s'abonnent aux mêmes événements ; l'enveloppe
  CloudEvents ci-dessus se publie telle quelle sur un topic MQTT le jour venu.
- **syslog / CEF / STIX-TAXII** : protocoles de journalisation et de renseignement sécurité (SIEM) —
  envoi sans réponse, sans garantie, pas faits pour transporter des données métier à intégrer.
