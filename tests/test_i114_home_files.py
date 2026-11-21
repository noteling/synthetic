"""#114: Satchel extracts a real home: the Telegram state_file / lock_file that telegram.json configures (and the
state's .guard marker) are excluded; name-choice.json and handoff transcripts are archived masked and restored."""

import json
from pathlib import Path
import tarfile
import unittest

from tests.test_satchel_p1 import Fixture, satchel

BOT = "%d:%s" % (123456789, "Q" * 35)


class HomeFiles(Fixture):
    def telegram(self, **fields):
        config = json.loads((self.home / "telegram.json").read_text())
        config.update(fields)
        (self.home / "telegram.json").write_text(json.dumps(config))

    def recreate(self, archive):
        into = Path(self.temporary.name) / "new"
        satchel.recreate(archive, into, source_lost=True, attest="test: source lost", temp_base=self.tmpbase)
        return into

    def assert_refused(self, code):
        with self.assertRaises(satchel.SatchelError) as caught:
            self.extract()
        self.assertEqual(caught.exception.code, code)
        self.assert_nothing_written()
        return str(caught.exception)

    # -- Telegram files named by telegram.json ------------------------------------------------------------

    def test_configured_state_file_guard_and_lock_are_excluded(self):
        self.telegram(state_file="telegram.state.json", lock_file="tg.lock")
        (self.home / "telegram.state.json").write_text(json.dumps({"cursor": 7, "token": BOT}))
        (self.home / "telegram.state.json.guard").write_text("")
        (self.home / "tg.lock").write_text("")
        result = self.extract()
        for name in ("telegram.state.json", "telegram.state.json.guard", "tg.lock"):
            self.assertIn(name, result["excluded"])
        with tarfile.open(result["archive"]) as tar:
            names = tar.getnames()
            manifest = json.loads(tar.extractfile("manifest.json").read())
        self.assertFalse([n for n in names if "telegram" in n or "tg.lock" in n], names)
        self.assertIn("state_file", manifest["excluded"]["telegram.state.json"])
        self.assertIn("guard", manifest["excluded"]["telegram.state.json.guard"])

    def test_absolute_state_file_inside_home_is_excluded(self):
        self.telegram(state_file=str(self.home / "tg-state.json"))
        (self.home / "tg-state.json").write_text("{}")
        (self.home / "tg-state.json.guard").write_text("")
        result = self.extract()
        self.assertIn("tg-state.json", result["excluded"])
        self.assertIn("tg-state.json.guard", result["excluded"])

    def test_default_state_guard_is_excluded(self):
        (self.home / "telegram_state.json").write_text("{}")
        (self.home / "telegram_state.json.guard").write_text("")
        result = self.extract()
        self.assertIn("telegram_state.json.guard", result["excluded"])

    def test_state_file_outside_home_does_not_excuse_a_home_file(self):
        elsewhere = Path(self.temporary.name) / "elsewhere"
        elsewhere.mkdir()
        self.telegram(state_file=str(elsewhere / "stray.json"))
        (self.home / "stray.json").write_text("{}")
        self.assertIn("stray.json", self.assert_refused("unknown_home_file"))

    def test_unparsable_telegram_json_refuses(self):
        (self.home / "telegram.json").write_text("{not json")
        self.assert_refused("telegram_config")

    # -- name-choice.json ------------------------------------------------------------------------------------

    def test_name_choice_archived_masked_and_restored(self):
        choice = {"name": "Morgan", "reason": "steady", "reported_cost_usd": 0.003, "api_key": "abcd1234efgh"}
        (self.home / "name-choice.json").write_text(json.dumps(choice))
        result = self.extract()
        members = self.members(result["archive"])
        self.assertIn("config/name-choice.json", members)
        self.assertNotIn(b"abcd1234efgh", members["config/name-choice.json"])
        self.assertTrue(satchel.verify(result["archive"], temp_base=self.tmpbase)["ok"])
        restored = json.loads((self.recreate(result["archive"]) / "name-choice.json").read_text())
        self.assertEqual(restored["name"], "Morgan")
        self.assertEqual(restored["reason"], "steady")
        self.assertTrue(restored["api_key"].startswith("«masked:assign:"))

    # -- transcripts/ ----------------------------------------------------------------------------------------

    def test_transcripts_archived_masked_and_restored(self):
        (self.home / "transcripts").mkdir()
        snapshot = {"thread": "codex:abc", "available_history_exhausted": True,
                    "messages": [{"sender": "david", "body": "the bot token is %s, keep it safe" % BOT},
                                 {"sender": "codex-manager", "body": "noted"}]}
        (self.home / "transcripts" / "codex-abc.json").write_text(json.dumps(snapshot))
        result = self.extract()
        with open(result["archive"], "rb") as handle:
            self.assertNotIn(BOT.encode(), handle.read())
        members = self.members(result["archive"])
        self.assertIn("transcripts/codex-abc.json", members)
        report = satchel.verify(result["archive"], temp_base=self.tmpbase)
        self.assertTrue(report["ok"], report.get("problems"))
        restored = json.loads((self.recreate(result["archive"]) / "transcripts" / "codex-abc.json").read_text())
        self.assertEqual(restored["thread"], "codex:abc")
        self.assertEqual(restored["messages"][1]["body"], "noted")
        self.assertIn("«masked:telegram:", restored["messages"][0]["body"])

    def test_transcripts_with_other_entries_refuse(self):
        (self.home / "transcripts").mkdir()
        (self.home / "transcripts" / "ok.json").write_text("{}")
        (self.home / "transcripts" / "notes.txt").write_text("x")
        self.assertIn("transcripts/notes.txt", self.assert_refused("unknown_home_file"))

    def test_transcript_that_is_not_json_refuses(self):
        (self.home / "transcripts").mkdir()
        (self.home / "transcripts" / "bad.json").write_text("{nope")
        self.assert_refused("unreadable_config")


if __name__ == "__main__":
    unittest.main()
