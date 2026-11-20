"""#100 P2 pure pieces: config v2 homes, header render (T1/T2/T15), migration (T7), reply resolution (T4/T11)."""
import tempfile
import unittest
from pathlib import Path

from synthetics import telegram_homes as th

CODEX = "employee-" + "a" * 32
QUILL = "employee-" + "b" * 32


def entries():
    return [
        dict(key="synthetics", home="/tmp/x-manager", employee_id=CODEX, colleague_id="david",
             label="🟦 Codex · Synthetics", button="Synthetics · Codex", thread="codex:1", primary=True),
        dict(key="noteling", home="/tmp/x-quill", employee_id=QUILL, colleague_id="david",
             label="🟨 Quill · Noteling", button="Noteling · Quill", thread=None,
             not_before="2026-11-20T18:00:00Z"),
    ]


class LoadHomesTest(unittest.TestCase):
    def setUp(self):
        self.base = Path(tempfile.mkdtemp())

    def test_valid(self):
        homes = th.load_homes(entries(), self.base)
        self.assertEqual([h["key"] for h in homes], ["synthetics", "noteling"])
        self.assertTrue(homes[0]["primary"])
        self.assertFalse(homes[1]["primary"])
        self.assertEqual(homes[1]["not_before"], "2026-11-20T18:00:00Z")

    def bad(self, mutate):
        data = entries()
        mutate(data)
        with self.assertRaises(th.HomesConfigError):
            th.load_homes(data, self.base)

    def test_refusals(self):
        self.bad(lambda d: d[1].update(primary=True))                      # two primaries
        self.bad(lambda d: d[0].update(primary=False))                     # none
        self.bad(lambda d: d[1].update(key="synthetics"))                  # duplicate key
        self.bad(lambda d: d[1].update(label=d[0]["label"]))               # duplicate label
        self.bad(lambda d: d[1].update(button=d[0]["button"]))             # duplicate button
        self.bad(lambda d: d[1].update(employee_id=CODEX))                 # duplicate employee
        self.bad(lambda d: d[1].update(employee_id="employee-abc"))        # not a full id
        self.bad(lambda d: d[1].update(label="two\nlines"))
        self.bad(lambda d: d[1].update(label="x" * 65))
        self.bad(lambda d: d[1].update(label=" padded"))
        self.bad(lambda d: d[1].update(key="Bad Key"))
        self.bad(lambda d: d[1].update(colleague_id="someone-else"))      # one paired human
        self.bad(lambda d: d[1].update(not_before="yesterday"))
        self.bad(lambda d: d[1].update(lable="typo"))                      # unknown field
        self.bad(lambda d: d.clear())


class RenderTest(unittest.TestCase):
    LABEL = "🟨 Quill · Noteling"

    def test_header_on_single_part(self):
        parts, meta = th.render_parts("hello", self.LABEL)
        self.assertEqual(parts, [self.LABEL + "\nhello"])
        self.assertEqual(meta, {"header": th.sha8(self.LABEL)})

    def test_multipart_header_every_part_footer_last(self):  # T2
        body = ("word " * 2000).strip()          # ~10000 chars
        parts, meta = th.render_parts(body, self.LABEL, footer="— Quill (synthetic)")
        self.assertGreater(len(parts), 2)
        for part in parts:
            self.assertTrue(part.startswith(self.LABEL + "\n"))
            self.assertLessEqual(len(part), th.MAX_MESSAGE_LENGTH)
        self.assertTrue(parts[-1].endswith("\n\n— Quill (synthetic)"))
        for part in parts[:-1]:
            self.assertNotIn("Quill (synthetic)", part)
        self.assertEqual(meta["footer"], th.sha8("— Quill (synthetic)"))
        # Nothing lost: the chunks rejoin to the body's words.
        words = " ".join(p[len(self.LABEL) + 1:].replace("\n\n— Quill (synthetic)", "") for p in parts).split()
        self.assertEqual(words, body.split())

    def test_dedupe_exact_first_line(self):  # T15
        parts, meta = th.render_parts(self.LABEL + "\nbody text", self.LABEL)
        self.assertEqual(parts, [self.LABEL + "\nbody text"])
        self.assertTrue(meta["header_deduped"])
        parts, meta = th.render_parts("  " + self.LABEL + "  \nbody", self.LABEL)
        self.assertEqual(parts, [self.LABEL + "\nbody"])
        # Not exact: kept.
        parts, meta = th.render_parts(self.LABEL + ": body", self.LABEL)
        self.assertEqual(parts, [self.LABEL + "\n" + self.LABEL + ": body"])
        self.assertNotIn("header_deduped", meta)

    def test_receipt(self):
        self.assertEqual(th.receipt(7, "1,2", {"header": "aaaaaaaa"}), "telegram:7:1,2;header:aaaaaaaa")
        self.assertEqual(th.receipt(7, "1", {"header": "aaaaaaaa", "footer": "bbbbbbbb"}),
                         "telegram:7:1;header:aaaaaaaa;footer:bbbbbbbb")


