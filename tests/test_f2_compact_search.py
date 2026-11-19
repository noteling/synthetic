"""F2: search results fit tool output (docs/routines.md "The dream", H5 page rule).

event_search cuts bodies to body_chars and marks body_len/body_truncated; every search result (since= pages and
query/thread mode, events and memories) stops before max_bytes with at least one item; a single item too big
alone is cut and still counts as returned; truncated_from bounds coverage; event_get reads a body in ID-keyed
chunks. The store's defaults are unchanged (C4): only mcp.py passes the compact defaults."""
import hashlib, json, tempfile, unittest
from pathlib import Path
from unittest import mock

from synthetics import mcp
from synthetics import store as store_module
from synthetics.mcp import METHODS, Server
from synthetics.store import Store


def size(value):
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = Path(self.tmp.name) / "a"
        self.s = Store(self.home)
        self.me = self.s.initialize("A", "developer")["id"]
        self.s.entity_put("topic", "Topic", "project")
        self.server = Server(self.s)

    def tearDown(self):
        self.s.close()
        self.tmp.cleanup()

    def call(self, name, arguments):
        response = self.server.respond({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                        "params": {"name": name, "arguments": arguments}})["result"]
        self.assertFalse(response.get("isError", False), response)
        return response["content"][0]["text"]

    def feed(self, n, chars, tag="ev"):
        return [self.s.feed("someone", ("%s %d " % (tag, i)) + "x" * chars, external_id="%s-%d" % (tag, i))["id"]
                for i in range(n)]

    def walk(self, search, name, **kw):
        seen, pages, page = [], [], search(since="2000-01-01T00:00:00Z", **kw)
        while True:
            pages.append(page)
            self.assertLess(len(pages), 200, "paging did not terminate")
            seen += [item["id"] for item in page[name]]
            if not page["capped"]:
                self.assertIsNone(page["next_cursor"])
                return seen, pages
            page = search(cursor=page["next_cursor"], **kw)


class QueryPreview(Case):
    def test_mcp_query_cuts_a_large_body_and_says_so(self):
        [eid] = self.feed(1, 50000, tag="green")
        out = json.loads(self.call("event_search", {"query": "green"}))
        [event] = out["events"]
        self.assertEqual(event["id"], eid)
        self.assertEqual(len(event["body"]), mcp.SEARCH_BODY_CHARS)
        self.assertEqual(event["body_len"], len("green 0 ") + 50000)
        self.assertTrue(event["body_truncated"])
        self.assertEqual(out["truncated_from"], event["created_at"])
        self.assertFalse(out["capped"])
        # The store default is unchanged: the full body, a plain list, no F2 keys.
        [full] = self.s.event_search("green")
        self.assertEqual(len(full["body"]), event["body_len"])
        self.assertNotIn("body_len", full)

    def test_short_bodies_are_whole_and_not_truncated(self):
        self.feed(2, 10, tag="tiny")
        out = json.loads(self.call("event_search", {"query": "tiny"}))
        self.assertEqual(len(out["events"]), 2)
        self.assertTrue(all(e["body_truncated"] is False and e["body_len"] == len(e["body"]) for e in out["events"]))
        self.assertIsNone(out["truncated_from"])

    def test_mcp_since_pages_keep_full_bodies(self):
        self.feed(1, 5000)
        out = json.loads(self.call("event_search", {"since": "2000-01-01T00:00:00Z"}))
        [event] = out["events"]
        self.assertFalse(event["body_truncated"])
        self.assertEqual(len(event["body"]), event["body_len"])
        self.assertIsNone(out["truncated_from"])


