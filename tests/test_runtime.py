import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path

from synthetics import runtime


def baseline(**overrides):
    value = dict(claude=sys.executable, model="claude-opus-5-5", budget=100.0, timeout=600.0, poll=2.0,
                 retry=60.0, daily_limit=75.0, limit_notify="david", limit_allow=["david"])
    value.update(overrides)
    return value


class ValidateTests(unittest.TestCase):
    def test_accepts_current_production_settings(self):
        self.assertEqual(runtime.validate(baseline())["model"], "claude-opus-5-5")

    def test_rejects_each_bad_field(self):
        cases = [dict(claude="relative/claude"), dict(claude="/nonexistent/claude"), dict(model="gpt-4"),
                 dict(budget=0), dict(budget=100.5), dict(budget=float("nan")), dict(budget=True),
                 dict(timeout=10), dict(poll=0), dict(retry=1), dict(daily_limit=-1),
                 dict(limit_notify="bad name"), dict(limit_allow="david"), dict(limit_allow=["x"] * 9)]
        for case in cases:
            with self.subTest(case=case), self.assertRaises(ValueError):
                runtime.validate(baseline(**case))

    def test_rejects_unknown_and_missing_fields(self):
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            runtime.validate(dict(baseline(), home="/tmp"))
        config = baseline()
        del config["timeout"]
        with self.assertRaisesRegex(ValueError, "lacks: timeout"):
            runtime.validate(config)

    def test_allowlist(self):
        with self.assertRaisesRegex(ValueError, "not in the allowed"):
            runtime.validate(baseline(model="claude-other-1"), allowed_models={"claude-opus-5-5"})
        runtime.validate(baseline(), allowed_models={"claude-opus-5-5"})

    def test_digest_ignores_metadata_and_allow_order(self):
        a = runtime.validate(baseline(limit_allow=["b", "a"]))
        b = runtime.validate(dict(baseline(limit_allow=["a", "b"]), request_id="r1", version=1))
        self.assertEqual(runtime.digest(a), runtime.digest(b))


class LoadTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.home = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def write(self, name, value):
        (self.home / name).write_text(value if isinstance(value, str) else json.dumps(value))

    def test_first_start_seeds_both_files_privately(self):
        result = runtime.load(self.home, baseline())
        self.assertEqual((result["source"], result["problems"]), ("baseline", []))
        for name in (runtime.CONFIG_FILE, runtime.LAST_GOOD_FILE):
            path = self.home / name
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(runtime.digest(json.loads(path.read_text())), result["sha"])
        self.assertEqual(runtime.load(self.home, baseline())["source"], "file")

    def test_file_wins_and_carries_request_id(self):
        self.write(runtime.CONFIG_FILE, dict(baseline(timeout=300), request_id="runtime-1"))
        result = runtime.load(self.home, baseline())
        self.assertEqual((result["source"], result["effective"]["timeout"], result["request_id"]),
                         ("file", 300.0, "runtime-1"))
        self.assertNotIn("request_id", result["effective"])

    def test_invalid_file_falls_back_to_last_good_and_reports(self):
        self.write(runtime.CONFIG_FILE, baseline(model="nope"))
        self.write(runtime.LAST_GOOD_FILE, baseline(timeout=900))
        result = runtime.load(self.home, baseline())
        self.assertEqual((result["source"], result["effective"]["timeout"]), ("last-good", 900.0))
        self.assertIn("invalid", result["problems"][0])

    def test_garbage_everywhere_falls_back_to_baseline_without_raising(self):
        self.write(runtime.CONFIG_FILE, "{not json")
        self.write(runtime.LAST_GOOD_FILE, "[]")
        result = runtime.load(self.home, baseline())
        self.assertEqual(result["source"], "baseline")
        self.assertEqual(len(result["problems"]), 2)
        # The broken file is left for inspection, not overwritten by the baseline.
        self.assertEqual((self.home / runtime.CONFIG_FILE).read_text(), "{not json")

    def test_missing_file_with_last_good_restores_it(self):
        self.write(runtime.LAST_GOOD_FILE, baseline(timeout=900))
        result = runtime.load(self.home, baseline())
        self.assertEqual(result["source"], "last-good")
        self.assertIn("restored", result["problems"][0])
        self.assertEqual(runtime.load(self.home, baseline())["source"], "file")

    def test_disallowed_model_on_disk_falls_back(self):
        self.write(runtime.CONFIG_FILE, baseline(model="claude-other-1"))
        result = runtime.load(self.home, baseline(), allowed_models={"claude-opus-5-5"})
        self.assertEqual((result["source"], result["effective"]["model"]), ("baseline", "claude-opus-5-5"))

    def test_invalid_baseline_still_starts_and_says_so(self):
        result = runtime.load(self.home, baseline(claude="/nonexistent/claude"))
        self.assertEqual(result["source"], "baseline-unvalidated")
        self.assertIn("baseline invalid", result["problems"][0])
        self.assertFalse((self.home / runtime.CONFIG_FILE).exists())

    def test_write_atomic_leaves_no_temporaries(self):
        runtime.write_atomic(self.home / runtime.CONFIG_FILE, runtime.validate(baseline()), request_id="r")
        self.assertEqual(sorted(os.listdir(self.home)), [runtime.CONFIG_FILE])


if __name__ == "__main__":
    unittest.main()
