"""#15 Byline r1 (Rowan's PR #70 review, F1 + L1-L3 + n1/n2). Each test fails on 82e5224 unless marked guard."""
import json
import os
import re
import subprocess
import unittest
from pathlib import Path

from synthetics import authority, byline
from synthetics.store import Store

from tests import test_i15_byline as base

_clean_env = base._clean_env


class _HookBase(base.Hook):
    pass


# Inherited tests already run in test_i15_byline; null them here so this module adds only its own.
for _name in [n for n in dir(base.Hook) if n.startswith("test_")]:
    setattr(_HookBase, _name, None)


class F1HookTargetInsideHome(_HookBase):
    def outside_repo(self):
        out = self.root / "elsewhere"
        out.mkdir()
        self.git(out, "init", "-q")
        self.git(out, "config", "user.name", "david")
        self.git(out, "config", "user.email", "david@example.invalid")
        self.git(out, "config", "commit.gpgsign", "false")
        self.git(out, "remote", "add", "origin", "git@github.com:noteling/synthetic.git")
        self.git(out, "commit", "-q", "--allow-empty", "-m", "root")
        return out

    def refused_rows(self):
        with Store(self.home) as s:
            return [r["data"] for r in s.audit_list() if r.get("action") == "authority.refused"
                    and (r.get("data") or {}).get("what") == "byline hook install"]

    def test_session_refused_for_worktree_of_outside_repo(self):
        self.configure()
        out = self.outside_repo()
        wt = self.home / "workspace" / "wt"
        wt.parent.mkdir(parents=True, exist_ok=True)
        self.git(out, "worktree", "add", "-q", str(wt))
        code, _, err = self.cli("byline", "hook", "install", "--repo", str(wt), session=True)
        self.assertNotEqual(code, 0)
        self.assertIn("own_home_only", err)
        self.assertFalse((out / ".git" / "hooks" / "prepare-commit-msg").exists())
        self.assertEqual(len(self.refused_rows()), 1)
        # the owner may still install it (owner's call)
        code, out_, err = self.cli("byline", "hook", "install", "--repo", str(wt))
        self.assertEqual(code, 0, err)
        self.assertTrue((out / ".git" / "hooks" / "prepare-commit-msg").exists())

    def test_session_refused_for_hookspath_outside_home(self):
        self.configure()
        repo = self.repo("syn", "git@github.com:noteling/synthetic.git")
        hooks = self.root / "outside-hooks"
        self.git(repo, "config", "core.hooksPath", str(hooks))
        code, _, err = self.cli("byline", "hook", "install", "--repo", str(repo), session=True)
        self.assertNotEqual(code, 0)
        self.assertIn("own_home_only", err)
        self.assertFalse(hooks.exists())
        self.assertEqual(len(self.refused_rows()), 1)

    def test_session_refused_for_hooks_symlink_outside_home(self):
        self.configure()
        repo = self.repo("syn", "git@github.com:noteling/synthetic.git")
        outside = self.root / "outside-hooks"
        outside.mkdir()
        hooks = repo / ".git" / "hooks"
        subprocess.run(["rm", "-rf", str(hooks)], check=True)
        hooks.symlink_to(outside)
        code, _, err = self.cli("byline", "hook", "install", "--repo", str(repo), session=True)
        self.assertNotEqual(code, 0)
        self.assertIn("own_home_only", err)
        self.assertFalse((outside / "prepare-commit-msg").exists())

    def test_guard_session_installs_hookspath_inside_home(self):
        """Guard (passes on 82e5224 too): a hooksPath inside the home is fine."""
        self.configure()
        repo = self.repo("syn", "git@github.com:noteling/synthetic.git")
        hooks = self.home / "workspace" / "hooks"
        self.git(repo, "config", "core.hooksPath", str(hooks))
        self.install(repo, session=True)
        self.assertTrue((hooks / "prepare-commit-msg").exists())
        self.assertIn("Synthetic-Employee", self.commit(repo, "mine"))