class Budget(Case):
    def test_since_page_stops_before_the_budget_and_resumes(self):
        ids = self.feed(10, 10000)
        page = self.s.event_search(since="2000-01-01T00:00:00Z", max_bytes=32000)
        self.assertTrue(page["capped"])
        self.assertLessEqual(size(page), 32000)
        n = len(page["events"])
        self.assertTrue(1 <= n < 10)
        self.assertEqual([e["id"] for e in page["events"]], ids[:n])
        self.assertIsNone(page["truncated_from"])  # C3: nothing was cut
        self.assertEqual(page["unread_from"], self.s.event_get(ids[n])["created_at"])
        seen, pages = self.walk(self.s.event_search, "events", max_bytes=32000)
        self.assertEqual(seen, ids)
        self.assertTrue(all(size(p) <= 32000 for p in pages))
        self.assertTrue(all(not e["body_truncated"] for p in pages for e in p["events"]))

    def test_a_first_item_too_big_alone_is_cut_and_the_cursor_moves_past_it(self):
        [big] = self.feed(1, 90000, tag="big")
        [small] = self.feed(1, 10, tag="small")
        page = self.s.event_search(since="2000-01-01T00:00:00Z", max_bytes=32000)
        [event] = page["events"]
        self.assertEqual(event["id"], big)
        self.assertTrue(event["body_truncated"])
        self.assertEqual(event["body_len"], len("big 0 ") + 90000)
        self.assertLessEqual(size(page), 32000)
        self.assertEqual(page["truncated_from"], event["created_at"])
        self.assertTrue(page["capped"])
        # C3: the next page does not repeat the cut item.
        nxt = self.s.event_search(cursor=page["next_cursor"], max_bytes=32000)
        self.assertEqual([e["id"] for e in nxt["events"]], [small])
        self.assertIsNone(nxt["truncated_from"])
        self.assertFalse(nxt["capped"])

    def test_query_mode_is_budgeted_too(self):
        """C1: query/thread mode also stops before max_bytes, returns at least one item and sets capped."""
        self.feed(20, 3000, tag="match")
        out = self.s.event_search("match", max_bytes=8000)
        self.assertTrue(out["capped"])
        self.assertTrue(1 <= len(out["events"]) < 20)
        self.assertLessEqual(size(out), 8000)
        out = json.loads(self.call("event_search", {"query": "match", "limit": 200}))
        self.assertFalse(out["capped"])  # 20 previews of 500 characters fit 32 000
        self.assertLessEqual(size(out), mcp.SEARCH_MAX_BYTES)
        thread = [self.s.feed("p", "t %d " % i + "y" * 4000, thread="T", external_id="T%d" % i)["id"] for i in range(9)]
        out = self.s.event_search(thread="T", max_bytes=8000)
        self.assertTrue(out["capped"])
        self.assertEqual(out["events"][0]["id"], thread[-1])  # newest first, as before

    def test_memory_query_and_since_are_budgeted_and_never_cut(self):
        ids = [self.s.memory_put("topic", "fact %d" % i, "v" * 1000, source="t")["id"] for i in range(100)]
        out = self.s.memory_search("fact", limit=200, max_bytes=8000)
        self.assertTrue(out["capped"])
        self.assertLessEqual(size(out), 8000)
        self.assertTrue(all(len(m["value"]) == 1000 for m in out["memories"]))
        seen, pages = self.walk(self.s.memory_search, "memories", limit=200, max_bytes=8000)
        self.assertEqual(sorted(seen), sorted(ids))
        self.assertEqual(len(seen), len(set(seen)))
        self.assertTrue(all(size(p) <= 8000 for p in pages))
        self.assertTrue(all(p["truncated_from"] is None for p in pages))
        out = json.loads(self.call("memory_search", {"query": "fact", "limit": 200}))
        self.assertTrue(out["capped"])
        self.assertLessEqual(size(out), mcp.SEARCH_MAX_BYTES)

    def test_one_memory_larger_than_the_budget_is_still_returned_whole(self):
        mid = self.s.memory_put("topic", "huge", "z" * 3000, source="t")["id"]
        out = self.s.memory_search("huge", max_bytes=1024)
        self.assertEqual([m["id"] for m in out["memories"]], [mid])
        self.assertEqual(len(out["memories"][0]["value"]), 3000)

    def test_tied_created_at_pages_under_a_budget_cover_each_once(self):
        fixed = "2026-11-19T11:30:00.000000Z"
        real = store_module.timestamp
        with mock.patch.object(store_module, "timestamp", lambda value=None: fixed if value is None else real(value)):
            ids = self.feed(6, 3000, tag="tied")
        seen, pages = self.walk(self.s.event_search, "events", max_bytes=8000)
        self.assertEqual(seen, ids)
        self.assertGreater(len(pages), 1)

    def test_bad_options_are_refused(self):
        for kw in ({"max_bytes": 100}, {"max_bytes": True}, {"body_chars": -1}, {"body_chars": "5"}):
            with self.assertRaises(ValueError):
                self.s.event_search(**kw)
        with self.assertRaises(ValueError):
            self.s.memory_search(max_bytes=10)
        [eid] = self.feed(1, 10)
        with self.assertRaises(ValueError):
            self.s.event_get(eid, body_offset=-1)


class Chunks(Case):
    def test_event_get_chunks_reassemble_the_exact_body(self):
        body = "".join(chr(0x41 + i % 26) + ("é" if i % 7 == 0 else "") for i in range(88000))
        eid = self.s.feed("someone", body, external_id="doc")["id"]
        parts, offset = [], 0
        while offset is not None:
            chunk = json.loads(self.call("event_get", {"id": eid, "body_offset": offset}))
            self.assertEqual(chunk["body_len"], len(body))
            self.assertEqual(chunk["body_offset"], offset)
            self.assertLessEqual(len(chunk["body"]), mcp.EVENT_CHUNK_CHARS)
            parts.append(chunk["body"])
            offset = chunk["body_next"]
        self.assertEqual(hashlib.sha256("".join(parts).encode()).hexdigest(), hashlib.sha256(body.encode()).hexdigest())
        past = self.s.event_get(eid, body_offset=len(body) + 10, body_chars=5)
        self.assertEqual(past["body"], "")
        self.assertIsNone(past["body_next"])

    def test_plain_event_get_is_unchanged(self):
        """C2: without a slice, event_get (store and MCP) returns the stored event as before."""
        eid = self.s.feed("someone", "q" * 40000, external_id="plain")["id"]
        stored = self.s.event_get(eid)
        self.assertNotIn("body_len", stored)
        self.assertEqual(self.call("event_get", {"id": eid}), json.dumps(stored, ensure_ascii=False))
        self.assertIn("body_offset", METHODS["event_get"]["description"])


class StoreDefaultsUnchanged(Case):
    """C4: no since, no body_chars, no max_bytes: the store returns what it returned before F2."""

    def test_no_options_give_plain_lists_and_full_records(self):
        ids = self.feed(3, 20000)
        self.s.memory_put("topic", "plain", "value", source="t")
        events = self.s.event_search()
        self.assertIsInstance(events, list)
        self.assertEqual([e["id"] for e in events], list(reversed(ids)))
        self.assertTrue(all("body_len" not in e and "body_truncated" not in e for e in events))
        self.assertTrue(all(len(e["body"]) > 20000 for e in events))
        self.assertIsInstance(self.s.memory_search("plain"), list)
        for page in (self.s.event_search(since="2000-01-01T00:00:00Z"),
                     self.s.memory_search(since="2000-01-01T00:00:00Z")):
            self.assertNotIn("truncated_from", page)
            self.assertEqual(set(page), {"events", "memories"} & set(page) | {"since", "capped", "unread_from",
                                                                               "next_cursor"})


if __name__ == "__main__":
    unittest.main()
