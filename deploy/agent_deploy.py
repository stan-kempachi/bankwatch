#!/usr/bin/env python3
"""Déploiement d'une branche d'agent : fusion, redémarrage, tests de santé, retour arrière.

Lancé par `bankwatch.agent.agent_deployer` dans une unité systemd transitoire (le déploiement peut
redémarrer le bot qui l'a demandé). Bibliothèque standard uniquement. Argument : un fichier JSON
décrivant le travail (supprimé dès la lecture : il peut contenir le jeton du bot Discord).

Étapes : fusion `--no-ff` de la branche dans la branche principale → alignement des clones suiveurs
(fast-forward) → redémarrage des services → tests de santé. Si une étape échoue, chaque dépôt revient
à son commit de départ (`git reset --keep` : les modifications locales non commitées sont préservées),
les services sont redémarrés et la santé revérifiée. Le résultat est posté dans le salon Discord et
inscrit dans la table `agent_journal`.
"""
from __future__ import annotations

import json
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.error
import urllib.request


def sh(*cmd: str, timeout: int = 120) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return 124, f"délai dépassé : {' '.join(cmd[:4])}"
    return p.returncode, (p.stdout + p.stderr).strip()


def git(repo: str, *args: str) -> tuple[int, str]:
    return sh("git", "-C", repo, *args)


