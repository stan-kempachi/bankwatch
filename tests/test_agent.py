"""Délégation de code (agent_code / agent_deployer) : garde-fous vérifiables sans Claude ni Discord."""
import asyncio
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("anthropic")  # bankwatch.chat
pytest.importorskip("mcp")        # bankwatch.chat_cc

from bankwatch import agent as A  # noqa: E402
from bankwatch import chat as C  # noqa: E402
from bankwatch import chat_cc as CC  # noqa: E402


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.org", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def depot(tmp_path, monkeypatch):
    repo = tmp_path / "depot"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "main")
    (repo / "app.py").write_text("x = 1\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "init")
    cfg = tmp_path / "depots.json"
    cfg.write_text(json.dumps({"demo": {"repo": str(repo), "services": [], "health": [{"port": 1}]}}))
    monkeypatch.setattr(A.settings, "chat_agent_depots_file", cfg)
    monkeypatch.setattr(C, "CHAT_DB", tmp_path / "chat.db")
    return repo


def test_depot_is_a_whitelist_key_never_a_path(depot):
    assert A.get_depot("demo").repo == depot
    for bad in (str(depot), "/etc", "../demo", "demo/..", "autre", ""):
        with pytest.raises(C.Refused):
            A.get_depot(bad)


def test_prompt_must_fit_in_the_confirmation(depot):
    assert A.check_prompt("  corrige le bug  ") == "corrige le bug"
    for bad in ("", "   ", "x" * (A.settings.chat_agent_max_prompt + 1)):
        with pytest.raises(C.Refused):
            A.check_prompt(bad)


def test_git_tool_only_exposes_safe_subcommands():
    assert A.git_argv("status --short") == ["status", "--short"]
    assert A.git_argv('git commit -m "zone : deux mots"') == ["commit", "-m", "zone : deux mots"]
    for bad in ("push", "push origin main", "checkout main", "switch -c x", "merge main", "reset --hard HEAD~1",
                "branch -D main", "config user.name x", "-c core.pager=sh status", "remote add o url", "fetch", "pull", ""):
        with pytest.raises(C.Refused):
            A.git_argv(bad)


def test_slug():
    assert A.slugify("Ajoute un bouton « Exporter en CSV » !") == "ajoute-un-bouton-exporter-en-csv"
    assert len(A.slugify("mot " * 50)) <= 32
    assert A.slugify("…") == "tache"


def test_command_sandbox_has_no_network_and_an_empty_home(tmp_path):
    work = tmp_path / "clone"
    cmd = A.sandbox_cmd(work, net=False)
    assert "--unshare-net" in cmd and "--unshare-user" in cmd and "--die-with-parent" in cmd
    assert cmd[cmd.index("--dir") + 1] == str(A.HOME)          # $HOME vide, pas monté
    binds = [cmd[i + 1] for i, a in enumerate(cmd) if a == "--bind"]
    assert binds == [str(work)]                                 # seul le clone est inscriptible
    assert "--unshare-net" not in A.sandbox_cmd(work, net=True)  # la session claude, elle, doit joindre l'API


def _bwrap_usable() -> bool:
    if not shutil.which("bwrap"):
        return False
    return subprocess.run(["bwrap", "--unshare-user", "--ro-bind", "/usr", "/usr", "--symlink", "usr/bin", "/bin",
                           "--symlink", "usr/lib", "/lib", "--", "/usr/bin/true"], capture_output=True).returncode == 0


@pytest.mark.skipif(not _bwrap_usable(), reason="bubblewrap indisponible sur cette machine")
def test_command_sandbox_for_real(tmp_path):
    work = tmp_path / "clone"
    work.mkdir()
    outside = tmp_path / "dehors.txt"
    home_file = A.HOME / f"agent-test-{work.parent.name}.txt"
    probe = f"""
import socket, os, subprocess
def essai(nom, f):
    try: f(); print(nom, "OK")
    except Exception as e: print(nom, "KO", type(e).__name__)
essai("reseau", lambda: socket.create_connection(("1.1.1.1", 53), timeout=2))
essai("local", lambda: socket.create_connection(("127.0.0.1", 22), timeout=2))
essai("dehors", lambda: open({str(outside)!r}, "w"))
essai("dehors2", lambda: open({str(home_file)!r}, "w"))
essai("home", lambda: os.listdir(os.path.expanduser("~/.ssh")))
essai("dedans", lambda: open("ok.txt", "w").write("x"))
print("sudo", subprocess.run(["sudo", "-n", "true"], capture_output=True).returncode if os.path.exists("/usr/bin/sudo") else 1)
"""
    rc, out = asyncio.run(A.boxed(work, tmp_path / "pas-de-venv", ["/usr/bin/python3", "-c", probe]))
    assert rc == 0, out
    assert "reseau KO" in out and "local KO" in out and "home KO" in out
    # écrire « ailleurs » ne touche qu'un tmpfs jetable du bac à sable : rien n'arrive sur la machine
    assert "dedans OK" in out and (work / "ok.txt").exists() and not outside.exists() and not home_file.exists()
    assert "sudo 0" not in out


def test_journal_records_every_call(depot):
    jid = A.journal_add("agent_code", "en cours", auteur_id=42, auteur="Stan", depot="demo", prompt="corrige")
    assert A.journal_last_run("demo") is None            # pas encore de branche aboutie
    A.journal_update(jid, statut="ok", branche="agent/x", tete="abc", fichiers="M\tapp.py", tests_ok=1)
    row = A.journal_last_run("demo")
    assert (row["auteur_id"], row["prompt"], row["fichiers"], row["statut"]) == ("42", "corrige", "M\tapp.py", "ok")
    assert row["ts"]


def test_agent_tools_need_a_validation_for_this_exact_call():
    async def nope(_t, _d):
        return False
    req = CC.Req(1, True, nope, None)
    args = {"depot": "demo", "prompt": "corrige le bug"}
    with pytest.raises(C.Refused):
        CC._need_approval(req, "agent_code", args)
    req.approved.add(CC._akey("agent_code", args))
    with pytest.raises(C.Refused):                        # autre prompt : la validation ne vaut pas
        CC._need_approval(req, "agent_code", {**args, "prompt": "efface tout"})
    with pytest.raises(C.Refused):                        # autre outil : non plus
        CC._need_approval(req, "agent_deployer", args)
    CC._need_approval(req, "agent_code", args)            # consommée…
    with pytest.raises(C.Refused):                        # …donc une seule fois
        CC._need_approval(req, "agent_code", args)
    assert {"agent_code", "agent_deployer"} <= set(CC.ACTION_TOOLS) and not {"agent_code", "agent_deployer"} & set(CC.READ_TOOLS)


def test_deploy_plan_refusals(depot):
    with pytest.raises(C.Refused, match="aucune branche"):
        asyncio.run(A.deploy_plan("demo"))
    git(depot, "switch", "-q", "-c", "agent/x")
    (depot / "app.py").write_text("x = 2\n")
    git(depot, "commit", "-q", "-am", "x = 2")
    head = git(depot, "rev-parse", "HEAD")
    git(depot, "switch", "-q", "main")
    jid = A.journal_add("agent_code", "partiel", depot="demo", branche="agent/x", tete=head, tests_ok=1)
    with pytest.raises(C.Refused, match="partiel"):
        asyncio.run(A.deploy_plan("demo"))
    A.journal_update(jid, statut="ok", tests_ok=0, tests="pytest ❌ 1 failed")
    with pytest.raises(C.Refused, match="tests"):
        asyncio.run(A.deploy_plan("demo"))
    A.journal_update(jid, tests_ok=1)
    plan = asyncio.run(A.deploy_plan("demo"))
    assert plan["branch"] == "agent/x" and plan["files"] == ["app.py"] and "x = 2" in plan["log"]
    A.journal_update(jid, tete="0" * 40)                  # la branche a bougé depuis la session
    with pytest.raises(C.Refused, match="bougé"):
        asyncio.run(A.deploy_plan("demo"))


def _deploy_module():
    spec = importlib.util.spec_from_file_location("agent_deploy", Path(__file__).parent.parent / "deploy" / "agent_deploy.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("healthy", [True, False])
def test_deploy_merges_then_rolls_back_when_unhealthy(depot, tmp_path, monkeypatch, healthy):
    D = _deploy_module()
    prev = git(depot, "rev-parse", "HEAD")
    git(depot, "switch", "-q", "-c", "agent/x")
    (depot / "app.py").write_text("x = 2\n")
    git(depot, "commit", "-q", "-am", "x = 2")
    head = git(depot, "rev-parse", "HEAD")
    git(depot, "switch", "-q", "main")
    (depot / "local.txt").write_text("réglage local non commité\n")
    run_id = A.journal_add("agent_code", "ok", depot="demo", branche="agent/x", tete=head)
    jid = A.journal_add("agent_deployer", "lancé", depot="demo", branche="agent/x")
    job = {"journal_id": jid, "run_id": run_id, "journal_db": str(tmp_path / "chat.db"), "depot": "demo", "repo": str(depot),
           "main": "main", "branch": "agent/x", "head": head, "services": [], "health": [{"port": 1}], "followers": [],
           "delay_s": 0, "channel_id": None, "discord_token": ""}
    monkeypatch.setattr(D.Deploy, "healthy", lambda self, wait=75: [] if healthy else ["port 1"])
    rc = D.Deploy(job).run()
    row = A.journal_last_run("demo")
    assert (depot / "local.txt").exists()
    if healthy:
        assert rc == 0 and (depot / "app.py").read_text() == "x = 2\n" and row["deploye_le"]
        assert git(depot, "log", "-1", "--format=%s") == "agent : fusion de agent/x"
    else:
        assert rc == 1 and git(depot, "rev-parse", "HEAD") == prev and (depot / "app.py").read_text() == "x = 1\n"
        assert row["deploye_le"] is None and git(depot, "rev-parse", "agent/x") == head  # branche conservée


def test_agent_tools_are_for_the_owner_and_listed_accounts(monkeypatch):
    async def nope(_t, _d):
        return False
    monkeypatch.setattr(CC, "AGENT_IDS", {7})
    assert CC._agent_ok(CC.Req(1, True, nope, None))          # propriétaire
    assert CC._agent_ok(CC.Req(7, False, nope, None))         # compte ajouté à BW_CHAT_AGENT_USER_IDS
    assert not CC._agent_ok(CC.Req(8, False, nope, None))     # autre compte autorisé à parler au bot
    assert not CC._agent_ok(CC.Req(None, False, nope, None))