class MigrateTest(unittest.TestCase):  # T7
    def test_schema3_to_4_idempotent(self):
        primary = th.load_homes(entries(), Path("/"))[0]
        d = {"schema": 3, "question_map": {"41": "question-" + "1" * 32},
             "deliveries": {"message-1": {"status": "accepted", "telegram_message_ids": [41]},
                            "message-2": {"status": "uncertain", "telegram_message_ids": []}}}
        self.assertTrue(th.migrate_state(d, primary))
        self.assertEqual(d["schema"], 4)
        self.assertEqual(d["route_map"]["41"], dict(home="synthetics", employee_id=CODEX, message=None,
                                                    question="question-" + "1" * 32, kind="out", legacy=True))
        self.assertEqual({r["home"] for r in d["deliveries"].values()}, {"synthetics"})
        self.assertEqual(d["question_map"], {"41": "question-" + "1" * 32})   # kept for rollback
        snapshot = repr(d)
        self.assertFalse(th.migrate_state(d, primary))
        self.assertEqual(repr(d), snapshot)


class ResolveReplyTest(unittest.TestCase):  # T4 / T11 / F4
    def setUp(self):
        self.homes = th.load_homes(entries(), Path("/"))
        self.map = {
            "10": dict(home="synthetics", employee_id=CODEX, message="message-c", question=None, kind="out"),
            "11": dict(home="noteling", employee_id=QUILL, message="message-q", question="question-q", kind="out"),
            "12": dict(home="synthetics", employee_id=CODEX, message=None, question="question-old", kind="out",
                       legacy=True),
            "13": dict(home="synthetics", employee_id=CODEX, message=None, question=None, kind="out", legacy=True),
            "14": dict(home="gone", employee_id=QUILL, message="message-g", question=None, kind="out"),
            "15": dict(home="noteling", employee_id=CODEX, message="message-x", question=None, kind="out"),
            "16": dict(kind="notice"),
            "17": dict(home="noteling", employee_id=QUILL, event="event-in", kind="in"),
        }

    def test_rules(self):
        verdict, home, entry = th.resolve_reply(self.map, None, self.homes)
        self.assertEqual(verdict, "none")
        verdict, home, entry = th.resolve_reply(self.map, 10, self.homes)
        self.assertEqual((verdict, home["key"], entry["question"]), ("home", "synthetics", None))
        verdict, home, entry = th.resolve_reply(self.map, 11, self.homes)
        self.assertEqual((verdict, home["key"], entry["question"]), ("home", "noteling", "question-q"))
        verdict, home, entry = th.resolve_reply(self.map, 12, self.homes)      # old question: exact, primary
        self.assertEqual((verdict, home["key"], entry["question"]), ("home", "synthetics", "question-old"))
        self.assertEqual(th.resolve_reply(self.map, 13, self.homes)[0], "hold")   # F4 legacy ordinary
        self.assertEqual(th.resolve_reply(self.map, 14, self.homes)[0], "hold")   # home removed
        self.assertEqual(th.resolve_reply(self.map, 15, self.homes)[0], "hold")   # employee changed
        self.assertEqual(th.resolve_reply(self.map, 99, self.homes)[0], "hold")   # unmapped
        self.assertEqual(th.resolve_reply(self.map, 16, self.homes)[0], "none")   # Router notice
        verdict, home, entry = th.resolve_reply(self.map, 17, self.homes)        # his own earlier message
        self.assertEqual((verdict, home["key"]), ("home", "noteling"))

    def test_in_reply_to(self):
        self.assertIsNone(th.in_reply_to(None, None))
        self.assertEqual(th.in_reply_to(self.map["10"], 10), "message-c")
        self.assertEqual(th.in_reply_to(self.map["17"], 17), "event-in")
        self.assertEqual(th.in_reply_to(self.map["12"], 12), "tg:12")
        self.assertEqual(th.in_reply_to(None, 99), "tg:99")


if __name__ == "__main__":
    unittest.main()
