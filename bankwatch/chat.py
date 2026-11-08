"""BankWatch dans Discord — assistant conversationnel (API Claude) cantonné à un salon.

Lancement : `python -m bankwatch.chat` (service systemd `bankwatch-chat.service`)
Test sans Discord : `python -m bankwatch.chat --test "où en sont mes comptes ?"`

Principes :
  - un seul salon (BW_CHAT_CHANNEL_IDS) et des utilisateurs autorisés (BW_CHAT_OWNER_ID, BW_CHAT_USER_IDS) ;
    tout le reste est ignoré en silence ;
  - chaque message du salon est une question pour l'assistant (pas de mention nécessaire) ;
  - outils : commandes `bankwatch` (liste blanche ; celles qui consomment du quota DSP2 ou modifient
    des données passent par un bouton ✅), SQL en lecture seule sur la base BankWatch et sur la base
    de l'appli Budget du foyer, lecture des fichiers `rules/` et `docs/`, écriture des règles (✅ propriétaire),
    mémoire durable (`data/chat_memoire.md`) ;
  - historique par salon/fil et compteur de tokens dans `data/chat.db` ; plafond quotidien.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import shlex
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo

import anthropic

from .config import settings

log = logging.getLogger("bankwatch.chat")
TZ = ZoneInfo("Europe/Paris")
ROOT = Path(__file__).resolve().parent.parent  # racine du dépôt (cwd du service)

# --------------------------------------------------------------------------- config


def _env_file_value(path: str, key: str) -> str:
    try:
        for line in open(path, encoding="utf-8"):
            k, _, v = line.strip().partition("=")
            if k == key:
                return v.strip().strip("\"'")
    except FileNotFoundError:
        pass
    return ""


def anthropic_key() -> str:
    key = os.environ.get("ANTHROPIC_API_KEY") or settings.chat_anthropic_api_key
    if not key and settings.chat_anthropic_env_file:
        key = _env_file_value(settings.chat_anthropic_env_file, "ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("ANTHROPIC_API_KEY introuvable (env, BW_CHAT_ANTHROPIC_API_KEY ou BW_CHAT_ANTHROPIC_ENV_FILE)")
    return key


def discord_token() -> str:
    tok = settings.discord_bot_token or os.environ.get("DISCORD_BOT_TOKEN", "")
    if not tok and settings.discord_token_file:
        tok = _env_file_value(settings.discord_token_file, "DISCORD_BOT_TOKEN")
    if not tok:
        raise SystemExit("DISCORD_BOT_TOKEN introuvable (BW_DISCORD_BOT_TOKEN ou BW_DISCORD_TOKEN_FILE)")
    return tok


def _ids(value: str) -> set[int]:
    return {int(x) for x in value.split(",") if x.strip()}


CHANNEL_IDS = _ids(settings.chat_channel_ids)
OWNER_ID = int(settings.chat_owner_id or 0)
ALLOWED_IDS = _ids(settings.chat_user_ids) | ({OWNER_ID} if OWNER_ID else set())
MODELS = [m.strip() for m in settings.chat_models.split(",") if m.strip()]
DAILY_CAP = settings.chat_daily_token_cap
MEMOIRE = Path(settings.chat_memory_path)
CHAT_DB = Path(settings.chat_db_path)
BUDGET_DB = Path(settings.chat_budget_db) if settings.chat_budget_db else None
BANKWATCH_BIN = Path(sys.executable).parent / "bankwatch"

# --------------------------------------------------------------------------- historique / usage


def _chat_conn() -> sqlite3.Connection:
    CHAT_DB.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(CHAT_DB))
    conn.executescript(
        """CREATE TABLE IF NOT EXISTS chat(scope TEXT, ts REAL, role TEXT, author TEXT, content TEXT);
           CREATE INDEX IF NOT EXISTS chat_scope ON chat(scope, ts);
           CREATE TABLE IF NOT EXISTS usage(day TEXT PRIMARY KEY, input INTEGER NOT NULL DEFAULT 0,
                                            output INTEGER NOT NULL DEFAULT 0);"""
    )
    return conn


def history_load(scope: str, max_msgs: int = 30, max_chars: int = 20000) -> list[dict[str, str]]:
    with _chat_conn() as c:
        rows = c.execute("SELECT role, author, content FROM chat WHERE scope=? ORDER BY ts DESC LIMIT ?",
                         (scope, max_msgs)).fetchall()
    out: list[dict[str, str]] = []
    total = 0
    for role, author, content in rows:
        text = f"[{author}] {content}" if role == "user" and author else content
        total += len(text)
        if total > max_chars:
            break
        out.append({"role": role, "content": text})
    out.reverse()
    # l'API exige une alternance user/assistant commençant par user
    merged: list[dict[str, str]] = []
    for m in out:
        if merged and merged[-1]["role"] == m["role"]:
            merged[-1]["content"] += "\n" + m["content"]
        else:
            merged.append(dict(m))
    while merged and merged[0]["role"] != "user":
        merged.pop(0)
    return merged


def history_add(scope: str, role: str, content: str, author: str | None = None) -> None:
    with _chat_conn() as c:
        c.execute("INSERT INTO chat VALUES(?,?,?,?,?)", (scope, time.time(), role, author, content[:12000]))


def history_clear(scope: str) -> int:
    with _chat_conn() as c:
        return c.execute("DELETE FROM chat WHERE scope=?", (scope,)).rowcount


def usage_today() -> tuple[int, int]:
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    with _chat_conn() as c:
        row = c.execute("SELECT input, output FROM usage WHERE day=?", (day,)).fetchone()
    return (row[0], row[1]) if row else (0, 0)


def usage_add(inp: int, out: int) -> None:
    day = datetime.now(TZ).strftime("%Y-%m-%d")
    with _chat_conn() as c:
        c.execute("""INSERT INTO usage(day,input,output) VALUES(?,?,?)
                     ON CONFLICT(day) DO UPDATE SET input=input+excluded.input, output=output+excluded.output""",
                  (day, inp, out))


# --------------------------------------------------------------------------- outils

# Commandes bankwatch autorisées. "free" = exécution directe ; "confirm" = bouton ✅ (quota DSP2 ou écriture).
FREE_CMDS = {"accounts", "alerts", "forecast", "watch", "institutions"}
FREE_SUBCMDS = {("user", "list"), ("token", "list")}
CONFIRM_CMDS = {"sync", "run", "check", "dismiss", "dedupe", "recategorize", "ignore", "report",
                "notify-test", "webhook-test", "link"}
RULE_FILES = {"categories.json", "forecast.json", "overdraft.json", "watch.json"}
SQL_FORBIDDEN = re.compile(r"\b(attach|detach|pragma|insert|update|delete|drop|alter|create|replace|vacuum|reindex)\b", re.I)


class Refused(Exception):
    """Action refusée (hors liste blanche, ou validation ✅ refusée)."""


def classify_cmd(args: list[str]) -> str:
    """Retourne 'free' ou 'confirm', ou lève Refused."""
    if not args:
        raise Refused("commande vide")
    head = args[0]
    if head in FREE_CMDS:
        return "free"
    if len(args) >= 2 and (head, args[1]) in FREE_SUBCMDS:
        return "free"
    if head == "report" and "--dry-run" in args:
        return "free"
    if head in CONFIRM_CMDS:
        return "confirm"
    raise Refused(f"`bankwatch {head}` n'est pas disponible depuis Discord (réservé au shell du Pi).")


async def run_bankwatch(args: list[str], timeout: int = 240) -> str:
    env = dict(os.environ, NO_COLOR="1", TERM="dumb", COLUMNS="110", PYTHONUNBUFFERED="1")
    proc = await asyncio.create_subprocess_exec(
        str(BANKWATCH_BIN), *args, cwd=str(ROOT), env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return f"⏱️ `bankwatch {' '.join(args)}` interrompu après {timeout}s."
    text = out.decode("utf-8", "replace").strip()
    text = re.sub(r"\x1b\[[0-9;]*m", "", text)
    if len(text) > 6000:
        text = text[:6000] + "\n… (sortie tronquée)"
    return f"(code retour {proc.returncode})\n{text}" if proc.returncode else (text or "(aucune sortie)")


def _ro_query(db: Path, sql: str, max_rows: int = 200) -> str:
    s = sql.strip().rstrip(";").strip()
    if not re.match(r"^(select|with)\b", s, re.I) or ";" in s or SQL_FORBIDDEN.search(s):
        raise Refused("Seules les requêtes SELECT (une seule instruction) sont autorisées.")
    if not db.exists():
        raise Refused(f"base introuvable : {db.name}")
    conn = sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=5)
    try:
        conn.execute("PRAGMA query_only=1")
        deadline = time.monotonic() + 8
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
        cur = conn.execute(s)
        cols = [d[0] for d in cur.description or []]
        rows = cur.fetchmany(max_rows + 1)
    finally:
        conn.close()
    more = len(rows) > max_rows
    rows = rows[:max_rows]
    lines = [" | ".join(cols)]
    for r in rows:
        lines.append(" | ".join("" if v is None else str(v)[:120] for v in r))
    if more:
        lines.append(f"… (plus de {max_rows} lignes, affine la requête)")
    text = "\n".join(lines)
    return text[:5000] + ("\n… (tronqué)" if len(text) > 5000 else "")


def t_bankwatch_sql(sql: str) -> str:
    return _ro_query(Path(settings.db_path), sql)


def t_budget_sql(sql: str) -> str:
    if not BUDGET_DB:
        raise Refused("Budget du foyer non configuré (BW_CHAT_BUDGET_DB).")
    return _ro_query(BUDGET_DB, sql)


def _safe_repo_path(rel: str, allowed_dirs: tuple[str, ...]) -> Path:
    p = (ROOT / rel).resolve()
    if not p.is_relative_to(ROOT) or p.name.startswith("."):
        raise Refused("chemin refusé")
    if p.name == "README.md" and p.parent == ROOT:
        return p
    if p.parent.name not in allowed_dirs or p.parent.parent != ROOT:
        raise Refused(f"lecture limitée à {', '.join(d + '/' for d in allowed_dirs)} et README.md")
    return p


def t_fichier_lire(chemin: str) -> str:
    p = _safe_repo_path(chemin, ("rules", "docs"))
    if not p.exists():
        raise Refused(f"{chemin} n'existe pas")
    text = p.read_text(encoding="utf-8")
    return text[:12000] + ("\n… (tronqué)" if len(text) > 12000 else "")


def _rule_write(fichier: str, contenu: str) -> str:
    if fichier not in RULE_FILES:
        raise Refused(f"fichier de règles inconnu : {fichier} (attendu : {', '.join(sorted(RULE_FILES))})")
    data = json.loads(contenu)  # lève ValueError si invalide
    path = ROOT / "rules" / fichier
    bak = ROOT / "rules" / ".bak"
    bak.mkdir(exist_ok=True)
    if path.exists():
        (bak / f"{path.stem}.{datetime.now(TZ):%Y%m%d_%H%M%S}.json").write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return f"{fichier} écrit (sauvegarde dans rules/.bak/). Pense à `bankwatch recategorize` si les catégories ont changé."


def memoire_lire() -> str:
    try:
        return MEMOIRE.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "(mémoire vide)"


def t_memoire_ajouter(note: str) -> str:
    note = " ".join(note.split())
    if not note:
        raise Refused("note vide")
    MEMOIRE.parent.mkdir(parents=True, exist_ok=True)
    with open(MEMOIRE, "a", encoding="utf-8") as f:
        f.write(f"- {datetime.now(TZ):%d/%m/%Y} : {note}\n")
    return "Noté dans la mémoire."


def t_etat() -> str:
    """Photo rapide : soldes (lus + estimés), quota du jour, alertes récentes, dernier passage."""
    from .db import Database

    db = Database()
    try:
        today = datetime.now(TZ).strftime("%Y-%m-%d")
        lines = []
        for a in db.accounts(include_ignored=False):
            eb = db.effective_balance(a["id"])
            used = db.get_meta(f"access:{a['id']}:{today}", "0")
            est = eb["estimate"]
            lines.append(
                f"- {a['bank']} {a['name'] or ''} ({(a['iban'] or '')[-4:]}) : solde lu {eb['balance']} € "
                f"({(eb['read_at'] or '')[:16]}), estimé {est} € ({eb['since_count']} op. depuis), "
                f"accès DSP2 aujourd'hui {used}/{settings.eb_daily_access_limit}")
        alerts = db.alerts(limit=10)
        lines.append("Alertes récentes (non écartées) :" if alerts else "Aucune alerte récente.")
        for al in alerts:
            lines.append(f"  · #{al['id']} [{al['severity']}] {al['title']} — {al['created_at'][:16]}")
        row = db.conn.execute("SELECT MAX(last_sync) FROM accounts WHERE ignored = 0").fetchone()
        last = (row[0] or "?")[:16]
        lines.append(f"Dernier passage : {last} · passages planifiés : {settings.sync_schedule}")
        return "\n".join(lines)
    finally:
        db.close()


TOOLS: list[dict[str, Any]] = [
    {"name": "etat", "description": "Photo rapide de BankWatch : solde lu et estimé par compte, quota DSP2 consommé aujourd'hui, alertes récentes, dernier passage. À appeler en premier pour « où j'en suis ? ».",
     "input_schema": {"type": "object", "properties": {}}},
    {"name": "bankwatch_cmd", "description": "Exécute une commande du CLI bankwatch. Sans validation : accounts, alerts [--all], forecast, watch, institutions <nom>, report --dry-run, user list, token list. Avec bouton ✅ de l'utilisateur : sync (consomme du quota DSP2 : 1 accès transactions par compte, +1 si relecture de solde), run (sync+contrôles+alertes), check [--no-notify], dismiss <n°>, dedupe, recategorize, ignore <id>, report --days N, notify-test, webhook-test, link <nom-court> \"<ASPSP>\" [--business] (génère un lien de consentement bancaire à transmettre à l'utilisateur ; ne consomme pas de quota). Tout le reste est refusé.",
     "input_schema": {"type": "object", "properties": {"args": {"type": "string", "description": "arguments après `bankwatch`, ex. `alerts --all` ou `dismiss 12`"}}, "required": ["args"]}},
    {"name": "bankwatch_sql", "description": "Requête SELECT en lecture seule sur la base BankWatch (SQLite). Tables : accounts(id,bank,name,iban,balance,balance_date,last_sync,ignored), transactions(id,account_id,booking_date,value_date,amount,counterparty,label,category,status[booked|pending],first_seen ; NE PAS sélectionner la colonne raw), alerts(id,kind,severity,title,message,amount,tx_ids,created_at,sent_at,dismissed), meta(key,value). Montants : débit négatif. Dates ISO. 200 lignes max.",
     "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
    {"name": "budget_sql", "description": "Requête SELECT en lecture seule sur la base de l'appli Budget du foyer (SQLite). Tables : docs(path,data JSON) avec path 'budget/config' (postes : id, nom, type pro|revenu|fixe|variable…, montant, compte), 'budget/jours', 'mois/AAAA-MM' (prev/reel par poste) ; transactions(id,account_uid,booking_date,amount,label,counterparty,status[a_classer|classe|…],poste_id,rule_id,category,internal) ; rules(id,pattern,sens,account_uid,action,poste_id,priority) ; bank_accounts(uid,bank,name,iban,role,last_sync,active). Utilise json_extract() sur docs.data.",
     "input_schema": {"type": "object", "properties": {"sql": {"type": "string"}}, "required": ["sql"]}},
    {"name": "fichier_lire", "description": "Lit un fichier du dépôt BankWatch : rules/*.json (categories, watch, forecast, overdraft, webhooks), docs/*.md, README.md.",
     "input_schema": {"type": "object", "properties": {"chemin": {"type": "string"}}, "required": ["chemin"]}},
    {"name": "regle_ecrire", "description": "Remplace un fichier de règles (categories.json, watch.json, forecast.json, overdraft.json) par le JSON fourni, après bouton ✅ du propriétaire. Toujours lire le fichier avant, modifier le minimum, renvoyer le JSON complet. Sauvegarde automatique.",
     "input_schema": {"type": "object", "properties": {"fichier": {"type": "string"}, "contenu": {"type": "string", "description": "JSON complet du fichier"}, "resume": {"type": "string", "description": "une phrase décrivant le changement, montrée dans la demande de validation"}}, "required": ["fichier", "contenu", "resume"]}},
    {"name": "memoire_ajouter", "description": "Ajoute une note durable à la mémoire de l'assistant (relue à chaque message). Pour ce que l'utilisateur demande explicitement de retenir (« retiens que… »).",
     "input_schema": {"type": "object", "properties": {"note": {"type": "string"}}, "required": ["note"]}},
]

ConfirmFn = Callable[[str, str], Awaitable[bool]]  # (titre, détail) -> validé ?


def system_prompt(authors: str) -> str:
    now = datetime.now(TZ)
    return f"""Tu es l'assistant de cette instance BankWatch, dans son salon Discord dédié. Nous sommes le {now:%A %d %B %Y, %H:%M} (Paris).

