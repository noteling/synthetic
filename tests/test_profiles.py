"""Role profiles (operating defaults P1): shipped, versioned role guidance selected per home by the owner.

Guards (pass before and after): with no profile.json the system prompt is exactly employee.md + host rules +
Byline, and a run record carries no profile field. Behaviour tests need synthetics.profiles (new module), so on
the base tree they fail all at once; each guard is also proven by a one-guard mutant (PR evidence).
"""
import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics import authority, brain, doctor
from synthetics.cli import main
from synthetics.store import Store

PROMPTS = Path(brain.__file__).parent / "prompts"
ROLES = PROMPTS / "roles"
COMPANY = {"policies": {"host.rules": {"id": "co-test", "text": "COMPANY HOST RULE"}}}

# Portability (K2): pattern checks only. Names of people of a particular installation are never committed; an
# optional local file (one name per line) named by SYNTHETICS_PORTABILITY_NAMES adds them and is skipped if absent.
PORTABILITY = {
    "absolute path": re.compile(r"(?<![\w.])(/Users/|/home/|/private/|/var/folders/|~/|[A-Za-z]:\\\\)"),
    "record id": re.compile(r"\b(pm|event|work|question|memory|run|message|runtime|access|reminder|co|ro|seed)"
                            r"-[0-9a-f]{8,}\b"),
    "loopback port": re.compile(r"\b(127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\]):\d+"),
}


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in (authority.SESSION_ENV, authority.HOME_ENV)}
    env.update(extra)
    return env


def _profiles():
    from synthetics import profiles
    return profiles


def _committed_prompt_files():
    return [PROMPTS / "employee.md"] + sorted(ROLES.glob("*.md"))


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = (self.root / "dev").resolve()
        with Store(self.home) as s:
            s.initialize("Dev", "developer")
        self.env_patch = mock.patch.dict(os.environ, _clean_env(), clear=True)
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def write(self, data):
        (self.home / "profile.json").write_text(data if isinstance(data, str) else json.dumps(data))

    def cli(self, *argv, session_run=None, home=None):
        home = home or self.home
        env = _clean_env(**({authority.SESSION_ENV: session_run, authority.HOME_ENV: str(home)}
                            if session_run else {}))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = main(["--home", str(home), *argv])
        return code, out.getvalue(), err.getvalue()

    def audit(self, action):
        with Store(self.home) as s:
            return sorted((r for r in s.audit_list() if r.get("action") == action), key=lambda r: r["seq"])


class NoProfileGuards(Base):
    """Pass on the base tree too: without profile.json nothing changes."""

    def test_prompt_without_profile_is_employee_plus_host_rules_plus_byline(self):
        expected = (PROMPTS / "employee.md").read_text()
        for label, text in brain.host_rules(self.home):
            expected += "\n\nHost rules (" + label + "):\n" + text + "\n"
        self.assertEqual(brain.system_prompt(self.home), expected)
        self.assertNotIn("Role defaults", brain.system_prompt(self.home))

    def test_run_record_profile_is_none_without_profile_json(self):
        # #85 L1: the key is always recorded (None = no profile at run start), so a record without it is detectable
        with Store(self.home) as s:
            run = s.run_begin()
            self.assertIn("profile", run)
            self.assertIsNone(run["profile"])
            s.run_finish(run["id"], "ok")
            self.assertIsNone(s.run_get(run["id"])["profile"])


class Shipped(unittest.TestCase):
    def test_shipped_profiles_have_header_cap_and_guidance_only_note(self):
        profiles = _profiles()
        self.assertEqual(profiles.available(), ["developer", "manager"])
        for name in profiles.available():
            prof = profiles.shipped(name)
            raw = (ROLES / (name + ".md")).read_bytes()
            self.assertLessEqual(len(raw), profiles.MAX_BYTES, name)
            self.assertEqual(raw.decode().split("\n", 1)[0], "<!-- profile: %s; version: %d -->"
                             % (name, prof["version"]))
            self.assertIn("grants no authority", raw.decode())  # K1, in the file itself
            self.assertIn("never overrides host rules", raw.decode())

    def test_portability_patterns(self):
        hits = []
        for f in _committed_prompt_files():
            for n, line in enumerate(f.read_text().splitlines(), 1):
                for what, rx in PORTABILITY.items():
                    if rx.search(line):
                        hits.append("%s:%d %s: %s" % (f.name, n, what, line.strip()[:100]))
        self.assertEqual(hits, [])

    def test_portability_names_from_optional_local_file(self):
        names_file = os.environ.get("SYNTHETICS_PORTABILITY_NAMES")
        if not names_file or not Path(names_file).is_file():
            self.skipTest("no local names file (SYNTHETICS_PORTABILITY_NAMES); pattern checks still ran")
        names = [n.strip() for n in Path(names_file).read_text().splitlines() if n.strip()]
        hits = [(f.name, n) for f in _committed_prompt_files() for n in names
                if re.search(r"\b%s\b" % re.escape(n), f.read_text())]
        self.assertEqual(hits, [])

    def test_portability_patterns_catch_the_cases(self):
        samples = {"absolute path": "see /Users/someone/x", "record id": "per pm-0123abcd",
                   "loopback port": "open 127.0.0.1:8765"}
        for what, text in samples.items():
            self.assertTrue(PORTABILITY[what].search(text), what)

    def test_oversize_or_bad_header_is_refused(self):
        profiles = _profiles()
        with tempfile.TemporaryDirectory() as d:
            roles = Path(d)
            (roles / "big.md").write_text("<!-- profile: big; version: 1 -->\n" + "x" * profiles.MAX_BYTES)
            (roles / "odd.md").write_text("<!-- profile: other; version: 1 -->\nbody\n")
            (roles / "nover.md").write_text("# no header\n")
            with mock.patch.object(profiles, "ROLES_DIR", roles):
                for name in ("big", "odd", "nover", "missing"):
                    with self.assertRaises(profiles.ProfileError, msg=name):
                        profiles.shipped(name)


