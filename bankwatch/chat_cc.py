"""Assistant Discord propulsé par Claude Code (abonnement) au lieu de l'API Claude.

Principe : le bot héberge un serveur MCP HTTP local (outils BankWatch + outil d'approbation) et
lance `claude -p` pour chaque message du salon. Claude Code n'a aucun outil intégré (pas de Bash,
pas d'écriture de fichiers) : seulement les outils de ce serveur. Les outils de lecture sont
pré-autorisés ; une action (sync, run, dismiss…, modification de règle) déclenche l'outil
d'approbation, qui pose la question ✅ dans Discord et répond allow/deny à Claude Code.

Chaque invocation de `claude` porte un en-tête `X-BW-Req` qui permet au serveur MCP de retrouver
qui parle (instance par défaut, droits) et où poser les questions.
"""
from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import shlex
import shutil
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from mcp.server.fastmcp import Context, FastMCP

from . import chat as C
from .config import settings

log = logging.getLogger("bankwatch.chat_cc")

ConfirmFn = Callable[[str, str], Awaitable[bool]]
ProgressFn = Callable[[str], Awaitable[None]]


@dataclass
class Req:
    """Contexte d'une invocation de claude : qui parle, où valider."""

    author_id: int | None
    is_owner: bool
    confirm: ConfirmFn
    progress: ProgressFn | None


REQUESTS: dict[str, Req] = {}

# --------------------------------------------------------------------------- serveur MCP

MCP_PORT = settings.chat_mcp_port
mcp = FastMCP("bankwatch", stateless_http=True, json_response=True, host="127.0.0.1", port=MCP_PORT,
              instructions="Outils BankWatch et Budget du foyer. Les actions passent par une validation Discord.")

# outils autorisés sans question ; tout le reste de ce serveur passe par `approuver`
READ_TOOLS = ("etat", "bankwatch_lire", "bankwatch_sql", "budget_sql", "fichier_lire", "memoire_ajouter")
ACTION_TOOLS = ("bankwatch_action", "regle_ecrire")


def _req(ctx: Context) -> Req:
    request = ctx.request_context.request
    rid = request.headers.get("x-bw-req") if request is not None else None
    req = REQUESTS.get(rid or "")
    if req is None:
        raise C.Refused("requête inconnue (en-tête X-BW-Req absent ou périmé)")
    return req


def _inst(req: Req, instance: str | None) -> C.Instance:
    return C.instance_for(req.author_id, instance)


def _foreign(req: Req, inst: C.Instance) -> bool:
    return C.MULTI and req.author_id is not None and req.author_id not in inst.users


def _guard(fn):
    """Transforme Refused / erreurs SQL en texte pour le modèle, comme dans chat.Agent.tool."""
    @functools.wraps(fn)
    async def wrapper(*a, **kw):
        try:
            return await fn(*a, **kw)
        except C.Refused as e:
            return f"REFUSÉ : {e}"
        except (ValueError, sqlite3.Error) as e:
            return f"ERREUR : {e}"
        except Exception as e:  # noqa: BLE001
            log.exception("outil %s", fn.__name__)
            return f"ERREUR interne : {type(e).__name__}: {e}"
    return wrapper


_INSTANCE_DOC = ("instance BankWatch visée (" + ", ".join(f"{k} = {v.label}" for k, v in C.INSTANCES.items())
                 + ") ; par défaut celle de la personne qui parle") if C.MULTI else "ignoré (une seule instance)"


@mcp.tool(structured_output=False, description="Photo rapide de BankWatch : solde lu et estimé par compte, quota DSP2 consommé aujourd'hui, "
                      "alertes récentes, dernier passage. À appeler en premier pour « où j'en suis ? ».")
@_guard
async def etat(ctx: Context, instance: str | None = None) -> str:
    req = _req(ctx)
    return await asyncio.to_thread(C.t_etat, _inst(req, instance))


@mcp.tool(structured_output=False, description="Commande du CLI bankwatch en lecture seule, sans validation : accounts, alerts [--all], forecast, "
                      "watch, institutions <nom>, report --dry-run, user list, token list. "
                      "Pour sync, run, dismiss, dedupe, recategorize, ignore, report, link… utilise bankwatch_action.")
@_guard
async def bankwatch_lire(ctx: Context, args: str, instance: str | None = None) -> str:
    req = _req(ctx)
    argv = shlex.split(args)
    if argv and argv[0] == "bankwatch":
        argv = argv[1:]
    if C.classify_cmd(argv) != "free":
        raise C.Refused("cette commande modifie des données ou consomme du quota : passe par bankwatch_action")
    return await C.run_bankwatch(argv, _inst(req, instance))


