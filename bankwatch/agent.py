"""Délégation de modifications de code depuis Discord : outils MCP `agent_code` et `agent_deployer`.

Principe
- `agent_code(depot, prompt)` : `depot` est une clé de la liste blanche (fichier JSON hors git), jamais
  un chemin. Le bot clone la branche principale du dépôt dans un dossier de travail, y crée la branche
  `agent/<date>-<slug>` et lance une session `claude -p` SÉPARÉE de celle de l'assistant, enfermée par
  bubblewrap :
    * elle ne voit que le clone : ni le dépôt en service, ni `.env`, ni `data/`, ni le reste de $HOME ;
    * outils intégrés limités à Read, Grep, Glob, Edit, Write ;
    * git, pytest et ruff passent par trois outils MCP que le bot exécute dans un second bac à sable
      SANS réseau (`--unshare-net`) : pas de sudo, pas de systemctl, aucune écriture hors du clone.
  Le dépôt en service n'est jamais modifié pendant la session. À la fin, la branche y est rapatriée
  (bundle git = simples données), sans fusion ni push, et le compte rendu (commits, fichiers, tests)
  est calculé par le bot, pas par l'agent.
- `agent_deployer(depot)` : fusionne la dernière branche d'agent, redémarre les services et lance les
  tests de santé depuis une unité systemd transitoire (`deploy/agent_deploy.py`), car le déploiement
  peut redémarrer le bot lui-même. Test de santé en échec → retour au commit précédent.

Chaque appel est journalisé dans la table `agent_journal` de la base du chat.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import time
import unicodedata
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import chat as C
from .config import settings

log = logging.getLogger("bankwatch.agent")

HOME = Path.home()
BWRAP = shutil.which("bwrap") or "/usr/bin/bwrap"
SANDBOX_HOME_CFG = str(HOME / ".claude-agent")  # CLAUDE_CONFIG_DIR vu depuis le bac à sable
GIT_OK = ("status", "diff", "log", "show", "add", "commit", "rm", "mv", "restore")
ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
ProgressFn = Callable[[str], Awaitable[None]]


def _abs(p: str | Path) -> Path:
    q = Path(p).expanduser()
    return q if q.is_absolute() else C.ROOT / q


# --------------------------------------------------------------------------- liste blanche


@dataclass(frozen=True)
class Depot:
    name: str
    repo: Path                      # dépôt en service (jamais modifié par agent_code)
    venv: Path                      # venv qui fournit python -m pytest et ruff (monté en lecture seule)
    main: str                       # branche de référence
    services: tuple[str, ...]       # unités redémarrées par agent_deployer
    health: tuple[dict, ...]        # tests de santé : {"url": …} | {"unit": …} | {"port": …}
    followers: tuple[dict, ...]     # autres clones alignés au déploiement : {"repo", "services", "health"}
    deps: tuple[str, ...]           # fichiers de dépendances : s'ils changent, pas de déploiement automatique

    @property
    def restarts_bot(self) -> bool:
        return any(s.split(".")[0] == "bankwatch-chat" for s in self.services)


def load_depots() -> dict[str, Depot]:
    """data/agent_depots.json : {"<nom>": {"repo": "/chemin", "venv": "…", "services": […], "health": […],
    "suivants": […], "deps": […], "branche": "main"}}. Relu à chaque appel ; sans fichier, aucun dépôt."""
    path = _abs(settings.chat_agent_depots_file)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    out: dict[str, Depot] = {}
    for name, spec in raw.items():
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,40}", name):
            raise C.Refused(f"liste blanche : nom de dépôt invalide « {name} »")
        repo = Path(spec["repo"]).expanduser().resolve()
        out[name] = Depot(
            name=name, repo=repo, venv=Path(spec.get("venv") or repo / ".venv").expanduser(),
            main=spec.get("branche", "main"), services=tuple(spec.get("services", [])),
            health=tuple(spec.get("health", [])), followers=tuple(spec.get("suivants", [])),
            deps=tuple(spec.get("deps", ["pyproject.toml", "requirements.txt"])))
    return out


def depot_names() -> list[str]:
    try:
        return sorted(load_depots())
    except Exception:  # noqa: BLE001 — sert à la description des outils, ne doit pas empêcher le bot de démarrer
        log.exception("liste blanche des dépôts illisible")
        return []


def get_depot(name: str) -> Depot:
    depots = load_depots()
    key = (name or "").strip().lower()
    if key not in depots:
        raise C.Refused(f"dépôt « {name} » hors liste blanche (autorisés : {', '.join(sorted(depots)) or 'aucun'})")
    d = depots[key]
    if not (d.repo / ".git").is_dir():
        raise C.Refused(f"{d.name} : le dossier configuré n'est pas un dépôt git")
    return d


def check_prompt(prompt: str) -> str:
    p = (prompt or "").strip()
    if not p:
        raise C.Refused("prompt vide")
    if len(p) > settings.chat_agent_max_prompt:
        raise C.Refused(f"prompt trop long ({len(p)} caractères, maximum {settings.chat_agent_max_prompt}) : "
                        "il doit tenir en entier dans la demande de validation")
    return p


# --------------------------------------------------------------------------- journal


JOURNAL_COLS = ("outil", "auteur_id", "auteur", "depot", "prompt", "statut", "branche", "base", "tete",
                "fichiers", "tests", "tests_ok", "detail", "duree_s", "deploye_le")


def _journal() -> Any:
    conn = C._chat_conn()
    conn.execute(
        """CREATE TABLE IF NOT EXISTS agent_journal(
               id INTEGER PRIMARY KEY AUTOINCREMENT,
               ts TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
               outil TEXT NOT NULL, auteur_id TEXT, auteur TEXT, depot TEXT, prompt TEXT,
               statut TEXT NOT NULL, branche TEXT, base TEXT, tete TEXT, fichiers TEXT,
               tests TEXT, tests_ok INTEGER, detail TEXT, duree_s REAL, deploye_le TEXT)""")
    return conn


def journal_add(outil: str, statut: str, **kw: Any) -> int:
    """Une ligne par appel : qui, quand, dépôt, prompt, puis branche / fichiers touchés / tests."""
    cols = {"outil": outil, "statut": statut, **{k: v for k, v in kw.items() if k in JOURNAL_COLS}}
    if cols.get("auteur_id") is not None:
        cols["auteur_id"] = str(cols["auteur_id"])
    with _journal() as conn:
        cur = conn.execute(f"INSERT INTO agent_journal({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                           tuple(cols.values()))
        jid = int(cur.lastrowid)
    log.info("journal #%s %s %s depot=%s auteur=%s prompt=%r", jid, outil, statut, cols.get("depot"),
             cols.get("auteur"), (cols.get("prompt") or "")[:200])
    return jid


def journal_update(jid: int, **kw: Any) -> None:
    cols = {k: v for k, v in kw.items() if k in JOURNAL_COLS}
    if not cols:
        return
    with _journal() as conn:
        conn.execute(f"UPDATE agent_journal SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", (*cols.values(), jid))
    log.info("journal #%s → %s", jid, {k: (str(v)[:120] if v is not None else None) for k, v in cols.items()})


def journal_last_run(depot: str) -> dict[str, Any] | None:
    """Dernière session agent_code du dépôt qui a produit une branche."""
    with _journal() as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM agent_journal WHERE outil='agent_code' AND depot=? AND branche IS NOT NULL "
                           "AND tete IS NOT NULL ORDER BY id DESC LIMIT 1", (depot,)).fetchone()
    return dict(row) if row else None


# --------------------------------------------------------------------------- bac à sable (bubblewrap)


def sandbox_cmd(work: Path, *, net: bool, ro: tuple[Path, ...] = (), rw: tuple[tuple[Path, str], ...] = ()) -> list[str]:
    """Préfixe bwrap : système en lecture seule, $HOME vide, seul `work` (et `rw`) inscriptible.
    bwrap pose no_new_privs (sudo inopérant) et n'expose ni /run ni D-Bus (systemctl inopérant).
    net=False → espace réseau vide (même pas le loopback de l'hôte)."""
    a = [BWRAP, "--die-with-parent", "--new-session", "--unshare-user", "--unshare-pid", "--unshare-ipc", "--unshare-uts",
         "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin", "--symlink", "usr/sbin", "/sbin",
         "--symlink", "usr/lib", "/lib"]
    if Path("/usr/lib64").is_dir():
        a += ["--symlink", "usr/lib64", "/lib64"]
    etc = ["/etc/passwd", "/etc/group", "/etc/alternatives"]
    if net:
        etc += ["/etc/ssl", "/etc/ca-certificates", "/etc/hosts", "/etc/nsswitch.conf"]
        resolv = Path("/etc/resolv.conf").resolve()
        if resolv.is_file():
            a += ["--ro-bind", str(resolv), "/etc/resolv.conf"]
    else:
        a += ["--unshare-net"]
    for p in etc:
        if os.path.exists(p):
            a += ["--ro-bind", p, p]
    a += ["--proc", "/proc", "--dev", "/dev", "--tmpfs", "/tmp", "--dir", str(HOME)]
    for p in ro:
        if p.exists():
            a += ["--ro-bind", str(p), str(p)]
    for src, dst in rw:
        a += ["--bind", str(src), dst]
    a += ["--bind", str(work), str(work), "--chdir", str(work), "--"]
    return a


def _box_env(work: Path) -> dict[str, str]:
    return {"PATH": "/usr/bin:/bin", "HOME": str(HOME), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8", "TERM": "dumb",
            "NO_COLOR": "1", "TZ": "Europe/Paris", "COLUMNS": "110", "PYTHONPATH": str(work),
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1", "GIT_PAGER": "cat", "PAGER": "cat",
            "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1", "RUFF_CACHE_DIR": "/tmp/ruff"}


def _clip(text: str, limit: int) -> str:
    text = ANSI.sub("", text).strip()
    if len(text) <= limit:
        return text
    head = limit // 3
    return text[:head] + f"\n… ({len(text) - limit} caractères coupés) …\n" + text[-(limit - head):]


async def _exec(cmd: list[str], *, env: dict[str, str], timeout: int, limit: int, cwd: str = "/") -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"⏱️ interrompu après {timeout}s"
    return proc.returncode or 0, _clip(out.decode("utf-8", "replace"), limit)


async def boxed(work: Path, venv: Path, argv: list[str], *, timeout: int = 60, limit: int = 12000) -> tuple[int, str]:
    """Exécute argv (sans shell) dans le clone, sans réseau."""
    return await _exec(sandbox_cmd(work, net=False, ro=(venv,)) + argv, env=_box_env(work), timeout=timeout, limit=limit)


def _git_id() -> list[str]:
    return ["git", "-c", f"user.name={settings.chat_agent_git_name}", "-c", f"user.email={settings.chat_agent_git_email}",
            "-c", "core.pager=cat", "-c", "commit.gpgsign=false", "-c", "core.hooksPath=/dev/null"]


async def git_live(repo: Path, *args: str, timeout: int = 60, limit: int = 8000) -> tuple[int, str]:
    """git dans le dépôt en service (de confiance), hors bac à sable — lectures et fetch uniquement."""
    env = {k: v for k, v in os.environ.items() if not k.startswith(("BW_", "GIT_"))}
    env.update(GIT_TERMINAL_PROMPT="0", GIT_PAGER="cat", LC_ALL="C.UTF-8")
    return await _exec(["git", "-C", str(repo), "-c", "core.pager=cat", *args], env=env, timeout=timeout, limit=limit)


# --------------------------------------------------------------------------- commandes offertes à l'agent


@dataclass
class Run:
    """Contexte d'une session d'agent, retrouvé par le serveur MCP des commandes (en-tête X-BW-Req)."""

    rid: str
    depot: Depot
    work: Path
    branch: str
    base: str


RUNS: dict[str, Run] = {}


def git_argv(args: str) -> list[str]:
    argv = shlex.split(args or "")
    if argv and argv[0] == "git":
        argv = argv[1:]
    if not argv:
        raise C.Refused("commande git vide")
    if argv[0] not in GIT_OK:
        raise C.Refused(f"`git {argv[0]}` n'est pas disponible ici (autorisés : {', '.join(GIT_OK)}). "
                        "Pas de changement de branche, de fusion, de push ni de configuration.")
    return argv


async def tool_git(run: Run, args: str) -> str:
    rc, out = await boxed(run.work, run.depot.venv, _git_id() + git_argv(args))
    return (f"(code retour {rc})\n" if rc else "") + (out or "(aucune sortie)")


def _py(run_or_depot: Run | Depot) -> str:
    depot = run_or_depot.depot if isinstance(run_or_depot, Run) else run_or_depot
    return str(depot.venv / "bin" / "python")


async def tool_pytest(run: Run, args: str = "") -> str:
    if not Path(_py(run)).exists():
        return "pytest indisponible : pas de venv configuré pour ce dépôt"
    argv = [_py(run), "-m", "pytest", "-p", "no:cacheprovider", *(shlex.split(args or "") or ["-q"])]
    rc, out = await boxed(run.work, run.depot.venv, argv, timeout=300, limit=8000)
    return f"(code retour {rc})\n{out or '(aucune sortie)'}"


async def tool_ruff(run: Run, args: str = "") -> str:
    ruff = run.depot.venv / "bin" / "ruff"
    if not ruff.exists():
        return "ruff indisponible : pas installé dans le venv de ce dépôt"
    argv = shlex.split(args or "") or ["check", "."]
    if argv[0] not in ("check", "format"):
        raise C.Refused("ruff : seules les sous-commandes check et format sont disponibles")
    rc, out = await boxed(run.work, run.depot.venv, [str(ruff), *argv], timeout=120, limit=8000)
    return f"(code retour {rc})\n{out or '(aucune sortie)'}"


# --------------------------------------------------------------------------- session d'agent (agent_code)

_LOCKS: dict[str, asyncio.Lock] = {}
BUILTIN_TOOLS = "Read,Grep,Glob,Edit,Write"
DEPOT_TOOLS = ("git", "pytest", "ruff")


def slugify(text: str, max_len: int = 32) -> str:
    s = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()
    out = ""
    for w in re.findall(r"[a-z0-9]+", s):
        if not out:
            out = w[:max_len]
        elif len(out) + len(w) + 1 <= max_len:
            out = f"{out}-{w}"
        else:
            break
    return out or "tache"


def oauth_token(min_left_s: int) -> str:
    """Jeton d'accès (courte durée) de l'abonnement Claude Code. La session en bac à sable le reçoit par
    l'environnement : elle n'a accès ni à ~/.claude ni au jeton de renouvellement."""
    try:
        o = json.loads((HOME / ".claude" / ".credentials.json").read_text(encoding="utf-8"))["claudeAiOauth"]
        tok, exp = str(o["accessToken"]), float(o.get("expiresAt") or 0) / 1000
    except (OSError, KeyError, ValueError, TypeError) as e:
        raise C.Refused("Claude Code n'est pas connecté sur le serveur (identifiants introuvables)") from e
    left = exp - time.time()
    if left < min_left_s:
        raise C.Refused(f"le jeton de l'abonnement expire dans {max(int(left // 60), 0)} min, moins que la durée "
                        "d'une session : réessaie dans une dizaine de minutes")
    return tok


def agent_rules(run: Run, minutes: int) -> str:
    return f"""Tu es un agent de modification de code lancé depuis Discord par le propriétaire du dépôt « {run.depot.name} ». Tu travailles seul : personne ne peut te répondre. Ne pose pas de question, fais des choix raisonnables et signale-les dans ton compte rendu.

Cadre de cette session :
- Ton répertoire de travail est un clone du dépôt, déjà placé sur la branche `{run.branch}`. Tu ne peux ni changer de branche, ni fusionner, ni pousser : le déploiement est une étape séparée, validée par le propriétaire.
- Outils : Read, Grep, Glob, Edit, Write pour les fichiers ; `mcp__depot__git` ({', '.join(GIT_OK)}), `mcp__depot__pytest`, `mcp__depot__ruff`. Il n'y a ni shell, ni réseau, ni installation de paquets : n'ajoute pas de dépendance.
- Les secrets et les données (.env, data/, instance/) ne sont pas dans ce clone : n'essaie pas de les lire, écris le code et les tests sans eux.
- Un commit par étape cohérente : `git add <fichiers>` puis `git commit -m "…"`, message court en français dans le style de l'historique (regarde `git log`).
- Reste dans le périmètre demandé et modifie le minimum. Avant de finir, lance pytest et ruff sur ce que tu as touché et corrige ce que tu as cassé.
- Termine par un compte rendu court : ce qui est fait, ce qui reste, ce qu'il faut vérifier avant de déployer.
Tu disposes d'environ {minutes} minutes."""


async def _prepare(depot: Depot, prompt: str) -> tuple[Path, str, str]:
    """Clone neuf de la branche principale + branche agent/<date>-<slug>. → (clone, branche, commit de base)"""
    rc, base = await git_live(depot.repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{depot.main}^{{commit}}")
    if rc or not re.fullmatch(r"[0-9a-f]{40,64}", base.strip()):
        raise C.Refused(f"{depot.name} : branche {depot.main} introuvable")
    base = base.strip()
    now = datetime.now(C.TZ)
    stem = f"agent/{now:%Y%m%d}-{slugify(prompt)}"
    for suffix in ("", f"-{now:%H%M}", f"-{now:%H%M%S}"):
        rc, _ = await git_live(depot.repo, "show-ref", "--verify", "--quiet", f"refs/heads/{stem}{suffix}")
        if rc:
            branch = stem + suffix
            break
    else:
        raise C.Refused("nom de branche déjà pris, réessaie dans une minute")
    root = _abs(settings.chat_agent_work_dir)
    root.mkdir(parents=True, exist_ok=True)
    root.chmod(0o700)
    work = root / depot.name
    if work.is_symlink():
        work.unlink()
    elif work.exists():
        await _exec(["chmod", "-R", "u+rwX", str(work)], env={"PATH": "/usr/bin:/bin"}, timeout=60, limit=500)
        await asyncio.to_thread(shutil.rmtree, work)
    env = {"PATH": "/usr/bin:/bin", "HOME": str(HOME), "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C.UTF-8"}
    for cmd in (["git", "clone", "--quiet", "--no-hardlinks", "--single-branch", "--branch", depot.main, str(depot.repo), str(work)],
                ["git", "-C", str(work), "switch", "--quiet", "-c", branch],
                ["git", "-C", str(work), "remote", "remove", "origin"]):
        rc, out = await _exec(cmd, env=env, timeout=120, limit=600)
        if rc:
            raise C.Refused(f"préparation du clone impossible ({' '.join(cmd[1:3])}) : {out[-300:]}")
    return work, branch, base


async def _claude(run: Run, prompt: str, cfg: Path, token: str, timeout: int) -> tuple[dict[str, Any] | None, str]:
    """Lance la session `claude -p` dans le bac à sable. → (résultat JSON ou None, note en cas de souci)"""
    found = shutil.which(settings.chat_cc_bin)
    if not found:
        raise C.Refused(f"Claude Code introuvable ({settings.chat_cc_bin})")
    real = Path(found).resolve()
    mcp_cfg = json.dumps({"mcpServers": {"depot": {
        "type": "http", "url": f"http://127.0.0.1:{settings.chat_agent_mcp_port}/mcp", "headers": {"X-BW-Req": run.rid}}}})
    args = [str(real), "-p", "Demande : " + prompt, "--output-format", "json",
            "--model", settings.chat_agent_model, "--max-turns", str(settings.chat_agent_max_turns),
            "--append-system-prompt", agent_rules(run, max(timeout // 60, 1)),
            "--permission-mode", "acceptEdits", "--permission-prompt-tool", "mcp__depot__approuver",
            "--strict-mcp-config", "--mcp-config", mcp_cfg, "--setting-sources", "",
            "--allowedTools", "Read", "Grep", "Glob", *(f"mcp__depot__{t}" for t in DEPOT_TOOLS),
            "--disallowedTools", "Bash", "WebFetch", "WebSearch", "Task", "Agent", "NotebookEdit", "Skill",
            "--tools", BUILTIN_TOOLS]
    env = {"PATH": "/usr/bin:/bin", "HOME": str(HOME), "LANG": "C.UTF-8", "TERM": "dumb", "NO_COLOR": "1", "CI": "1",
           "TZ": "Europe/Paris", "CLAUDE_CONFIG_DIR": SANDBOX_HOME_CFG, "CLAUDE_CODE_OAUTH_TOKEN": token,
           "DISABLE_AUTOUPDATER": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"}
    ro = () if str(real).startswith("/usr/") else (real,)
    cmd = sandbox_cmd(run.work, net=True, ro=ro, rw=((cfg, SANDBOX_HOME_CFG),)) + args
    proc = await asyncio.create_subprocess_exec(*cmd, cwd="/", env=env, stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return None, f"délai de {timeout // 60} min dépassé, session interrompue"
    raw = out.decode("utf-8", "replace").strip()
    try:
        data = json.loads(raw)
    except ValueError:
        log.error("agent : sortie claude illisible (code %s) : %s / %s", proc.returncode, raw[:400], err.decode("utf-8", "replace")[:400])
        return None, f"Claude Code a échoué (code {proc.returncode}) : {_clip(err.decode('utf-8', 'replace') or raw, 300)}"
    return (data if isinstance(data, dict) else None), ""


async def _final_tests(run: Run) -> tuple[str, int | None]:
    """Tests relancés par le bot en fin de session (on ne se fie pas au compte rendu de l'agent)."""
    parts: list[str] = []
    ok: int | None = None
    venv, py = run.depot.venv, _py(run)
    if Path(py).exists():
        rc, out = await boxed(run.work, venv, [py, "-m", "pytest", "-q", "-p", "no:cacheprovider"], timeout=300, limit=3000)
        last = (out.splitlines() or ["(aucune sortie)"])[-1].strip()
        if "No module named pytest" in out:
            parts.append("pytest : non installé pour ce dépôt")
        elif rc == 0:
            parts.append(f"pytest ✅ {last}")
            ok = 1
        elif rc == 5:
            parts.append("pytest : aucun test dans ce dépôt")
        else:
            fails = [ln for ln in out.splitlines() if ln.startswith(("FAILED", "ERROR"))][:6]
            parts.append(f"pytest ❌ {last}" + ("\n" + "\n".join(fails) if fails else ""))
            ok = 0
    ruff = venv / "bin" / "ruff"
    if ruff.exists():
        rc, names = await boxed(run.work, venv, _git_id() + ["diff", "--name-only", "--diff-filter=ACMR", f"{run.base}..HEAD", "--", "*.py"])
        files = [f for f in names.splitlines() if f.strip()] if rc == 0 else []
        if files:
            rc, out = await boxed(run.work, venv, [str(ruff), "check", "--", *files[:200]], timeout=120, limit=1500)
            parts.append("ruff ✅" if rc == 0 else f"ruff ⚠️ {(out.splitlines() or ['?'])[-1].strip()}")
    return " · ".join(parts) or "aucun test lancé", ok


async def _leaks(repo: Path, base: str, ref: str, secret: str) -> bool:
    """Le jeton de session (lisible par l'agent dans son propre environnement) s'est-il retrouvé dans un commit ?"""
    proc = await asyncio.create_subprocess_exec("git", "-C", str(repo), "log", "-p", "--no-color", "--no-textconv", "--no-ext-diff",
                                                f"{base}..{ref}", stdin=asyncio.subprocess.DEVNULL,
                                                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
    out, _ = await proc.communicate()
    return bool(secret) and secret.encode() in out


async def _finish(run: Run, rundir: Path, secret: str = "") -> dict[str, Any]:
    """Commit du reliquat, tests, rapatriement de la branche dans le dépôt en service (sans fusion)."""
    d, work, venv, g = run.depot, run.work, run.depot.venv, _git_id()
    rc, cur = await boxed(work, venv, g + ["symbolic-ref", "--short", "HEAD"])
    if rc or cur.strip() != run.branch:
        raise C.Refused(f"le clone n'est plus sur {run.branch} : résultat de la session ignoré")
    rc, dirty = await boxed(work, venv, g + ["status", "--porcelain"])
    if dirty.strip():
        await boxed(work, venv, g + ["add", "-A"])
        await boxed(work, venv, g + ["commit", "-q", "-m", "agent : modifications non commitées en fin de session"])
    rc, n = await boxed(work, venv, g + ["rev-list", "--count", f"{run.base}..HEAD"])
    commits = int(n.strip()) if rc == 0 and n.strip().isdigit() else 0
    if not commits:
        return {"commits": 0}
    tests, tests_ok = await _final_tests(run)
    ref = f"refs/heads/{run.branch}"
    rc, out = await boxed(work, venv, g + ["bundle", "create", ".git/agent.bundle", f"{run.base}..{ref}"])
    src = work / ".git" / "agent.bundle"
    if rc or src.is_symlink() or not src.is_file():
        raise C.Refused(f"impossible d'empaqueter la branche : {out[-300:]}")
    bundle = rundir / "branche.bundle"
    await asyncio.to_thread(shutil.copyfile, src, bundle)
    rc, out = await git_live(d.repo, "fetch", "--quiet", "--no-tags", str(bundle), f"{ref}:{ref}")
    if rc:
        raise C.Refused(f"rapatriement de la branche impossible : {out[-300:]}")
    rc, head = await git_live(d.repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    rc2, _ = await git_live(d.repo, "merge-base", "--is-ancestor", run.base, ref)
    if rc or rc2:
        await git_live(d.repo, "update-ref", "-d", ref)
        raise C.Refused("la branche rapatriée ne descend pas du commit de départ : écartée")
    if await _leaks(d.repo, run.base, ref, secret):
        await git_live(d.repo, "update-ref", "-d", ref)
        raise C.Refused("un commit de l'agent contient le jeton de session : branche écartée")
    _, log_ = await git_live(d.repo, "log", "--oneline", "--no-decorate", "-n", "15", f"{run.base}..{ref}", limit=1500)
    _, stat = await git_live(d.repo, "diff", "--shortstat", f"{run.base}..{ref}", limit=300)
    _, names = await git_live(d.repo, "diff", "--name-status", f"{run.base}..{ref}", limit=20000)
    return {"commits": commits, "head": head.strip(), "log": log_, "stat": stat.strip(), "files": names.strip(),
            "tests": tests, "tests_ok": tests_ok}


def _prune_runs(root: Path, keep_days: int = 30) -> None:
    limit = time.time() - keep_days * 86400
    for p in root.iterdir() if root.is_dir() else ():
        try:
            if p.is_dir() and not p.is_symlink() and p.stat().st_mtime < limit:
                shutil.rmtree(p, ignore_errors=True)
            elif p.is_file() and p.stat().st_mtime < limit:
                p.unlink()
        except OSError:
            pass


def _dur(s: float) -> str:
    return f"{int(s // 60)} min {int(s % 60):02d} s" if s >= 60 else f"{int(s)} s"


async def _session(depot: Depot, prompt: str, jid: int, t0: float) -> tuple[str, dict[str, Any], str]:
    timeout = settings.chat_agent_timeout
    token = oauth_token(timeout + 120)
    work, branch, base = await _prepare(depot, prompt)
    runs = _abs(settings.chat_agent_runs_dir)
    runs.mkdir(parents=True, exist_ok=True)
    runs.chmod(0o700)
    await asyncio.to_thread(_prune_runs, runs)
    rundir = runs / f"{jid:05d}-{depot.name}"
    cfg = rundir / "claude"
    cfg.mkdir(parents=True, exist_ok=True)
    run = Run(uuid.uuid4().hex, depot, work, branch, base)
    journal_update(jid, branche=branch, base=base)
    RUNS[run.rid] = run
    try:
        data, note = await _claude(run, prompt, cfg, token, timeout)
    finally:
        RUNS.pop(run.rid, None)
    if data:
        u = data.get("usage") or {}
        C.usage_add(int(u.get("input_tokens", 0) or 0) + int(u.get("cache_read_input_tokens", 0) or 0)
                    + int(u.get("cache_creation_input_tokens", 0) or 0), int(u.get("output_tokens", 0) or 0))
        if data.get("is_error") or data.get("subtype") not in (None, "success"):
            note = {"error_max_turns": "nombre maximal d'étapes atteint"}.get(data.get("subtype") or "", f"fin anormale ({data.get('subtype') or 'erreur'})")
    said = _clip(str((data or {}).get("result") or ""), 1200)
    res = await _finish(run, rundir, token)
    took = _dur(time.monotonic() - t0)
    if not res["commits"]:
        statut = "sans changement" if not note else "échec"
        text = (f"Session terminée en {took} sans aucune modification du dépôt {depot.name}"
                + (f" — {note}" if note else "") + "." + (f"\n\nCompte rendu de l'agent :\n{said}" if said else ""))
        return statut, {"detail": (note + "\n" if note else "") + said}, text
    statut = "partiel" if note else "ok"
    files = res["files"].splitlines()
    shown = files[:40] + ([f"… et {len(files) - 40} autres"] if len(files) > 40 else [])
    lines = [
        f"{'⚠️ Session incomplète' if note else '✅ Session terminée'} en {took} — dépôt {depot.name}, branche `{branch}` "
        f"(base {base[:7]}, {res['commits']} commit{'s' if res['commits'] > 1 else ''})." + (f" {note.capitalize()}." if note else ""),
        f"Rien n'est fusionné ni poussé : le service tourne toujours sur {depot.main}.",
        "", "Commits :", res["log"], "", f"Fichiers modifiés ({len(files)}) :", *shown, "",
        f"Diff : {res['stat'] or '?'}", f"Tests relancés par le bot en fin de session : {res['tests']}"]
    if said:
        lines += ["", "Compte rendu de l'agent :", said]
    lines += ["", f"Suite : relire (`git diff {depot.main}...{branch}`), puis déployer avec l'outil agent_deployer "
                  f"(dépôt {depot.name}) — validation ✅ séparée." if statut == "ok" and res["tests_ok"] != 0 else
              "Suite : branche à relire ou à reprendre (nouvelle session agent_code) avant tout déploiement."]
    fields = {"tete": res["head"], "fichiers": res["files"], "tests": res["tests"], "tests_ok": res["tests_ok"],
              "detail": (note + "\n" if note else "") + said}
    return statut, fields, _clip("\n".join(lines), 3800)


async def agent_code(depot_name: str, prompt: str, *, author_id: int | None, author: str) -> str:
    """Point d'entrée de l'outil MCP (droits et validation ✅ déjà vérifiés par l'appelant)."""
    who = {"auteur_id": author_id, "auteur": author, "depot": depot_name, "prompt": prompt}
    try:
        depot, prompt = get_depot(depot_name), check_prompt(prompt)
        if not Path(BWRAP).exists():
            raise C.Refused("bubblewrap (bwrap) n'est pas installé : pas de bac à sable, pas d'agent")
    except C.Refused as e:
        journal_add("agent_code", "refusé", detail=str(e), **who)
        raise
    lock = _LOCKS.setdefault(depot.name, asyncio.Lock())
    if lock.locked():
        journal_add("agent_code", "refusé", detail="session déjà en cours sur ce dépôt", **who)
        raise C.Refused(f"une session d'agent est déjà en cours sur {depot.name}")
    async with lock:
        who.update(depot=depot.name, prompt=prompt)
        jid, t0 = journal_add("agent_code", "en cours", **who), time.monotonic()
        try:
            statut, fields, text = await _session(depot, prompt, jid, t0)
        except C.Refused as e:
            journal_update(jid, statut="échec", detail=str(e), duree_s=round(time.monotonic() - t0, 1))
            raise
        except BaseException as e:
            journal_update(jid, statut="erreur", detail=f"{type(e).__name__}: {e}", duree_s=round(time.monotonic() - t0, 1))
            raise
        journal_update(jid, statut=statut, duree_s=round(time.monotonic() - t0, 1), **fields)
        return f"[journal #{jid}] {text}"


# --------------------------------------------------------------------------- déploiement (agent_deployer)


async def deploy_plan(depot_name: str) -> dict[str, Any]:
    """Ce que ferait agent_deployer maintenant ; lève Refused si le déploiement automatique n'est pas possible.
    Sert à la fois à la demande de validation ✅ et à l'exécution."""
    d = get_depot(depot_name)
    row = journal_last_run(d.name)
    if not row:
        raise C.Refused(f"aucune branche d'agent à déployer pour {d.name}")
    branch, head = row["branche"], row["tete"]
    if row["deploye_le"]:
        raise C.Refused(f"la dernière branche d'agent ({branch}) a déjà été déployée le {row['deploye_le']}")
    if row["statut"] != "ok":
        raise C.Refused(f"la dernière session ({branch}) s'est terminée en « {row['statut']} » : à relire ou à reprendre, "
                        "pas de déploiement automatique")
    if row["tests_ok"] == 0:
        raise C.Refused(f"les tests échouent sur {branch} ({row['tests']}) : pas de déploiement automatique")
    rc, cur = await git_live(d.repo, "symbolic-ref", "--short", "HEAD")
    if rc or cur.strip() != d.main:
        raise C.Refused(f"le dépôt en service n'est pas sur {d.main} (« {cur.strip()[:60]} ») : déploiement manuel")
    ref = f"refs/heads/{branch}"
    rc, now = await git_live(d.repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    if rc or now.strip() != head:
        raise C.Refused(f"la branche {branch} a disparu ou a bougé depuis la session : déploiement manuel")
    rc, _ = await git_live(d.repo, "merge-base", "--is-ancestor", ref, d.main)
    if rc == 0:
        raise C.Refused(f"{branch} est déjà fusionnée dans {d.main}")
    _, prev = await git_live(d.repo, "rev-parse", "--short", d.main)
    _, names = await git_live(d.repo, "diff", "--name-only", f"{d.main}...{ref}", limit=20000)
    files = [f for f in names.splitlines() if f.strip()]
    touched = sorted(set(files) & set(d.deps))
    if touched:
        raise C.Refused(f"la branche modifie les dépendances ({', '.join(touched)}) : installation et déploiement manuels")
    _, log_ = await git_live(d.repo, "log", "--oneline", "--no-decorate", "-n", "10", f"{d.main}..{ref}", limit=900)
    _, stat = await git_live(d.repo, "diff", "--shortstat", f"{d.main}...{ref}", limit=300)
    services = list(d.services) + [s for f in d.followers for s in f.get("services", [])]
    return {"depot": d, "run_id": row["id"], "branch": branch, "head": head, "prev": prev.strip(), "files": files,
            "log": log_, "stat": stat.strip(), "services": services, "tests": row["tests"] or "",
            "followers": [Path(f["repo"]).name for f in d.followers]}


def plan_text(plan: dict[str, Any]) -> str:
    d: Depot = plan["depot"]
    out = [f"Fusion de `{plan['branch']}` dans {d.main} (actuellement {plan['prev']}) : {plan['stat'] or '?'}",
           "```", plan["log"][:700], "```",
           f"Tests de la session : {plan['tests'][:200]}",
           f"Redémarrage : {', '.join(plan['services']) or 'aucun service'}"]
    if plan["followers"]:
        out.append(f"Clones alignés : {', '.join(plan['followers'])}")
    out.append("Test de santé ensuite ; en cas d'échec, retour automatique au commit précédent.")
    return "\n".join(out)


async def agent_deployer(depot_name: str, *, author_id: int | None, author: str, channel_id: int | None) -> str:
    """Lance le déploiement dans une unité systemd transitoire et rend la main : le compte rendu est posté
    dans le salon par le script (le bot peut être redémarré en cours de route)."""
    who = {"auteur_id": author_id, "auteur": author, "depot": depot_name}
    try:
        plan = await deploy_plan(depot_name)
    except C.Refused as e:
        journal_add("agent_deployer", "refusé", detail=str(e), **who)
        raise
    d: Depot = plan["depot"]
    script = C.ROOT / "deploy" / "agent_deploy.py"
    if not script.is_file():
        raise C.Refused("script de déploiement introuvable (deploy/agent_deploy.py)")
    jid = journal_add("agent_deployer", "lancé", branche=plan["branch"], base=plan["prev"], tete=plan["head"],
                      fichiers="\n".join(plan["files"]), **{**who, "depot": d.name})
    runs = _abs(settings.chat_agent_runs_dir)
    runs.mkdir(parents=True, exist_ok=True)
    runs.chmod(0o700)
    token = ""
    if channel_id:
        try:
            token = C.discord_token()
        except SystemExit:
            token = ""
    job = {"journal_id": jid, "run_id": plan["run_id"], "journal_db": str(C.CHAT_DB.resolve()), "depot": d.name,
           "repo": str(d.repo), "main": d.main, "branch": plan["branch"], "head": plan["head"],
           "services": list(d.services), "health": list(d.health),
           "followers": [{"repo": str(Path(f["repo"]).expanduser()), "services": f.get("services", []), "health": f.get("health", [])}
                         for f in d.followers],
           "git_name": settings.chat_agent_git_name, "git_email": settings.chat_agent_git_email, "author": author,
           "delay_s": 20 if d.restarts_bot else 2, "channel_id": channel_id, "discord_token": token}
    jobfile = runs / f"deploy-{jid:05d}.json"
    fd = os.open(jobfile, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(job, f, ensure_ascii=False)
    unit = f"bw-agent-deploy-{jid}"
    cmd = ["sudo", "-n", "systemd-run", "--quiet", "--collect", "--unit", unit, "--uid", str(os.getuid()), "--gid", str(os.getgid()),
           "--working-directory", str(d.repo), "--setenv", "PYTHONUNBUFFERED=1", "--setenv", f"HOME={HOME}",
           "/usr/bin/python3", str(script), str(jobfile)]
    rc, out = await _exec(cmd, env={"PATH": "/usr/bin:/bin"}, timeout=30, limit=600)
    if rc:
        jobfile.unlink(missing_ok=True)
        journal_update(jid, statut="échec", detail=f"systemd-run : {out}")
        raise C.Refused(f"impossible de lancer le déploiement : {out[-300:]}")
    where = "dans ce salon" if channel_id and token else f"dans `journalctl -u {unit}`"
    extra = " Le bot fait partie des services redémarrés : il sera indisponible quelques secondes." if d.restarts_bot else ""
    return (f"[journal #{jid}] Déploiement de `{plan['branch']}` lancé pour {d.name} (unité {unit}) : fusion, redémarrage de "
            f"{', '.join(plan['services']) or 'aucun service'}, test de santé, retour arrière automatique en cas d'échec. "
            f"Le compte rendu arrive {where} dans la minute.{extra}")