class Prompt(Base):
    def test_block_order_profile_before_host_rules_and_byline(self):
        self.write({"v": 1, "profile": "developer"})
        (self.home / "prompt.local.md").write_text("LOCAL HOST RULE")
        (self.home / "attribution.json").write_text(json.dumps({"v": 1, "scopes": {"channels": ["telegram"]}}))
        with mock.patch("synthetics.roster.company_state_for_home", return_value=COMPANY):
            text = brain.system_prompt(self.home)
        employee = (PROMPTS / "employee.md").read_text()
        self.assertTrue(text.startswith(employee))
        self.assertEqual(text.count("Role defaults (profile"), 1)
        at = [text.index(s) for s in ("Role defaults (profile developer v",
                                      "# Role defaults: developer",
                                      "COMPANY HOST RULE", "LOCAL HOST RULE")]
        self.assertEqual(at, sorted(at))
        self.assertGreater(text.index("LOCAL HOST RULE"), len(employee))
        byline_at = text.rfind("Synthetic-Employee")
        self.assertGreater(byline_at, text.index("LOCAL HOST RULE"))

    def test_label_names_version_and_sha(self):
        profiles = _profiles()
        self.write({"v": 1, "profile": "manager"})
        prof = profiles.shipped("manager")
        text = brain.system_prompt(self.home)
        self.assertIn("Role defaults (profile manager v%d, sha256 %s; guidance only"
                      % (prof["version"], prof["sha256"][:12]), text)
        self.assertIn(prof["text"].strip(), text)

    def test_fail_closed_on_invalid_profile_json(self):
        for bad in ("{not json", json.dumps({"v": 1, "profile": "nobody"}), json.dumps({"v": 2, "profile": "manager"}),
                    json.dumps({"v": 1, "profile": "manager", "extra": 1}), json.dumps(["manager"])):
            self.write(bad)
            with self.assertRaises(brain.BrainError, msg=bad):
                brain.system_prompt(self.home)

    def test_system_prompt_cli_shows_label(self):
        self.write({"v": 1, "profile": "developer"})
        code, out, err = self.cli("system-prompt")
        self.assertEqual(code, 0, err)
        self.assertIn("Role defaults (profile developer v", out)