@mcp.tool(structured_output=False, description="Commande du CLI bankwatch qui agit (validation ✅ demandée automatiquement à l'utilisateur dans "
                      "Discord) : sync (consomme du quota DSP2 : 1 accès transactions par compte, +1 si relecture de solde), "
                      "run (sync + contrôles + alertes), check [--no-notify], dismiss <n°>, dedupe, recategorize, ignore <id>, "
                      "report --days N, notify-test, webhook-test, link <nom-court> \"<ASPSP>\" [--business] (lien de "
                      "consentement bancaire à transmettre). Ne l'appelle que si l'utilisateur a demandé l'action.")
@_guard
async def bankwatch_action(ctx: Context, args: str, instance: str | None = None) -> str:
    req = _req(ctx)
    inst = _inst(req, instance)
    argv = shlex.split(args)
    if argv and argv[0] == "bankwatch":
        argv = argv[1:]
    mode = C.classify_cmd(argv)
    if mode == "free":
        return await C.run_bankwatch(argv, inst)
    if _foreign(req, inst) and not req.is_owner:
        raise C.Refused(f"action sur {inst.label} : seul le propriétaire peut la lancer")
    if req.progress:
        tag = f"[{inst.label}] " if C.MULTI else ""
        await req.progress(f"⏳ {tag}`bankwatch {' '.join(argv)}` en cours…")
    return await C.run_bankwatch(argv, inst)


@mcp.tool(structured_output=False, description="Requête SELECT en lecture seule sur la base BankWatch (SQLite). Tables : accounts(id,bank,name,iban,"
                      "balance,balance_date,last_sync,ignored), transactions(id,account_id,booking_date,value_date,amount,"
                      "counterparty,label,category,status[booked|pending],first_seen ; NE PAS sélectionner la colonne raw), "
                      "alerts(id,kind,severity,title,message,amount,tx_ids,created_at,sent_at,dismissed), meta(key,value). "
                      "Montants : débit négatif. Dates ISO. 200 lignes max.")
@_guard
async def bankwatch_sql(ctx: Context, sql: str, instance: str | None = None) -> str:
    req = _req(ctx)
    return await asyncio.to_thread(C.t_bankwatch_sql, sql, _inst(req, instance))


@mcp.tool(structured_output=False, description="Requête SELECT en lecture seule sur la base de l'appli Budget du foyer (SQLite), commune aux deux "
                      "instances. Tables : docs(path,data JSON) avec path 'budget/config' (postes : id, nom, type pro|revenu|"
                      "fixe|variable…, montant, compte), 'budget/jours', 'mois/AAAA-MM' (prev/reel par poste) ; transactions("
                      "id,account_uid,booking_date,amount,label,counterparty,status[a_classer|classe|…],poste_id,rule_id,"
                      "category,internal) ; rules(id,pattern,sens,account_uid,action,poste_id,priority) ; bank_accounts(uid,"
                      "bank,name,iban,role,session_id,last_sync,active) — session_id = bankwatch:<instance>. "
                      "Utilise json_extract() sur docs.data.")
@_guard
async def budget_sql(ctx: Context, sql: str) -> str:
    _req(ctx)
    return await asyncio.to_thread(C.t_budget_sql, sql)


@mcp.tool(structured_output=False, description="Lit un fichier du dépôt BankWatch de l'instance : rules/*.json (categories, watch, forecast, "
                      "overdraft, webhooks), docs/*.md, README.md.")
@_guard
async def fichier_lire(ctx: Context, chemin: str, instance: str | None = None) -> str:
    req = _req(ctx)
    return C.t_fichier_lire(chemin, _inst(req, instance))


@mcp.tool(structured_output=False, description="Remplace un fichier de règles (categories.json, watch.json, forecast.json, overdraft.json) de "
                      "l'instance par le JSON fourni (validation ✅ du propriétaire demandée automatiquement). Toujours lire "
                      "le fichier avant, modifier le minimum, renvoyer le JSON complet. Sauvegarde automatique. "
                      "resume : une phrase décrivant le changement, montrée dans la demande de validation.")
@_guard
async def regle_ecrire(ctx: Context, fichier: str, contenu: str, resume: str, instance: str | None = None) -> str:
    req = _req(ctx)
    if not req.is_owner:
        raise C.Refused("seul le propriétaire peut modifier les règles")
    json.loads(contenu)
    return C._rule_write(fichier, contenu, _inst(req, instance))


@mcp.tool(structured_output=False, description="Ajoute une note durable à la mémoire de l'assistant (relue à chaque message). Pour ce que "
                      "l'utilisateur demande explicitement de retenir (« retiens que… »).")
@_guard
async def memoire_ajouter(ctx: Context, note: str) -> str:
    _req(ctx)
    return C.t_memoire_ajouter(note)


