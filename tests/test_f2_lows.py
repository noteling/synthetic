"""F2 lows (PR #60 review L1, L2).

L1: a first item too big alone keeps the longest body prefix that fits the byte budget, also when its text is
accented, newline-heavy or outside the BMP (the old cut subtracted bytes from a character count and kept 0 or
a few characters). L2: event_get refuses body_chars=0, which returned body_next == body_offset and so a chunk
loop never ended."""
import json, tempfile, unittest
from pathlib import Path

from synthetics.mcp import METHODS, Server
from synthetics.store import Store

SINCE = "2000-01-01T00:00:00Z"


def size(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.s = Store(Path(self.tmp.name) / "a")
        self.s.initialize("A", "developer")
        self.server = Server(self.s)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def call(self, name, arguments):
        return self.server.respond({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                    "params": {"name": name, "arguments": arguments}})["result"]


class CutKeepsTheLongestPrefix(Case):
    TEXTS = {"ascii": "x", "accented": "é", "newlines": "\nz", "quotes": '"', "astral": "\U0001F600", "mixed": "aé\n"}

    def test_cut_first_item_is_the_longest_prefix_that_fits(self):
        for max_bytes in (1024, 4000, 32000):
            for label, unit in self.TEXTS.items():
                with self.subTest(text=label, max_bytes=max_bytes):
                    with tempfile.TemporaryDirectory() as tmp:
                        s = Store(Path(tmp) / "h")
                        s.initialize("A", "developer")
                        try:
                            eid = s.feed("someone", unit * (3 * max_bytes), external_id="big")["id"]
                            body = s.event_get(eid)["body"]  # as stored (feed may strip whitespace)
                            page = s.event_search(since=SINCE, max_bytes=max_bytes)
                            [event] = page["events"]
                            self.assertTrue(event["body_truncated"])
                            self.assertEqual(event["body_len"], len(body))
                            self.assertLessEqual(size(page), max_bytes)
                            kept = len(event["body"])
                            self.assertTrue(body.startswith(event["body"]))
                            # Room the page allows for one item, as _fit computes it.
                            # The page sizes items with their _seq tie-break attached (a few bytes over,
                            # never under), so the kept item fills the item room to within that slack.
                            room = max_bytes - Store.PAGE_ENVELOPE
                            self.assertLessEqual(size(event) + 2, room)
                            longer = dict(event, body=body[:kept + 1])
                            self.assertGreater(size(longer) + 2 + 32, room, "the preview is far shorter than fits")
                            self.assertGreater(kept, 0)
                        finally:
                            s.close()

    def test_cut_to_fit_returns_the_longest_fitting_prefix(self):
        for max_bytes in (300, 1024, 4000, 32000):
            for label, unit in self.TEXTS.items():
                with self.subTest(text=label, max_bytes=max_bytes):
                    body = unit * (3 * max_bytes)
                    event = {"id": "event-1", "created_at": "2026-11-19T15:00:00Z", "body": body,
                             "body_len": len(body), "body_truncated": False}
                    cut = Store._cut_to_fit(event, max_bytes)
                    kept = len(cut["body"])
                    self.assertEqual(cut["body"], body[:kept])
                    self.assertTrue(cut["body_truncated"])
                    self.assertEqual(cut["body_len"], len(body))
                    self.assertLessEqual(size(cut) + 2, max_bytes)
                    self.assertGreater(size(dict(cut, body=body[:kept + 1])) + 2, max_bytes)
                    self.assertGreater(kept, 0)

    def test_cut_to_fit_empty_body_when_nothing_fits(self):
        event = {"id": "event-1", "sender": "s" * 500, "body": "abc", "body_truncated": False}
        cut = Store._cut_to_fit(event, 100)
        self.assertEqual(cut["body"], "")
        self.assertTrue(cut["body_truncated"])
        self.assertEqual(Store._cut_to_fit({"id": "e", "body": None}, 100), {"id": "e", "body": None})

    def test_accented_preview_is_useful_at_4000_bytes(self):
        """The review's case: at max_bytes=4000 the old cut kept 0 characters of accented text."""
        self.s.feed("someone", "é" * 50000, external_id="acc")
        [event] = self.s.event_search(since=SINCE, max_bytes=4000)["events"]
        self.assertGreater(len(event["body"]), 1000)

    def test_an_item_that_fits_is_not_cut(self):
        eid = self.s.feed("someone", "é\nb" * 100, external_id="small")["id"]
        [event] = self.s.event_search(since=SINCE, max_bytes=4000)["events"]
        self.assertFalse(event["body_truncated"])
        self.assertEqual(event["body"], self.s.event_get(eid)["body"])


class ChunkSizeAtLeastOne(Case):
    def test_store_refuses_body_chars_zero(self):
        eid = self.s.feed("someone", "abc", external_id="e")["id"]
        for bad in (0, -1, True, "3"):
            with self.subTest(body_chars=bad), self.assertRaises(ValueError):
                self.s.event_get(eid, body_offset=0, body_chars=bad)
        one = self.s.event_get(eid, body_offset=0, body_chars=1)
        self.assertEqual((one["body"], one["body_next"]), ("a", 1))

    def test_mcp_refuses_body_chars_zero(self):
        eid = self.s.feed("someone", "abc", external_id="e")["id"]
        self.assertEqual(METHODS["event_get"]["inputSchema"]["properties"]["body_chars"]["minimum"], 1)
        result = self.call("event_get", {"id": eid, "body_offset": 0, "body_chars": 0})
        self.assertTrue(result.get("isError"), result)

    def test_every_accepted_chunk_size_advances(self):
        eid = self.s.feed("someone", "aé\nb" * 7, external_id="e")["id"]
        body = self.s.event_get(eid)["body"]
        for chars in (1, 2, 5, len(body), len(body) + 3):
            with self.subTest(body_chars=chars):
                parts, offset, steps = [], 0, 0
                while offset is not None:
                    chunk = self.s.event_get(eid, body_offset=offset, body_chars=chars)
                    self.assertTrue(chunk["body_next"] is None or chunk["body_next"] > offset)
                    parts.append(chunk["body"])
                    offset = chunk["body_next"]
                    steps += 1
                    self.assertLess(steps, 100)
                self.assertEqual("".join(parts), body)


if __name__ == "__main__":
    unittest.main()
