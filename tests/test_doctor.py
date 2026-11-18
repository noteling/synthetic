"""M25 §6 doctor, design test 9 (--compare-legacy) and C2/C3 reporting."""
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from synthetics import capture, doctor
from synthetics.store import Store
from tests.humans_fixture import HOST_HUMANS, HOST_LEGACY, write_host_humans

ROOT = Path(__file__).resolve().parents[1]


def _tree_digest(home):
    out = {}
    for path in sorted(Path(home).rglob("*")):
        # SQLite's own -shm/-wal side files: a read-only WAL reader may create them; they hold no data.
        if path.is_file() and not path.name.endswith(("-shm", "-wal")):
            out[str(path.relative_to(home))] = (hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_mode)
    return out


def _codes(report, level=None):
    return {f["code"] for f in report["findings"] if level is None or f["level"] == level}


class DoctorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "home"
        store = Store(self.home)
        store.initialize("Test", "tester")
        store.feed("david", "hello", source="codex-desktop:t1", external_id="e1")
        store.feed("codex-desktop", "notice", source="operator-runtime", external_id="e2")
        store.close()
        self.legacy = Path(self.tmp.name) / "legacy.json"
        self.legacy.write_text(json.dumps(HOST_LEGACY), encoding="utf-8")
        (self.home / "runtime.json").write_text(json.dumps({
            "claude": sys.executable, "model": "claude-test", "budget": 1, "timeout": 60, "poll": 2, "retry": 60,
            "daily_limit": 10, "limit_notify": "david", "limit_allow": ["david"]}), encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_humans_warns_and_channel_unprovisioned(self):
        report = doctor.diagnose(self.home, launchd=lambda label: (False, None))
        self.assertIn("humans_missing", _codes(report, "warning"))
        self.assertIn("channel_unprovisioned", _codes(report, "info"))
        self.assertTrue(report["ok"], report["findings"])
        self.assertEqual(report["schema_version"], Store.SCHEMA_VERSION)
        self.assertEqual(report["outbox"]["undelivered"], 0)
        self.assertNotIn("limit_notify_unroutable", _codes(report))  # no channels: pre-contract, not flagged

    def test_backlog25_limit_notify_unroutable(self):
        store = Store(self.home)
        store.channel_register("fieldpost", "0" * 64, ["fieldpost"], "operator", routes=["codex-manager"])
        store.close()
        report = doctor.diagnose(self.home, launchd=lambda label: (False, None))
        self.assertIn("limit_notify_unroutable", _codes(report, "warning"))  # limit_notify is "david"
        store = Store(self.home)
        store.channel_register("telegram", "1" * 64, ["telegram:"], "operator", routes=["david"])
        store.close()
        report = doctor.diagnose(self.home, launchd=lambda label: (False, None))
        self.assertNotIn("limit_notify_unroutable", _codes(report))

    def test_home_path_with_uri_metacharacters(self):
        # Review N1: ?, # and % in the home path must not break the read-only URI.
        home = Path(self.tmp.name) / "we?ird#100%" / "home"
        store = Store(home)
        store.initialize("Test", "tester")
        store.close()
        report = doctor.diagnose(home, launchd=lambda label: (False, None))
        self.assertNotIn("home_unreadable", _codes(report), report["findings"])
        self.assertEqual(report["schema_version"], Store.SCHEMA_VERSION)

    def test_doctor_only_reads(self):
        write_host_humans(self.home)
        before = _tree_digest(self.home)
        doctor.diagnose(self.home, compare=self.legacy, launchd=lambda label: (True, 123), label="com.synthetics.worker.x")
        self.assertEqual(before, _tree_digest(self.home))

    def test_compare_legacy_zero_diffs_on_host_config(self):
        write_host_humans(self.home)
        report = doctor.diagnose(self.home, compare=self.legacy)
        cmp = report["compare_legacy"]
        self.assertEqual(cmp["diffs"], [])
        self.assertEqual(cmp["events"], 2)
        self.assertGreater(cmp["probes"], 0)
        # C3: unauthenticated forwards from david don't bypass today either: a warning, not a diff
        self.assertIn("unauthenticated_no_bypass", _codes(report, "warning"))
        # C1: operator notices now map to secretary: info only
        self.assertIn("operator_as_secretary", _codes(report, "info"))
        self.assertTrue(report["ok"], report["findings"])

    def test_compare_legacy_reports_altered_file(self):
        altered = json.loads(json.dumps(HOST_HUMANS))
        altered["limit_bypass"]["channel_prefixes"] = ["telegram:"]  # drops desktop bypass
        capture.write_humans(self.home, altered)
        report = doctor.diagnose(self.home, compare=self.legacy)
        diffs = report["compare_legacy"]["diffs"]
        self.assertTrue(diffs)
        self.assertTrue(all(d["field"] == "limit_bypass" for d in diffs))
        self.assertIn("legacy_diff", _codes(report, "error"))
        self.assertFalse(report["ok"])

        altered = json.loads(json.dumps(HOST_HUMANS))
        altered["senders"] = ["someone-else"]
        capture.write_humans(self.home, altered)
        diffs = doctor.diagnose(self.home, compare=self.legacy)["compare_legacy"]["diffs"]
        self.assertTrue(any(d["field"] == "role" and d["legacy"] == "human" for d in diffs))

    def test_invalid_humans_and_launch_agent_and_key_file(self):
        (self.home / "humans.json").write_text("[]", encoding="utf-8")
        key = Path(self.tmp.name) / "c.key"
        key.write_text("secret", encoding="utf-8")
        key.chmod(0o644)
        report = doctor.diagnose(self.home, label="com.synthetics.worker.none", key_files=[str(key)],
                                 launchd=lambda label: (False, None))
        self.assertTrue({"humans_invalid", "launch_agent_not_loaded", "key_file_exposed"} <= _codes(report, "error"))
        self.assertFalse(report["ok"])

    def test_failure_streak_and_missing_home(self):
        store = Store(self.home)
        for _ in range(3):
            run = store.run_begin()
            store.run_fail(run["id"], "boom")
        store.close()
        self.assertIn("failure_streak", _codes(doctor.diagnose(self.home), "error"))
        self.assertIn("home_missing", _codes(doctor.diagnose(Path(self.tmp.name) / "nope"), "error"))
        self.assertFalse((Path(self.tmp.name) / "nope").exists())

    def test_cli_exit_codes(self):
        env = dict(os.environ, PYTHONPATH=str(ROOT))
        env.pop("SYNTHETICS_SESSION_RUN", None)
        write_host_humans(self.home)
        ok = subprocess.run([sys.executable, "-m", "synthetics", "--home", str(self.home), "doctor",
                             "--compare-legacy", str(self.legacy), "--json"], capture_output=True, text=True, env=env, cwd=str(ROOT))
        self.assertEqual(ok.returncode, 0, ok.stderr)
        self.assertEqual(json.loads(ok.stdout)["compare_legacy"]["diffs"], [])
        (self.home / "humans.json").write_text("{", encoding="utf-8")
        bad = subprocess.run([sys.executable, "-m", "synthetics", "--home", str(self.home), "doctor"],
                             capture_output=True, text=True, env=env, cwd=str(ROOT))
        self.assertEqual(bad.returncode, 1)
        self.assertIn("humans_invalid", bad.stdout)


if __name__ == "__main__":
    unittest.main()