def _describe(tool_name: str, tool_input: dict[str, Any], req: Req) -> tuple[str, str]:
    """(titre, détail) de la demande de validation, d'après l'outil et ses arguments."""
    inst = C.instance_for(req.author_id, tool_input.get("instance"))
    tag = f"[{inst.label}] " if C.MULTI else ""
    short = tool_name.split("__")[-1]
    if short == "bankwatch_action":
        return f"{tag}bankwatch {tool_input.get('args', '')}", "Exécuter cette commande sur le serveur ?"
    if short == "regle_ecrire":
        return f"{tag}Modifier rules/{tool_input.get('fichier', '?')}", tool_input.get("resume", "") or ""
    return f"{tag}{short}", json.dumps(tool_input, ensure_ascii=False)[:300]


@mcp.tool(structured_output=False, description="Outil interne de Claude Code : demande de permission. Ne pas appeler directement.")
async def approuver(ctx: Context, tool_name: str, input: dict[str, Any], tool_use_id: str | None = None) -> str:
    """Appelé par Claude Code (--permission-prompt-tool) avant un outil non pré-autorisé.
    Pose la question ✅ dans Discord et renvoie {"behavior": "allow"|"deny"} en JSON."""
    try:
        req = _req(ctx)
    except C.Refused as e:
        return json.dumps({"behavior": "deny", "message": str(e)})
    short = tool_name.split("__")[-1]
    if short not in ACTION_TOOLS:
        return json.dumps({"behavior": "deny", "message": f"outil {tool_name} non autorisé depuis Discord"})
    if short == "regle_ecrire" and not req.is_owner:
        return json.dumps({"behavior": "deny", "message": "seul le propriétaire peut modifier les règles"})
    try:
        title, detail = _describe(tool_name, input, req)
    except C.Refused as e:
        return json.dumps({"behavior": "deny", "message": str(e)})
    ok = await req.confirm(title, detail)
    if ok:
        return json.dumps({"behavior": "allow", "updatedInput": input})
    return json.dumps({"behavior": "deny", "message": "non validé par l'utilisateur (bouton ✅ refusé ou délai dépassé)"})