Périmètre : BankWatch (surveillance des comptes bancaires : soldes, opérations, alertes, prévisions de prélèvements, recommandations de virements, règles de catégorisation, commandes du CLI) et, en lecture seule, l'appli « Budget du foyer » (postes, prévu/réel par mois, règles de classement). Pour tout autre sujet, réponds en une phrase que tu es cantonné à BankWatch et au budget.

Interlocuteurs autorisés : {authors}. Les messages arrivent préfixés par [pseudo].

Règles de travail :
- Vérifie avant d'affirmer : utilise les outils (etat, SQL, commandes) plutôt que ta mémoire pour tout chiffre.
- `sync`/`run` consomment le quota DSP2 (4 accès par compte et par jour, Swan/Indy strict) : regarde d'abord `etat` et dis combien il reste avant de proposer une synchro ; ne lance jamais une action à validation sans que l'utilisateur l'ait demandée.
- Les montants : débits négatifs en base ; présente-les en euros avec le signe clair. Solde « estimé » = dernière lecture banque + opérations vues depuis.
- Pour modifier une règle : lis le fichier, propose le changement, écris le JSON complet via regle_ecrire, puis propose `recategorize`.
- Format Discord : réponses courtes (téléphone), listes à puces, **gras** pour les chiffres clés, blocs ``` pour les sorties de commandes ; pas de tableaux Markdown (Discord ne les rend pas), pas de titres #. Réponds en français.
- Une action refusée ou non validée : dis-le simplement et propose la suite.

Mémoire durable (contexte du projet et notes) :
{memoire_lire()}
"""


class Agent:
    def __init__(self) -> None:
        self.client = anthropic.AsyncAnthropic(api_key=anthropic_key())
        self.model = MODELS[0]

    async def create(self, **kw: Any) -> anthropic.types.Message:
        last: Exception | None = None
        for m in [self.model] + [x for x in MODELS if x != self.model]:
            try:
                resp = await self.client.messages.create(model=m, **kw)
                self.model = m
                return resp
            except anthropic.NotFoundError as e:
                log.warning("modèle %s indisponible : %s", m, e)
                last = e
        raise last or RuntimeError("aucun modèle disponible")

    async def run(self, scope: str, text: str, author: str, is_owner: bool, authors: str,
                  confirm: ConfirmFn, progress: Callable[[str], Awaitable[None]] | None = None) -> str:
        inp, out = usage_today()
        if inp + out >= DAILY_CAP:
            return f"⛔ Plafond quotidien atteint ({inp + out:,} tokens / {DAILY_CAP:,}). Reprise demain, ou augmente BW_CHAT_DAILY_TOKEN_CAP."
        messages = history_load(scope) + [{"role": "user", "content": f"[{author}] {text}"}]
        history_add(scope, "user", text, author)
        final = ""
        for _ in range(12):
            resp = await self.create(max_tokens=2048, system=system_prompt(authors), tools=TOOLS, messages=messages)
            usage_add(resp.usage.input_tokens, resp.usage.output_tokens)
            messages.append({"role": "assistant", "content": resp.content})
            texts = [b.text for b in resp.content if b.type == "text"]
            calls = [b for b in resp.content if b.type == "tool_use"]
            if texts:
                final = "\n".join(texts).strip()
            if not calls:
                break
            results = []
            for call in calls:
                result = await self.tool(call.name, dict(call.input), is_owner, confirm, progress)
                results.append({"type": "tool_result", "tool_use_id": call.id, "content": result})
            messages.append({"role": "user", "content": results})
        else:
            final = final or "Je me suis arrêté : trop d'étapes d'outils pour une seule question."
        final = final or "(pas de réponse)"
        history_add(scope, "assistant", final)
        return final

    async def tool(self, name: str, args: dict[str, Any], is_owner: bool, confirm: ConfirmFn,
                   progress: Callable[[str], Awaitable[None]] | None) -> str:
        try:
            if name == "etat":
                return await asyncio.to_thread(t_etat)
            if name == "bankwatch_sql":
                return await asyncio.to_thread(t_bankwatch_sql, args["sql"])
            if name == "budget_sql":
                return await asyncio.to_thread(t_budget_sql, args["sql"])
            if name == "fichier_lire":
                return t_fichier_lire(args["chemin"])
            if name == "memoire_ajouter":
                return t_memoire_ajouter(args["note"])
            if name == "regle_ecrire":
                if not is_owner:
                    raise Refused("seul le propriétaire peut modifier les règles")
                json.loads(args["contenu"])
                if not await confirm(f"Modifier rules/{args['fichier']}", args.get("resume", "")):
                    raise Refused("modification non validée")
                return _rule_write(args["fichier"], args["contenu"])
            if name == "bankwatch_cmd":
                argv = shlex.split(args["args"])
                if argv and argv[0] == "bankwatch":
                    argv = argv[1:]
                mode = classify_cmd(argv)
                if mode == "confirm" and not await confirm(f"bankwatch {' '.join(argv)}", "Exécuter cette commande sur le Pi ?"):
                    raise Refused("commande non validée")
                if progress and mode == "confirm":
                    await progress(f"⏳ `bankwatch {' '.join(argv)}` en cours…")
                return await run_bankwatch(argv)
            return f"outil inconnu : {name}"
        except Refused as e:
            return f"REFUSÉ : {e}"
        except (ValueError, sqlite3.Error) as e:
            return f"ERREUR : {e}"
        except Exception as e:  # noqa: BLE001 — on renvoie l'erreur au modèle plutôt que de planter le bot
            log.exception("outil %s", name)
            return f"ERREUR interne : {type(e).__name__}: {e}"


HELP = """**Assistant BankWatch** — je réponds à tout message de ce salon.
Exemples : « où j'en suis ? », « combien de courses ce mois ? », « quelles alertes cette semaine ? », « lance une synchro » (✅ à valider), « écarte l'alerte 12 », « ajoute le mot-clé X à la catégorie restos », « qu'est-ce qui est prévu au budget en décembre ? », « retiens que … ».
Mots-clés : `aide` · `reset` (oublie l'historique du salon) · `usage` (tokens du jour)."""


def chunks(text: str, size: int = 1990) -> list[str]:
    parts: list[str] = []
    while len(text) > size:
        cut = text.rfind("\n", 0, size)
        if cut < size // 2:
            cut = size
        parts.append(text[:cut])
        text = text[cut:].lstrip("\n")
    parts.append(text)
    return parts


# --------------------------------------------------------------------------- Discord


def run_discord() -> None:
    import discord

    agent = Agent()
    intents = discord.Intents.default()
    intents.message_content = True
    client = discord.Client(intents=intents)
    authors_desc = ", ".join(f"<@{i}>" for i in sorted(ALLOWED_IDS))

    class ConfirmView(discord.ui.View):
        def __init__(self, allowed: set[int]):
            super().__init__(timeout=300)
            self.allowed = allowed
            self.result: asyncio.Future[bool] = asyncio.get_running_loop().create_future()

        async def _answer(self, inter: discord.Interaction, ok: bool) -> None:
            if inter.user.id not in self.allowed:
                await inter.response.send_message("Seul le demandeur ou le propriétaire peut valider.", ephemeral=True)
                return
            for item in self.children:
                item.disabled = True  # type: ignore[attr-defined]
            await inter.response.edit_message(content=inter.message.content + ("\n✅ validé" if ok else "\n❌ annulé"), view=self)
            if not self.result.done():
                self.result.set_result(ok)
            self.stop()

        @discord.ui.button(label="✅ Valider", style=discord.ButtonStyle.success)
        async def yes(self, inter: discord.Interaction, _b: discord.ui.Button) -> None:
            await self._answer(inter, True)

        @discord.ui.button(label="❌ Annuler", style=discord.ButtonStyle.danger)
        async def no(self, inter: discord.Interaction, _b: discord.ui.Button) -> None:
            await self._answer(inter, False)

        async def on_timeout(self) -> None:
            if not self.result.done():
                self.result.set_result(False)

    @client.event
    async def on_ready() -> None:
        log.info("connecté : %s — salons %s — autorisés %s — modèle %s", client.user, CHANNEL_IDS, ALLOWED_IDS, agent.model)

    @client.event
    async def on_message(msg: discord.Message) -> None:
        if msg.author.bot or msg.author.id not in ALLOWED_IDS:
            return
        ch = msg.channel
        parent_id = ch.parent_id if isinstance(ch, discord.Thread) else None
        if ch.id not in CHANNEL_IDS and parent_id not in CHANNEL_IDS:
            return
        text = (msg.content or "").strip()
        if not text or text.startswith("!"):
            return  # commandes des autres bots
        low = text.lower().strip("` ")
        if low in ("aide", "help", "?"):
            await ch.send(HELP)
            return
        if low == "reset":
            n = history_clear(str(ch.id))
            await ch.send(f"Historique oublié ({n} messages).")
            return
        if low == "usage":
            i, o = usage_today()
            await ch.send(f"Aujourd'hui : {i:,} tokens en entrée, {o:,} en sortie (plafond {DAILY_CAP:,}) — modèle {agent.model}.")
            return

        is_owner = msg.author.id == OWNER_ID
        author = msg.author.display_name or msg.author.name

        async def confirm(title: str, detail: str) -> bool:
            view = ConfirmView({OWNER_ID, msg.author.id})
            await ch.send(f"🔐 **{title}**\n{detail}\n(5 min pour répondre)", view=view)
            return await view.result

        async def progress(t: str) -> None:
            await ch.send(t)

        async with ch.typing():
            try:
                answer = await agent.run(str(ch.id), text, author, is_owner, authors_desc, confirm, progress)
            except Exception as e:  # noqa: BLE001
                log.exception("agent")
                answer = f"⚠️ Erreur : {type(e).__name__}: {e}"
        for part in chunks(answer):
            await ch.send(part)

    client.run(discord_token(), log_handler=None)


async def run_cli(question: str) -> None:
    agent = Agent()

    async def confirm(title: str, detail: str) -> bool:
        print(f"[confirm refusé en mode test] {title} — {detail}")
        return False

    answer = await agent.run("cli-test", question, "test", True, "test", confirm)
    print(answer)
    i, o = usage_today()
    print(f"\n[{agent.model} — {i:,} in / {o:,} out aujourd'hui]")


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    ap = argparse.ArgumentParser(description="BankWatch dans Discord")
    ap.add_argument("--test", metavar="QUESTION", help="pose une question sans Discord (actions à validation refusées)")
    ns = ap.parse_args()
    if ns.test:
        asyncio.run(run_cli(ns.test))
    else:
        if not CHANNEL_IDS or not ALLOWED_IDS:
            raise SystemExit("BW_CHAT_CHANNEL_IDS et BW_CHAT_OWNER_ID/BW_CHAT_USER_IDS doivent être renseignés dans .env")
        run_discord()


if __name__ == "__main__":
    main()
