# Migration cloud — statut pour BankWatch

Fiche tenue à jour par Claude à chaque jalon de la migration. Dernière mise à jour : 8 novembre 2026, 15h.

## Où tourne BankWatch maintenant

Depuis le **7 novembre 2026**, BankWatch ne tourne plus sur le Raspberry Pi mais sur le VPS Hetzner **aria-cloud** (CAX21 : 4 vCPU ARM, 8 Go, 80 Go ; Helsinki ; Ubuntu 26.04 ; utilisateur `pi`, uid 1000, mêmes chemins qu'avant).

| Composant | Sur le VPS | Remarque |
| --- | --- | --- |
| `bankwatch.service` | actif, port 8117, exposé en `bankwatch.example.com` via l'apps-proxy et le tunnel Cloudflare `aria-cloud` | Basic Auth inchangée |
| `bankwatch-chat.service` (ce bot) | actif | lit `rules/`, `docs/`, `README.md` de ce dépôt, sur le VPS |
| `bankwatch-run.timer` | actif | premier passage sur le VPS le 8 nov. |
| `.venv` | reconstruit en Python 3.14 (`pip install -e .`) | le Pi était en 3.13 |
| `data/` | copié en dernier le 7 nov. 22h40, puis plus jamais écrit sur le Pi | la base d'état de référence est celle du VPS |
| `.env` | copié tel quel | aucune valeur changée |
| Alertes | Discord #bankwatch, inchangé | |
| budget-foyer | `budget-foyer.service`, port 8118, `budget.example.com` | même bascule, même soir |

Sur le Pi : `bankwatch`, `bankwatch-chat` et `bankwatch-run.timer` sont **arrêtés et désactivés**. Le Pi reste allumé quelques jours comme filet de sécurité, puis sera éteint définitivement avant fin janvier 2027 (départ en voyage en avril 2027, maison sans courant ni internet).

## Ce qui change concrètement

- **Adresse IP de sortie** : les appels vers Enable Banking partent désormais de l'IP Hetzner `95.216.175.227` (Finlande) et plus de la box française. Si une banque ou Enable Banking contrôle le pays de l'IP, c'est ici qu'il faudra regarder en premier.
- **Redirect URL / webhooks** : inchangés, tout passe par `*.example.com` qui pointe maintenant sur le VPS.
- **Fuseau horaire** : `Europe/Paris` sur le VPS comme sur le Pi, les timers tombent aux mêmes heures.
- **Quota DSP2** : la copie des données a conservé l'état des accès ; aucune synchro supplémentaire n'a été déclenchée par la migration.
- **Accès admin** : SSH par clé uniquement, Tailscale `100.83.131.66`. Le Pi est joignable depuis le VPS via `ssh pi`.

## Statut global de la migration

- 7 nov. : socle VPS, Karakuri, RecovR, BankWatch, budget-foyer, Claude dans Discord, Msafara, veilles, Vaultwarden, n8n, MCP ARIA basculés en une soirée. Tous les DNS pointent sur le VPS.
- 8 nov. : nuit sans incident (backups Vaultwarden et Msafara, veille emploi). Corrections : droits Postgres RecovR, credential Postgres et URL de la veille tech qui pointaient encore sur le Pi, dossier backup Vaultwarden.
- À venir : run usine Karakuri du 10 nov. 10h (validation réelle), extinction test du Pi, suppression de l'ancien tunnel, archivage des artefacts du Pi sur Dropbox.
- Non migré par décision : générateur d'apps, agent LinkedIn, runner GitLab, Pi-hole, MariaDB.

## Références

- Plan complet (inventaire, budget, avancement) : doc Claude « Migration du Pi vers le cloud ».
- Journal en direct : Discord #karakuri-monitor et `/home/pi/migration/journal.md` sur le VPS.