async def serve_mcp() -> None:
    """Lance le serveur MCP HTTP dans la boucle courante (à côté de discord.py)."""
    import uvicorn

    app = mcp.streamable_http_app()
    config = uvicorn.Config(app, host="127.0.0.1", port=MCP_PORT, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    await server.serve()


# --------------------------------------------------------------------------- sessions (une par salon)


def _sessions_table() -> None:
    with C._chat_conn() as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS cc_sessions (scope TEXT PRIMARY KEY, session_id TEXT NOT NULL, "
                     "updated_at TEXT NOT NULL DEFAULT (datetime('now')))")


def session_get(scope: str) -> str | None:
    _sessions_table()
    with C._chat_conn() as conn:
        row = conn.execute("SELECT session_id FROM cc_sessions WHERE scope=?", (scope,)).fetchone()
    return row[0] if row else None


def session_set(scope: str, session_id: str) -> None:
    _sessions_table()
    with C._chat_conn() as conn:
        conn.execute("INSERT INTO cc_sessions(scope, session_id, updated_at) VALUES (?, ?, datetime('now')) "
                     "ON CONFLICT(scope) DO UPDATE SET session_id=excluded.session_id, updated_at=excluded.updated_at",
                     (scope, session_id))


def session_clear(scope: str) -> None:
    _sessions_table()
    with C._chat_conn() as conn:
        conn.execute("DELETE FROM cc_sessions WHERE scope=?", (scope,))


# --------------------------------------------------------------------------- agent


def claude_bin() -> str:
    b = settings.chat_cc_bin
    found = shutil.which(b) if not os.path.isabs(b) else (b if os.path.exists(b) else None)
    if not found:
        raise RuntimeError(f"Claude Code introuvable ({b}) : installe-le (npm i -g @anthropic-ai/claude-code) et connecte-toi (claude /login)")
    return found


def system_prompt(authors: str) -> str:
    extra = ("\nOutils : `etat`, `bankwatch_lire` (commandes de lecture), `bankwatch_action` (commandes qui agissent — "
             "la validation ✅ est demandée automatiquement à l'utilisateur, tu n'as pas à la demander toi-même, mais "
             "n'appelle cet outil que si l'action a été explicitement demandée), `bankwatch_sql`, `budget_sql`, "
             "`fichier_lire`, `regle_ecrire`, `memoire_ajouter`. Si un outil répond REFUSÉ ou qu'une permission est "
             "refusée, dis-le simplement et propose la suite. Tu n'as aucun accès au système de fichiers ni au shell : "
             "ne prétends jamais avoir exécuté autre chose que ces outils.\n")
    return C.system_prompt(authors) + extra


ALLOWED = [f"mcp__bankwatch__{t}" for t in READ_TOOLS]
DISALLOWED = ["Bash", "Edit", "Write", "NotebookEdit", "Read", "Glob", "Grep", "WebFetch",
              "WebSearch", "Task", "Agent", "TodoWrite", "KillShell", "BashOutput", "Skill", "EnterPlanMode",
              "ExitPlanMode", "AskUserQuestion"]


class CCAgent:
    """Même contrat que chat.Agent.run, mais chaque tour est un `claude -p`."""

    def __init__(self) -> None:
        self.bin = claude_bin()
        self.model = settings.chat_cc_model
        self.cwd = C.ROOT / "data" / "claude-cwd"
        self.cwd.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock(self, scope: str) -> asyncio.Lock:
        return self._locks.setdefault(scope, asyncio.Lock())

    async def run(self, scope: str, text: str, author: str, is_owner: bool, authors: str,
                  confirm: ConfirmFn, progress: ProgressFn | None = None, author_id: int | None = None) -> str:
        rid = uuid.uuid4().hex
        REQUESTS[rid] = Req(author_id, is_owner, confirm, progress)
        try:
            async with self._lock(scope):
                return await self._run(rid, scope, text, author, authors)
        finally:
            REQUESTS.pop(rid, None)

    async def _run(self, rid: str, scope: str, text: str, author: str, authors: str) -> str:
        mcp_cfg = json.dumps({"mcpServers": {"bankwatch": {
            "type": "http", "url": f"http://127.0.0.1:{MCP_PORT}/mcp", "headers": {"X-BW-Req": rid}}}})
        prev = session_get(scope)
        new_sid = None if prev else str(uuid.uuid4())
        args = [self.bin, "-p", f"[{author}] {text}", "--output-format", "json",
                "--system-prompt", system_prompt(authors),
                "--model", self.model, "--max-turns", str(settings.chat_cc_max_turns),
                "--strict-mcp-config", "--mcp-config", mcp_cfg,
                "--permission-prompt-tool", "mcp__bankwatch__approuver",
                "--allowedTools", *ALLOWED, "--disallowedTools", *DISALLOWED,
                "--setting-sources", "", "--tools", ""]
        args += ["--resume", prev] if prev else ["--session-id", new_sid]
        # ni clé API (sinon elle prime sur l'abonnement), ni variables BankWatch
        env = {k: v for k, v in os.environ.items()
               if not k.startswith(("BW_", "ANTHROPIC_", "CLAUDE_CODE_")) and k not in ("ANTHROPIC_API_KEY",)}
        env.update(TERM="dumb", NO_COLOR="1", CI="1")
        C.history_add(scope, "user", text, author)
        proc = await asyncio.create_subprocess_exec(*args, cwd=str(self.cwd), env=env,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        try:
            out, err = await asyncio.wait_for(proc.communicate(), timeout=settings.chat_cc_timeout)
        except asyncio.TimeoutError:
            proc.kill()
            return f"⏱️ Claude Code n'a pas répondu en {settings.chat_cc_timeout}s ; j'ai interrompu."
        raw = out.decode("utf-8", "replace").strip()
        try:
            data = json.loads(raw)
        except ValueError:
            log.error("sortie claude illisible (code %s) : %s / %s", proc.returncode, raw[:500], err.decode("utf-8", "replace")[:500])
            if prev and ("No conversation found" in raw or "No conversation found" in err.decode("utf-8", "replace")):
                session_clear(scope)
                return await self._run(rid, scope, text, author, authors)
            return f"⚠️ Claude Code a échoué (code {proc.returncode}) : {(err.decode('utf-8', 'replace') or raw)[:400]}"
        sid = data.get("session_id") or new_sid or prev
        if sid:
            session_set(scope, sid)
        usage = data.get("usage") or {}
        C.usage_add(int(usage.get("input_tokens", 0) or 0) + int(usage.get("cache_read_input_tokens", 0) or 0)
                    + int(usage.get("cache_creation_input_tokens", 0) or 0), int(usage.get("output_tokens", 0) or 0))
        final = (data.get("result") or "").strip()
        if data.get("is_error") or data.get("subtype") not in (None, "success"):
            sub = data.get("subtype")
            if sub == "error_max_turns":
                final = (final + "\n\n" if final else "") + "Je me suis arrêté : trop d'étapes d'outils pour une seule question."
            elif not final:
                final = f"⚠️ Claude Code : {sub or 'erreur'} — {str(data.get('error') or data.get('api_error_status') or '')[:300]}"
        final = final or "(pas de réponse)"
        C.history_add(scope, "assistant", final)
        return final
