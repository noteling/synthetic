"""M25 test 8 (CLI part): every former literal-human default resolves from humans.json default_human."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from synthetics import capture

ROOT = Path(__file__).resolve().parents[1]


class HumansCliTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = dict(os.environ, PYTHONPATH=str(ROOT))
        self.env.pop("SYNTHETICS_SESSION_RUN", None)

    def cli(self, home, *args):
        out = subprocess.run([sys.executable, "-m", "synthetics", "--home", str(home)] + list(args),
                             capture_output=True, text=True, env=self.env, cwd=str(ROOT))
        return out

    def test_init_human_writes_config_and_is_default_everywhere(self):
        home = self.root / "a"
        out = self.cli(home, "init", "--name", "A", "--role", "r", "--human", "ana",
                       "--human-channel", "chat:")
        self.assertEqual(out.returncode, 0, out.stderr)
        cfg = capture.humans(home)
        self.assertEqual((cfg["default_human"], cfg["senders"], cfg["channel_prefixes"]), ("ana", ["ana"], ["chat:"]))
        self.assertEqual(cfg["limit_bypass"]["senders"], [])  # bypass is an explicit operator decision
        self.assertEqual(oct((home / "humans.json").stat().st_mode & 0o777), "0o644")
        out = self.cli(home, "entities", "ana")
        self.assertIn('"ana"', out.stdout)
        target = self.root / "t"
        target.mkdir()
        out = self.cli(home, "access", "grant", str(target), "--mode", "read", "--purpose", "p")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout)["granted_by"], "ana")
        # A second --human never rewrites existing trust config.
        out = self.cli(home, "init", "--name", "A", "--role", "r", "--human", "bob")
        self.assertNotEqual(out.returncode, 0)
        self.assertEqual(capture.default_human(home), "ana")

    def test_no_file_and_no_flag_fails_clearly(self):
        home = self.root / "b"
        out = self.cli(home, "init", "--name", "B", "--role", "r")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("default_human", out.stderr + out.stdout)
        # init without --human leaves an empty v1 file (no humans; doctor warns) even when it fails on --by.
        self.assertEqual(capture.humans(home)["senders"], [])
        out = self.cli(home, "init", "--name", "B", "--role", "r", "--by", "carol")
        self.assertEqual(out.returncode, 0, out.stderr)
        target = self.root / "t2"
        target.mkdir()
        out = self.cli(home, "access", "grant", str(target), "--mode", "read", "--purpose", "p")
        self.assertNotEqual(out.returncode, 0)
        self.assertIn("default_human", out.stderr + out.stdout)
        out = self.cli(home, "access", "grant", str(target), "--mode", "read", "--purpose", "p", "--by", "carol")
        self.assertEqual(out.returncode, 0, out.stderr)

    def test_human_channel_needs_human(self):
        out = self.cli(self.root / "c", "init", "--name", "C", "--role", "r", "--human-channel", "x:")
        self.assertNotEqual(out.returncode, 0)
        self.assertFalse((self.root / "c" / "humans.json").exists())


if __name__ == "__main__":
    unittest.main()