class L1NotOthersWork(_HookBase):
    def setUp(self):
        super().setUp()
        self.configure()
        self.r = self.repo("syn", "git@github.com:noteling/synthetic.git")
        self.install(self.r)

    def test_amend_no_edit_of_owner_commit_gets_no_trailer(self):
        self.assertNotIn("Synthetic-Employee", self.commit(self.r, "owner", session=False))
        env = _clean_env(**{authority.SESSION_ENV: self.run, authority.HOME_ENV: str(self.home)})
        self.git(self.r, "commit", "-q", "--amend", "--no-edit", env=env)
        self.assertNotIn("Synthetic-Employee", self.git(self.r, "log", "-1", "--format=%B").stdout)

    def test_cherry_pick_gets_no_trailer(self):
        self.commit(self.r, "base", session=False)
        self.git(self.r, "checkout", "-q", "-b", "side")
        (self.r / "s").write_text("s")
        self.git(self.r, "add", "s")
        self.git(self.r, "-c", "user.name=someone", "commit", "-q", "-m", "someone's work")
        self.git(self.r, "checkout", "-q", "-")
        env = _clean_env(**{authority.SESSION_ENV: self.run, authority.HOME_ENV: str(self.home)})
        self.git(self.r, "cherry-pick", "side", env=env)
        log = self.git(self.r, "log", "-1", "--format=%an%n%B").stdout
        self.assertTrue(log.startswith("someone"))
        self.assertNotIn("Synthetic-Employee", log)

    def test_guard_own_commit_still_trailed(self):
        """Guard: an ordinary session commit keeps its trailer."""
        self.assertIn("Synthetic-Employee", self.commit(self.r, "mine"))


class L2FailOpen(_HookBase):
    def test_missing_python_does_not_abort_commit(self):
        self.configure()
        repo = self.repo("syn", "git@github.com:noteling/synthetic.git")
        byline.hook_install(self.home, repo, python="/nonexistent/python3")
        log = self.commit(repo, "still commits")  # check=True: a hook failure would raise
        self.assertIn("still commits", log)
        self.assertNotIn("Synthetic-Employee", log)


class L3OwnerAudited(base.Base):
    def test_owner_set_and_clear_are_audited(self):
        cfg = self.root / "cfg.json"
        cfg.write_text(json.dumps(base.SCOPED))
        code, out, err = self.cli("byline", "set", "--file", str(cfg))
        self.assertEqual(code, 0, err)
        sha8 = byline.footer_sha8((self.home / "attribution.json").read_text(encoding="utf-8"))
        code, _, err = self.cli("byline", "clear")
        self.assertEqual(code, 0, err)
        with Store(self.home) as s:
            rows = [(r["action"], r["data"]) for r in s.audit_list()
                    if r.get("action") in ("byline.set", "byline.cleared")]
        self.assertEqual(sorted(a for a, _ in rows), ["byline.cleared", "byline.set"])
        data = dict(rows)
        self.assertEqual(data["byline.set"]["sha8"], sha8)
        self.assertEqual(data["byline.set"]["actor"], "owner")
        self.assertTrue(data["byline.cleared"]["removed"])


class N2NoFixedTempName(base.Base):
    def test_set_does_not_write_through_a_planted_tmp_symlink(self):
        victim = self.root / "victim.txt"
        victim.write_text("keep")
        (self.home / "attribution.json.tmp").symlink_to(victim)
        cfg = self.root / "cfg.json"
        cfg.write_text(json.dumps(base.SCOPED))
        code, _, err = self.cli("byline", "set", "--file", str(cfg))
        self.assertEqual(code, 0, err)
        self.assertEqual(victim.read_text(), "keep")
        self.assertEqual(json.loads((self.home / "attribution.json").read_text()), base.SCOPED)
        self.assertEqual([p.name for p in self.home.iterdir() if p.name.endswith(".tmp")], ["attribution.json.tmp"])


URLS = ["git@github.com:noteling/synthetic.git", "https://github.com/noteling/synthetic.git",
        "ssh://git@github.com:22/noteling/synthetic.git", "https://user:tok@github.com/noteling/synthetic",
        "https://github.com:443/noteling/synthetic/", "file:///srv/git/repo.git", "GITHUB.COM/Noteling/Synthetic",
        "git@host:2020/repo", "github.com/noteling/synthetic", "ssh://host/path/to/repo.git/", "/local/path/repo",
        "  git@gitlab.example.com:group/sub/proj.git  "]


class N1NormParity(unittest.TestCase):
    def test_hook_norm_matches_normalize_remote(self):
        src = byline.HOOK
        m = re.search(r"^def norm\(u\):\n(?:    .*\n)+", src, re.M)
        self.assertIsNotNone(m)
        ns = {"re": re}
        exec(m.group(0), ns)
        for url in URLS:
            self.assertEqual(ns["norm"](url), byline.normalize_remote(url), url)


if __name__ == "__main__":
    unittest.main()
