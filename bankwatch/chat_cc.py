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
import hashlib
import json
import logging
import os
import shlex
import shutil
import sqlite3
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from . import agent as A
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
    author: str = ""
    channel_id: int | None = None
    deadline: float = 0.0  # échéance de l'invocation claude (horloge de la boucle) ; un outil long la repousse
    approved: set[str] = field(default_factory=set)  # validations ✅ accordées, consommées par les outils d'agent


REQUESTS: dict[str, Req] = {}

# --------------------------------------------------------------------------- serveur MCP

MCP_PORT = settings.chat_mcp_port
AGENT_MCP_PORT = settings.chat_agent_mcp_port
# json_response=False : réponses en flux SSE. Claude Code abandonne au bout de 60 s une réponse JSON simple,
# alors qu'une validation ✅, un `run` ou une session d'agent durent plus longtemps (voir _alive).
mcp = FastMCP("bankwatch", stateless_http=True, json_response=False, host="127.0.0.1", port=MCP_PORT,
              instructions="Outils BankWatch et Budget du foyer. Les actions passent par une validation Discord.")

# outils autorisés sans question ; tout le reste de ce serveur passe par `approuver`
READ_TOOLS = ("etat", "bankwatch_lire", "bankwatch_sql", "budget_sql", "fichier_lire", "memoire_ajouter")
AGENT_TOOLS = ("agent_code", "agent_deployer")  # réservés aux comptes autorisés, ✅ à chaque appel
AGENT_IDS = C._ids(settings.chat_agent_user_ids)  # comptes autorisés en plus du propriétaire
ACTION_TOOLS = ("bankwatch_action", "regle_ecrire", *AGENT_TOOLS)


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


async def _alive(ctx: Context, aw: Awaitable[Any], every: float = 15.0) -> Any:
    """Attend `aw` en envoyant un signe de vie périodique sur le flux de la réponse (appels longs)."""
    task = asyncio.ensure_future(aw)
    n = 0
    try:
        while True:
            done, _ = await asyncio.wait({task}, timeout=every)
            if done:
                return task.result()
            n += 1
            try:
                await ctx.report_progress(n, None)
            except Exception:  # noqa: BLE001 — client sans jeton de progression ou flux fermé : sans conséquence
                pass
    finally:
        if not task.done():
            task.cancel()


def _agent_ok(req: Req) -> bool:
    """Qui peut utiliser les outils d'agent : le propriétaire et les comptes de BW_CHAT_AGENT_USER_IDS."""
    return req.is_owner or (req.author_id is not None and req.author_id in AGENT_IDS)


def _akey(short: str, tool_input: dict[str, Any]) -> str:
    keys = ("depot", "prompt") if short == "agent_code" else ("depot",)
    blob = json.dumps([str(tool_input.get(k, "")) for k in keys], ensure_ascii=False)
    return short + ":" + hashlib.sha256(blob.encode()).hexdigest()


def _need_approval(req: Req, short: str, tool_input: dict[str, Any]) -> None:
    """Les outils d'agent vérifient eux-mêmes qu'un ✅ a été donné pour CET appel (mêmes arguments)."""
    key = _akey(short, tool_input)
    if key not in req.approved:
        raise C.Refused("pas de validation ✅ pour cet appel précis (elle est demandée automatiquement quand tu appelles l'outil)")
    req.approved.discard(key)


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


@mcp.tool(structured_output=False, description="Commande du CLI bankwatch en lecture seule, sans validation : accounts, alerts [--all] [--active], forecast, "
                      "watch, institutions <nom>, report --dry-run, user list, token list. "
                      "Pour sync, run, resolve, reopen, dismiss, dedupe, recategorize, ignore, report, link… utilise bankwatch_action.")
@_guard
async def bankwatch_lire(ctx: Context, args: str, instance: str | None = None) -> str:
    req = _req(ctx)
    argv = shlex.split(args)
    if argv and argv[0] == "bankwatch":
        argv = argv[1:]
    if C.classify_cmd(argv) != "free":
        raise C.Refused("cette commande modifie des données ou consomme du quota : passe par bankwatch_action")
    return await _alive(ctx, C.run_bankwatch(argv, _inst(req, instance)))