class Deploy:
    def __init__(self, job: dict) -> None:
        self.job = job
        self.token = job.pop("discord_token", "") or ""
        self.targets = [{"repo": job["repo"], "services": job.get("services", []), "health": job.get("health", [])},
                        *job.get("followers", [])]
        self.services = [s for t in self.targets for s in t.get("services", [])]
        self.checks = [c for t in self.targets for c in t.get("health", [])]
        self.prev: dict[str, str] = {}
        self.restarts: dict[str, str] = {}
        self.notes: list[str] = []

    # ------------------------------------------------------------------ sorties

    def say(self, text: str) -> None:
        print(text, flush=True)
        if not (self.token and self.job.get("channel_id")):
            return
        req = urllib.request.Request(
            f"https://discord.com/api/v10/channels/{self.job['channel_id']}/messages",
            data=json.dumps({"content": text[:1900], "allowed_mentions": {"parse": []}}).encode(),
            headers={"Authorization": f"Bot {self.token}", "Content-Type": "application/json",
                     "User-Agent": "DiscordBot (https://github.com/stan-kempachi/bankwatch, 1.0)"})
        try:
            urllib.request.urlopen(req, timeout=15).read()
        except Exception as e:  # noqa: BLE001 — le déploiement ne dépend pas de Discord
            print(f"discord : {e}", flush=True)

    def journal(self, statut: str, detail: str, deployed: bool = False) -> None:
        try:
            conn = sqlite3.connect(self.job["journal_db"], timeout=15)
            with conn:
                conn.execute("UPDATE agent_journal SET statut=?, detail=?, duree_s=? WHERE id=?",
                             (statut, detail[:4000], round(time.monotonic() - self.t0, 1), self.job["journal_id"]))
                if deployed:
                    conn.execute("UPDATE agent_journal SET deploye_le=datetime('now','localtime') WHERE id=?",
                                 (self.job["run_id"],))
            conn.close()
        except sqlite3.Error as e:
            print(f"journal : {e}", flush=True)

    # ------------------------------------------------------------------ santé

    def restart(self) -> tuple[int, str]:
        if not self.services:
            return 0, ""
        rc, out = sh("sudo", "-n", "systemctl", "restart", *self.services, timeout=180)
        for c in self.checks:
            if "unit" in c:
                self.restarts[c["unit"]] = sh("systemctl", "show", "-p", "NRestarts", "--value", c["unit"])[1]
        return rc, out

    def _ok(self, c: dict) -> bool:
        if "url" in c:
            try:
                with urllib.request.urlopen(c["url"], timeout=6) as r:
                    return r.status == int(c.get("status", 200))
            except (urllib.error.URLError, OSError, ValueError):
                return False
        if "unit" in c:
            active = sh("systemctl", "is-active", "--quiet", c["unit"])[0] == 0
            same = sh("systemctl", "show", "-p", "NRestarts", "--value", c["unit"])[1] == self.restarts.get(c["unit"], "")
            return active and same  # actif ET pas relancé par systemd depuis notre restart (boucle de crash)
        if "port" in c:
            try:
                socket.create_connection(("127.0.0.1", int(c["port"])), timeout=3).close()
                return True
            except OSError:
                return False
        return False

    @staticmethod
    def _name(c: dict) -> str:
        return str(c.get("url") or c.get("unit") or f"port {c.get('port')}")

    def healthy(self, wait: int = 75) -> list[str]:
        """→ liste des tests en échec (vide = tout va bien, confirmé deux fois à 8 s d'intervalle)."""
        time.sleep(6)
        deadline = time.monotonic() + wait
        bad = [c for c in self.checks if not self._ok(c)]
        while bad and time.monotonic() < deadline:
            time.sleep(4)
            bad = [c for c in self.checks if not self._ok(c)]
        if not bad:
            time.sleep(8)
            bad = [c for c in self.checks if not self._ok(c)]
        return [self._name(c) for c in bad]

    # ------------------------------------------------------------------ étapes

    def rollback(self) -> list[str]:
        errs = []
        for repo, sha in self.prev.items():
            if git(repo, "rev-parse", "HEAD")[1] == sha:
                continue
            rc, out = git(repo, "reset", "--keep", sha)
            if rc:
                errs.append(f"{repo} : reset impossible ({out[-200:]})")
        return errs

    def run(self) -> int:
        job, self.t0 = self.job, time.monotonic()
        repo, main, branch = job["repo"], job["main"], job["branch"]
        time.sleep(float(job.get("delay_s", 0)))
        ident = ["-c", f"user.name={job.get('git_name', 'agent-code')}", "-c", f"user.email={job.get('git_email', 'agent-code@localhost')}"]

        # 0. état de départ
        for t in self.targets:
            rc, sha = git(t["repo"], "rev-parse", "HEAD")
            if rc:
                return self.fail(f"dépôt illisible : {t['repo']}")
            self.prev[t["repo"]] = sha
        if git(repo, "symbolic-ref", "--short", "HEAD")[1] != main:
            return self.fail(f"le dépôt n'est plus sur {main}")
        if git(repo, "rev-parse", f"refs/heads/{branch}")[1] != job["head"]:
            return self.fail(f"la branche {branch} a bougé depuis la validation")
        self.say(f"🚀 Déploiement de `{job['depot']}` : fusion de `{branch}` dans {main} ({self.prev[repo][:7]})…")

        # 1. fusion + alignement des suiveurs
        rc, out = git(repo, *ident, "merge", "--no-ff", "--no-edit", "-m", f"agent : fusion de {branch}", f"refs/heads/{branch}")
        if rc:
            git(repo, "merge", "--abort")
            return self.fail(f"fusion impossible :\n```\n{out[-600:]}\n```")
        new = git(repo, "rev-parse", "--short", "HEAD")[1]
        for t in self.targets[1:]:
            rc, out = git(t["repo"], "fetch", "--quiet", repo, main)
            if rc == 0:
                rc, out = git(t["repo"], "merge", "--ff-only", "FETCH_HEAD")
            if rc:
                errs = self.rollback()
                return self.fail(f"alignement de {t['repo']} impossible :\n```\n{out[-400:]}\n```" + "".join(f"\n⚠️ {e}" for e in errs),
                                 touched=False)

        # 2. redémarrage + santé
        rc, out = self.restart()
        bad = [f"redémarrage : {out[-300:]}"] if rc else self.healthy()
        if not bad:
            done = f"{', '.join(self.services)} redémarré(s)" if self.services else "aucun service à redémarrer"
            msg = f"✅ `{job['depot']}` déployé : {main} est en {new} (avant : {self.prev[repo][:7]}) ; {done}, tests de santé OK."
            self.journal("déployé", msg, deployed=True)
            self.say(msg)
            return 0

        # 3. retour arrière
        self.say(f"❌ Test de santé en échec après déploiement ({' ; '.join(bad)[:500]}) → retour à {self.prev[repo][:7]}…")
        errs = self.rollback()
        rc, out = self.restart()
        bad2 = [f"redémarrage : {out[-300:]}"] if rc else self.healthy()
        if not errs and not bad2:
            msg = (f"↩️ Retour arrière effectué : `{job['depot']}` est revenu à {self.prev[repo][:7]}, services relancés et sains. "
                   f"La branche `{branch}` est conservée pour correction. Échec initial : {' ; '.join(bad)[:400]}")
            self.journal("annulé (retour arrière)", msg)
        else:
            msg = (f"🆘 Retour arrière INCOMPLET sur `{job['depot']}` — intervention manuelle nécessaire. "
                   f"Reset : {'; '.join(errs) or 'ok'}. Santé après retour : {'; '.join(bad2) or 'ok'}.")
            self.journal("échec (retour arrière incomplet)", msg)
        self.say(msg)
        return 1

    def fail(self, why: str) -> int:
        msg = f"❌ Déploiement de `{self.job['depot']}` abandonné, rien n'a été redémarré : {why}"
        self.journal("échec", msg)
        self.say(msg)
        return 1


def main() -> int:
    path = sys.argv[1]
    with open(path, encoding="utf-8") as f:
        job = json.load(f)
    try:
        os.unlink(path)
    except OSError:
        pass
    return Deploy(job).run()


if __name__ == "__main__":
    sys.exit(main())
