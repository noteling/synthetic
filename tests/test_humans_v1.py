"""M25 design test 8 (unit part) + C1: humans.json v1 roles and limit bypass."""

import json
import tempfile
import unittest
from pathlib import Path

from synthetics import capture
from tests.humans_fixture import HOST_HUMANS, write_host_humans


class HumansV1Tests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def cfg(self):
        return capture.humans(self.home)

    def test_operators_map_to_secretary_never_human(self):  # C1
        write_host_humans(self.home)
        cfg = self.cfg()
        self.assertEqual(capture.role_of({"sender": "david", "source": "codex-desktop:00000000"}, "in", cfg), "human")
        self.assertEqual(capture.role_of({"sender": "david", "source": "telegram:1000000001"}, "in", cfg), "human")
        for ev in ({"sender": "codex-desktop", "source": "codex-desktop:00000000"},
                   {"sender": "someone", "source": "operator-notice-1"},
                   {"sender": "david", "source": "codex-desktop-handoff"},
                   {"sender": "codex-desktop", "source": "desktop-secretary-x"}):
            self.assertEqual(capture.role_of(ev, "in", cfg), "secretary", ev)
        self.assertEqual(capture.role_of({"sender": "rowan", "source": "fieldpost"}, "in", cfg), "other")
        self.assertEqual(capture.role_of({"sender": "david", "source": "x"}, "out", cfg), "out")

    def test_limit_bypass_needs_auth_config_and_runtime_allow(self):
        write_host_humans(self.home)
        cfg = self.cfg()
        ev = {"sender": "david", "source": "telegram:1", "auth": {"channel": "telegram"}}
        self.assertTrue(capture.may_bypass_limit(ev, cfg, ["david"]))
        self.assertFalse(capture.may_bypass_limit(dict(ev, auth=None), cfg, ["david"]))
        self.assertFalse(capture.may_bypass_limit(ev, cfg, []))  # runtime limit_allow intersects
        self.assertFalse(capture.may_bypass_limit(dict(ev, source="slack:1"), cfg, ["david"]))
        op = {"sender": "codex-desktop", "source": "codex-desktop:1", "auth": {"channel": "d"}}
        self.assertFalse(capture.may_bypass_limit(op, cfg, ["codex-desktop"]))

    def test_missing_file_means_no_humans(self):
        cfg = self.cfg()
        self.assertIsNone(capture.default_human(self.home))
        self.assertEqual(capture.role_of({"sender": "david", "source": "telegram:1"}, "in", cfg), "other")

    def test_phase2_format_still_loads(self):
        (self.home / "humans.json").write_text(json.dumps(
            {"senders": ["alice"], "channel_prefixes": ["slack:"], "secretary_prefixes": ["assistant-"]}))
        cfg = self.cfg()
        self.assertEqual(cfg["senders"], ["alice"])
        self.assertIsNone(cfg["default_human"])

    def test_invalid_v1_rejected(self):
        bad = json.loads(json.dumps(HOST_HUMANS))
        bad["limit_bypass"]["require_auth"] = False
        (self.home / "humans.json").write_text(json.dumps(bad))
        with self.assertRaises(ValueError):
            self.cfg()
        bad = json.loads(json.dumps(HOST_HUMANS))
        bad["operators"]["senders"] = ["david"]  # overlap with human senders
        (self.home / "humans.json").write_text(json.dumps(bad))
        with self.assertRaises(ValueError):
            self.cfg()


if __name__ == "__main__":
    unittest.main()