@mcp.tool(structured_output=False, description="Commande du CLI bankwatch qui agit (validation ✅ demandée automatiquement à l'utilisateur dans "
                      "Discord) : sync (consomme du quota DSP2 : 1 accès transactions par compte, +1 si relecture de solde), "
                      "run (sync + contrôles + alertes), check [--no-notify], resolve <n°> (« c'est bon » : alerte réglée, plus active), reopen <n°>, "
                      "dismiss <n°> (fausse alerte, ne renaît pas), dedupe, recategorize, ignore <id>, "
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
    return await _alive(ctx, C.run_bankwatch(argv, inst))


@mcp.tool(structured_output=False, description="Requête SELECT en lecture seule sur la base BankWatch (SQLite). Tables : accounts(id,bank,name,iban,"
                      "balance,balance_date,last_sync,ignored), transactions(id,account_id,booking_date,value_date,amount,"
                      "counterparty,label,category,status[booked|pending],first_seen ; NE PAS sélectionner la colonne raw), "
                      "alerts(id,kind,severity,title,message,amount,tx_ids,created_at,sent_at,dismissed,resolved_at,resolved_by[auto|nom] ; "
                      "active = dismissed=0 AND resolved_at IS NULL), meta(key,value). "
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


def _who(req: Req, tool_input: dict[str, Any]) -> dict[str, Any]:
    return {"auteur_id": req.author_id, "auteur": req.author, "depot": str(tool_input.get("depot", ""))[:60],
            "prompt": tool_input.get("prompt")}


async def _detached(req: Req, coro: Awaitable[str], ctx: Context) -> str:
    """Un travail long ne doit pas mourir avec l'appel MCP : si l'appel est coupé (délai côté Claude Code,
    assistant interrompu), le travail va au bout et son compte rendu est posté dans le salon."""
    task = asyncio.ensure_future(coro)
    try:
        return await _alive(ctx, asyncio.shield(task))
    except asyncio.CancelledError:
        def late(t: asyncio.Task) -> None:
            if req.progress and not t.cancelled():
                text = f"⚠️ {t.exception()}" if t.exception() else str(t.result())
                asyncio.ensure_future(req.progress(text[:1900]))
        task.add_done_callback(late)
        raise


@mcp.tool(structured_output=False, description="Délègue une modification de CODE à une session Claude Code séparée, enfermée dans un bac à sable. "
                      "Réservé au propriétaire du bot et aux comptes autorisés ; une validation ✅ affichant le dépôt et le prompt est demandée "
                      "automatiquement à chaque appel. depot : un nom de la liste blanche (" + (", ".join(A.depot_names()) or "aucun configuré")
                      + "), jamais un chemin. prompt : la demande complète et autonome, en français — l'agent ne voit pas cette "
                      f"conversation ({settings.chat_agent_max_prompt} caractères max, montré tel quel dans la validation). "
                      "L'agent travaille dans un clone, sur une branche agent/<date>-<slug>, un commit par étape, avec les seuls "
                      "outils Read/Grep/Glob/Edit/Write et git/pytest/ruff, sans réseau ; rien n'est fusionné, poussé ni "
                      f"redémarré. Dure jusqu'à {settings.chat_agent_timeout // 60} min. Renvoie les commits, les fichiers modifiés et le "
                      "résultat des tests : relaie-les fidèlement. Ne l'appelle que si un compte autorisé demande explicitement de "
                      "modifier du code.")
@_guard
async def agent_code(ctx: Context, depot: str, prompt: str) -> str:
    req = _req(ctx)
    args = {"depot": depot, "prompt": prompt}
    if not _agent_ok(req):
        A.journal_add("agent_code", "refusé", detail="compte non autorisé", **_who(req, args))
        raise C.Refused("agent_code est réservé aux comptes autorisés")
    _need_approval(req, "agent_code", args)
    req.deadline = max(req.deadline, asyncio.get_running_loop().time() + settings.chat_agent_timeout + 480)
    if req.progress:
        await req.progress(f"🛠️ Agent lancé sur `{depot}` (jusqu'à {settings.chat_agent_timeout // 60} min). Compte rendu à la fin.")
    return await _detached(req, A.agent_code(depot, prompt, author_id=req.author_id, author=req.author), ctx)


@mcp.tool(structured_output=False, description="Déploie la dernière branche produite par agent_code pour ce dépôt : fusion dans la branche "
                      "principale, redémarrage des services, test de santé, et retour automatique au commit précédent si le "
                      "test échoue. Réservé au propriétaire du bot et aux comptes autorisés ; validation ✅ séparée, demandée automatiquement, qui "
                      "affiche la branche, les commits et les services. depot : un nom de la liste blanche ("
                      + (", ".join(A.depot_names()) or "aucun configuré") + "). L'outil rend la main dès le lancement ; le compte "
                      "rendu est posté dans le salon par le script de déploiement (le bot peut redémarrer entre-temps). "
                      "Ne l'appelle que si un compte autorisé demande explicitement de déployer.")
@_guard
async def agent_deployer(ctx: Context, depot: str) -> str:
    req = _req(ctx)
    args = {"depot": depot}
    if not _agent_ok(req):
        A.journal_add("agent_deployer", "refusé", detail="compte non autorisé", **_who(req, args))
        raise C.Refused("agent_deployer est réservé aux comptes autorisés")
    _need_approval(req, "agent_deployer", args)
    return await A.agent_deployer(depot, author_id=req.author_id, author=req.author, channel_id=req.channel_id)


async def _describe_agent(short: str, tool_input: dict[str, Any]) -> tuple[str, str]:
    """(titre, détail) de la validation d'un outil d'agent ; lève Refused si l'appel est irrecevable."""
    if short == "agent_code":
        d = A.get_depot(str(tool_input.get("depot", "")))
        prompt = A.check_prompt(str(tool_input.get("prompt", ""))).replace("`" * 3, "'''")
        return (f"agent_code → dépôt « {d.name} »",
                f"Prompt transmis à l'agent :\n```\n{prompt}\n```\nSession Claude Code séparée, en bac à sable, sur une branche "
                f"`agent/…` ; {settings.chat_agent_timeout // 60} min max ; ni réseau, ni push, ni fusion.")
    plan = await A.deploy_plan(str(tool_input.get("depot", "")))
    return f"agent_deployer → dépôt « {plan['depot'].name} »", A.plan_text(plan)


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
    agent = short in AGENT_TOOLS
    if agent and not _agent_ok(req):
        A.journal_add(short, "refusé", detail="compte non autorisé", **_who(req, input))
        return json.dumps({"behavior": "deny", "message": f"{short} est réservé aux comptes autorisés : indisponible pour cette personne"})
    try:
        title, detail = await _describe_agent(short, input) if agent else _describe(tool_name, input, req)
    except C.Refused as e:
        if agent:
            A.journal_add(short, "refusé", detail=str(e), **_who(req, input))
        return json.dumps({"behavior": "deny", "message": str(e)})
    ok = await _alive(ctx, req.confirm(title, detail))
    if ok:
        if agent:
            req.approved.add(_akey(short, input))
        return json.dumps({"behavior": "allow", "updatedInput": input})
    if agent:
        A.journal_add(short, "non validé", detail="bouton ✅ refusé ou délai dépassé", **_who(req, input))
    return json.dumps({"behavior": "deny", "message": "non validé par l'utilisateur (bouton ✅ refusé ou délai dépassé)"})


# --------------------------------------------------------------------------- serveur MCP de la session d'agent

mcp_depot = FastMCP("depot", stateless_http=True, json_response=False, host="127.0.0.1", port=AGENT_MCP_PORT,
                    instructions="git, pytest et ruff dans le dépôt de travail, exécutés sans réseau.")


def _run_of(ctx: Context) -> A.Run:
    request = ctx.request_context.request
    rid = request.headers.get("x-bw-req") if request is not None else None
    run = A.RUNS.get(rid or "")
    if run is None:
        raise C.Refused("session d'agent inconnue ou terminée")
    return run


@mcp_depot.tool(name="git", structured_output=False, description="git dans le dépôt de travail. args = sous-commande et arguments, par ex. "
                "`status --short`, `diff`, `log --oneline -10`, `add chemin/fichier.py`, `commit -m \"zone : changement\"`. "
                "Sous-commandes disponibles : " + ", ".join(A.GIT_OK) + ".")
@_guard
async def depot_git(ctx: Context, args: str) -> str:
    return await A.tool_git(_run_of(ctx), args)


@mcp_depot.tool(name="pytest", structured_output=False, description="Lance pytest dans le dépôt de travail (sans réseau, 5 min max). "
                "args = arguments de pytest, par ex. `-q`, `tests/test_x.py -q`, `-k motif -x`. Par défaut : -q.")
@_guard
async def depot_pytest(ctx: Context, args: str = "") -> str:
    return await _alive(ctx, A.tool_pytest(_run_of(ctx), args))


@mcp_depot.tool(name="ruff", structured_output=False, description="Lance ruff dans le dépôt de travail. args = `check [chemins]`, "
                "`check --fix [chemins]` ou `format [chemins]`. Par défaut : check .")
@_guard
async def depot_ruff(ctx: Context, args: str = "") -> str:
    return await _alive(ctx, A.tool_ruff(_run_of(ctx), args))


@mcp_depot.tool(name="approuver", structured_output=False, description="Outil interne de Claude Code : demande de permission. Ne pas appeler directement.")
async def depot_approuver(ctx: Context, tool_name: str, input: dict[str, Any], tool_use_id: str | None = None) -> str:
    """Dans une session d'agent, personne ne valide : tout ce qui n'est pas pré-autorisé est refusé."""
    return json.dumps({"behavior": "deny", "message": "non autorisé dans cette session : seuls Read, Grep, Glob, Edit, Write "
                       "(dans le dépôt de travail) et les outils git, pytest, ruff sont disponibles"})


async def _serve(server_def: FastMCP, port: int) -> None:
    import uvicorn

    config = uvicorn.Config(server_def.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    server.install_signal_handlers = lambda: None  # type: ignore[method-assign]
    await server.serve()


async def serve_mcp() -> None:
    """Lance les serveurs MCP HTTP dans la boucle courante (à côté de discord.py) : celui de l'assistant
    et celui, séparé, des commandes d'une session d'agent."""
    await asyncio.gather(_serve(mcp, MCP_PORT), _serve(mcp_depot, AGENT_MCP_PORT))


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
             "ne prétends jamais avoir exécuté autre chose que ces outils.\n"
             "Code (réservé à " + ", ".join(f"<@{i}>" for i in sorted({C.OWNER_ID} | AGENT_IDS))
             + ", sur demande explicite) : `agent_code(depot, prompt)` confie "
             "une modification de code à un agent séparé qui travaille sur une branche, sans rien déployer ; "
             "`agent_deployer(depot)` fusionne ensuite cette branche et redémarre le service. Dépôts autorisés : "
             + (", ".join(A.depot_names()) or "aucun") + ". Rédige pour l'agent un prompt complet et fidèle à la demande, sans "
             "rien y ajouter de ton cru ; ne déploie jamais dans la foulée sans qu'on te le demande ; pour toute autre "
             "personne, réponds que ces deux outils ne lui sont pas ouverts.\n")
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
        REQUESTS[rid] = Req(author_id, is_owner, confirm, progress, author=author,
                            channel_id=int(scope) if scope.isdigit() else None)
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
        env.update(TERM="dumb", NO_COLOR="1", CI="1", MCP_TOOL_TIMEOUT=str((settings.chat_agent_timeout + 600) * 1000))
        C.history_add(scope, "user", text, author)
        proc = await asyncio.create_subprocess_exec(*args, cwd=str(self.cwd), env=env,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        # échéance repoussable : agent_code la prolonge de la durée d'une session d'agent
        loop, req = asyncio.get_running_loop(), REQUESTS[rid]
        req.deadline = loop.time() + settings.chat_cc_timeout
        comm = asyncio.ensure_future(proc.communicate())
        while not comm.done():
            left = req.deadline - loop.time()
            if left <= 0:
                proc.kill()
                await comm
                return "⏱️ Claude Code n'a pas répondu à temps ; j'ai interrompu."
            await asyncio.wait({comm}, timeout=min(left, 30))
        out, err = comm.result()
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