class Cli(Base):
    def test_owner_set_show_clear_audited_0600(self):
        profiles = _profiles()
        code, out, err = self.cli("profile", "set", "developer")
        self.assertEqual(code, 0, err)
        p = self.home / "profile.json"
        self.assertEqual(json.loads(p.read_text()), {"v": 1, "profile": "developer"})
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        code, out, _ = self.cli("profile", "show")
        shown = json.loads(out)
        self.assertEqual((shown["name"], shown["sha256"]), ("developer", profiles.shipped("developer")["sha256"]))
        code, _, err = self.cli("profile", "set", "manager")
        self.assertEqual(code, 0, err)
        rows = self.audit("profile.set")
        self.assertEqual([(r["data"]["from"] or {}).get("name") for r in rows], [None, "developer"])
        self.assertEqual([r["data"]["to"]["name"] for r in rows], ["developer", "manager"])
        code, _, err = self.cli("profile", "clear")
        self.assertEqual(code, 0, err)
        self.assertFalse(p.exists())
        self.assertEqual(self.audit("profile.cleared")[0]["data"]["from"]["name"], "manager")

    def test_unknown_profile_refused_and_old_file_kept(self):
        self.cli("profile", "set", "developer")
        code, _, err = self.cli("profile", "set", "admin")
        self.assertNotEqual(code, 0)
        self.assertEqual(json.loads((self.home / "profile.json").read_text())["profile"], "developer")

    def test_session_cannot_set_or_clear_its_profile(self):
        with Store(self.home) as s:
            run = s.run_begin()["id"]
        code, _, err = self.cli("profile", "set", "manager", session_run=run)
        self.assertNotEqual(code, 0)
        self.assertIn("owner_only", err)
        self.assertFalse((self.home / "profile.json").exists())
        self.write({"v": 1, "profile": "developer"})
        code, _, err = self.cli("profile", "clear", session_run=run)
        self.assertNotEqual(code, 0)
        self.assertTrue((self.home / "profile.json").exists())
        whats = sorted(r["data"]["what"] for r in self.audit("authority.refused"))
        self.assertEqual(whats, ["profile clear", "profile set"])

    def test_init_with_profile_writes_and_audits(self):
        home = (self.root / "mgr").resolve()
        code, out, err = self.cli("init", "--name", "M", "--role", "manager", "--human", "owner",
                                  "--profile", "manager", home=home)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(out)["profile"]["name"], "manager")
        self.assertEqual(json.loads((home / "profile.json").read_text()), {"v": 1, "profile": "manager"})
        with Store(home) as s:
            rows = [r for r in s.audit_list() if r.get("action") == "profile.set"]
        self.assertEqual(rows[0]["data"]["to"]["name"], "manager")

    def test_init_without_profile_writes_nothing_and_unknown_is_refused_before_home(self):
        home = (self.root / "plain").resolve()
        code, out, err = self.cli("init", "--name", "P", "--role", "dev", "--human", "owner", home=home)
        self.assertEqual(code, 0, err)
        self.assertNotIn("profile", json.loads(out))
        self.assertFalse((home / "profile.json").exists())
        bad = (self.root / "bad").resolve()
        code, _, _ = self.cli("init", "--name", "B", "--role", "dev", "--human", "owner", "--profile", "boss",
                              home=bad)
        self.assertNotEqual(code, 0)
        self.assertFalse((bad / "state.sqlite3").exists())


class Evidence(Base):
    def test_run_record_carries_the_profile_triple(self):
        profiles = _profiles()
        self.write({"v": 1, "profile": "developer"})
        prof = profiles.shipped("developer")
        with Store(self.home) as s:
            run = s.run_begin()
            want = {"name": "developer", "version": prof["version"], "sha256": prof["sha256"]}
            self.assertEqual(run["profile"], want)
            s.run_finish(run["id"], "ok")
            self.assertEqual(s.run_get(run["id"])["profile"], want)
            started = [r for r in s.audit_list() if r.get("action") == "run.started"]
        self.assertEqual(started[-1]["data"].get("profile"), want)

    def test_export_carries_triple_and_older_runs_export_as_before(self):
        from synthetics import ledgerline
        with Store(self.home) as s:
            old = s.run_begin()
            s.run_finish(old["id"], "ok")
        self.write({"v": 1, "profile": "developer"})
        with Store(self.home) as s:
            new = s.run_begin()
            s.run_finish(new["id"], "ok")
        lines = {l["run_id"]: l for l in ledgerline.export_runs(self.home) if l["kind"] == "run"}
        self.assertNotIn("profile", lines[old["id"]])  # recorded None exports exactly as before
        old_audit = [l for l in ledgerline.export_runs(self.home, audit=True)
                     if l["kind"] == "audit" and l.get("record_id") == old["id"] and l.get("action", "").startswith("run.")]
        self.assertTrue(old_audit)
        self.assertTrue(all("profile" not in l.get("fields", {}) for l in old_audit))
        self.assertEqual(lines[new["id"]]["profile"]["name"], "developer")
        self.assertEqual(len(lines[new["id"]]["profile"]["sha256"]), 64)
        audit = [l for l in ledgerline.export_runs(self.home, audit=True)
                 if l["kind"] == "audit" and l.get("action") == "run.finished" and l.get("record_id") == new["id"]]
        self.assertEqual(audit[0]["fields"]["profile"]["name"], "developer")
        self.write("{broken")
        with Store(self.home) as s:
            bad = s.run_begin()
        line = [l for l in ledgerline.export_runs(self.home, run=bad["id"]) if l["kind"] == "run"][0]
        self.assertEqual(line["profile"], {"error": "ProfileError"})  # never the detail text

    def test_invalid_profile_never_fails_run_begin(self):
        self.write("{broken")
        with Store(self.home) as s:
            run = s.run_begin()
        self.assertEqual(run["profile"]["error"], "ProfileError")

    def test_doctor_reports_profile_and_flags_invalid(self):
        self.write({"v": 1, "profile": "manager"})
        report = doctor.diagnose(self.home, launchd=lambda label: None)
        self.assertEqual(report["profile"]["name"], "manager")
        self.assertNotIn("profile_invalid", [f["code"] for f in report["findings"]])
        self.write({"v": 1, "profile": "ghost"})
        report = doctor.diagnose(self.home, launchd=lambda label: None)
        self.assertIn("profile_invalid", [f["code"] for f in report["findings"]])
        self.assertFalse(report.get("ok"))


if __name__ == "__main__":
    unittest.main()
