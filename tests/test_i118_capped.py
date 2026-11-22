"""#118: in query/thread mode, capped means matching items exist that the result leaves out, whether limit or
max_bytes cut it. Exactly limit matches is not capped. since= pages, plain Store lists and the dream's read chain
are unchanged."""

import json
from unittest import mock

from synthetics import routines
from synthetics.mcp import METHODS
from synthetics.store import Store
from tests import test_i17_export_verify as i17
from tests.test_f2_compact_search import Case as F2Case
from tests.test_i17_ledgerline import E1Case

BUDGET = 1 << 20  # never the cause of a cut


class Case(F2Case):
    def memories(self, n, text="fact", tag="p"):
        return [self.s.memory_put("topic", "%s %d" % (tag, i), text, source="t")["id"] for i in range(n)]

    def mem(self, *args, **kw):
        return self.s.memory_search(*args, **dict({"max_bytes": BUDGET}, **kw))


class Events(Case):
    def test_limit_cut_is_capped_through_mcp(self):
        ids = self.feed(5, 10, tag="needle")
        out = json.loads(self.call("event_search", {"query": "needle", "limit": 3}))
        self.assertEqual([e["id"] for e in out["events"]], ids[:1:-1])  # newest first; the extra match isn't returned
        self.assertIs(out["capped"], True)

    def test_exactly_limit_is_not_capped(self):
        ids = self.feed(3, 10, tag="needle")
        self.s.feed("someone", "other", external_id="newer")
        out = json.loads(self.call("event_search", {"query": "needle", "limit": 3}))
        self.assertEqual([e["id"] for e in out["events"]], ids[::-1])
        self.assertIs(out["capped"], False)

    def test_exactly_limit_with_older_non_matches_is_not_capped(self):
        self.s.feed("someone", "other", external_id="older")
        self.feed(3, 10, tag="needle")
        self.assertIs(self.s.event_search("needle", limit=3, max_bytes=BUDGET)["capped"], False)

    def test_one_past_the_limit_is_capped(self):
        self.feed(4, 10, tag="needle")
        self.feed(2, 10, tag="other")
        self.assertIs(self.s.event_search("needle", limit=3, max_bytes=BUDGET)["capped"], True)

    def test_thread_mode(self):
        for i in range(3):
            self.s.feed("p", "t %d" % i, thread="T", external_id="T%d" % i)
        self.s.feed("p", "elsewhere", thread="U", external_id="U")
        self.assertIs(self.s.event_search(thread="T", limit=2, max_bytes=BUDGET)["capped"], True)
        self.assertIs(self.s.event_search(thread="T", limit=3, max_bytes=BUDGET)["capped"], False)

    def test_body_chars_alone_is_covered(self):
        self.feed(3, 10, tag="needle")
        self.assertIs(self.s.event_search("needle", limit=2, body_chars=5)["capped"], True)
        self.assertIs(self.s.event_search("needle", limit=3, body_chars=5)["capped"], False)

    def test_byte_cut_within_the_limit_is_still_capped(self):
        self.feed(4, 3000, tag="needle")
        out = self.s.event_search("needle", limit=4, max_bytes=8000)
        self.assertLess(len(out["events"]), 4)
        self.assertIs(out["capped"], True)

    def test_the_extra_match_is_not_in_truncated_from(self):
        self.feed(1, 5000, tag="needle")  # older, long: one past the limit
        self.feed(1, 10, tag="needle new")
        out = json.loads(self.call("event_search", {"query": "needle", "limit": 1}))
        self.assertIs(out["capped"], True)
        self.assertIsNone(out["truncated_from"])

    def count_parsed(self, **kw):
        """How many stored events a search reads: each is parsed once from its row."""
        real, seen = json.loads, []
        with mock.patch("synthetics.store.json.loads", side_effect=lambda text, *a, **k: seen.append(1) or real(text, *a, **k)):
            self.s.event_search("needle", **kw)
        return len(seen)

    def test_one_past_lookup_stops_at_the_next_match(self):
        self.feed(60, 10, tag="needle")
        plain, compact = self.count_parsed(limit=5), self.count_parsed(limit=5, max_bytes=BUDGET)
        self.assertLess(plain, 20)
        self.assertLessEqual(compact - plain, 1)

    def test_a_byte_cut_page_reads_nothing_more(self):
        self.feed(60, 3000, tag="needle")
        self.assertEqual(self.count_parsed(limit=5, max_bytes=4000), self.count_parsed(limit=5))


class Memories(Case):
    def test_limit_cut_is_capped_through_mcp(self):
        ids = self.memories(5)
        out = json.loads(self.call("memory_search", {"query": "fact", "limit": 3}))
        self.assertEqual([m["id"] for m in out["memories"]], ids[:1:-1])
        self.assertIs(out["capped"], True)

    def test_the_reported_shape_default_limit_unfiltered(self):
        """#118: 60 memories, no query, the default limit (30)."""
        self.memories(60)
        out = json.loads(self.call("memory_search", {}))
        self.assertEqual(len(out["memories"]), 30)
        self.assertIs(out["capped"], True)

    def test_exactly_limit_is_not_capped(self):
        self.memories(3)
        for match in ("any", "all"):
            for query in ("fact", ""):
                out = self.mem(query, limit=3, match=match)
                self.assertEqual(len(out["memories"]), 3)
                self.assertIs(out["capped"], False, (match, query))

    def test_match_all_cut_by_limit(self):
        self.memories(4)
        self.assertIs(self.mem("fact", limit=3, match="all")["capped"], True)

    def test_match_any_counts_ranked_matches_the_fill_found(self):
        self.memories(1, "heron sighting", tag="a")
        self.memories(3, "heron", tag="b")
        self.assertIs(self.mem("heron sighting", limit=2)["capped"], True)
        out = self.mem("heron sighting", limit=4)
        self.assertEqual(len(out["memories"]), 4)
        self.assertIs(out["capped"], False)

    def test_match_any_exactly_limit_full_matches_counts_a_skipped_ranked_match(self):
        self.memories(1, "heron", tag="a")
        self.memories(2, "heron sighting", tag="b")
        self.assertIs(self.mem("heron sighting", limit=2)["capped"], True)
        self.assertIs(self.mem("heron sighting", limit=2, match="all")["capped"], False)  # ranked matches aren't asked for

    def test_match_any_exactly_limit_with_no_ranked_match_is_not_capped(self):
        self.memories(3, "kettle", tag="a")
        self.memories(2, "heron sighting", tag="b")
        self.assertIs(self.mem("heron sighting", limit=2)["capped"], False)

    def test_the_lookup_agrees_with_the_fill(self):
        """The existence lookup at exactly limit gives what the fill would: one larger limit returns another item."""
        self.memories(2, "heron sighting", tag="b")
        for text in ("heron", "a sighting", "the", "herons nest", "unrelated", "an it"):
            mid = self.memories(1, text, tag="x")[0]
            for query in ("heron sighting", "heron"):
                full = len(self.mem(query, limit=500, match="all")["memories"])
                more = len(self.mem(query, limit=full + 1)["memories"]) > full
                self.assertIs(self.mem(query, limit=full)["capped"], more, (text, query))
            self.s.memory_put("topic", "gone", "replaced", source="t", replaces=mid)

    def test_the_ranked_lookup_runs_only_at_exactly_limit(self):
        self.memories(2, "heron", tag="a")
        self.memories(3, "heron sighting", tag="b")
        real, calls = Store._ranked_match_exists, []
        with mock.patch.object(Store, "_ranked_match_exists", lambda *a: calls.append(1) or real(*a)):
            self.mem("heron sighting", limit=2)  # more full matches than limit: already known
            self.mem("heron sighting", limit=9)  # the fill ran
            self.mem("heron sighting", limit=3, match="all")
            self.s.memory_search("heron sighting", limit=3)  # plain list
            self.assertEqual(calls, [])
            self.assertIs(self.mem("heron sighting", limit=3)["capped"], True)
            self.assertEqual(calls, [1])

    def test_fill_is_not_scored_when_full_matches_reach_limit(self):
        self.memories(2, "heron", tag="a")
        self.memories(3, "heron sighting", tag="b")
        with mock.patch("synthetics.store.math.log", side_effect=AssertionError("scored")):
            self.assertIs(self.mem("heron sighting", limit=3)["capped"], True)
            self.assertIs(self.mem("heron sighting", limit=2)["capped"], True)

    def test_subject_and_superseded_bound_the_lookup(self):
        self.s.entity_put("other", "Other", "project")
        self.s.memory_put("other", "p", "heron", source="t")
        old = self.s.memory_put("topic", "old", "heron", source="t")["id"]
        self.s.memory_put("topic", "new", "kettle", source="t", replaces=old)
        self.memories(2, "heron sighting", tag="b")
        self.assertIs(self.mem("heron sighting", subject="topic", limit=2)["capped"], False)
        self.assertIs(self.mem("heron sighting", subject="topic", limit=2, include_superseded=True)["capped"], True)
        self.assertIs(self.mem("heron sighting", limit=2)["capped"], True)

    def test_byte_cut_is_still_capped(self):
        self.memories(5, "fact " + "v" * 1000)
        out = self.mem("fact", limit=5, max_bytes=3000)
        self.assertLess(len(out["memories"]), 5)
        self.assertIs(out["capped"], True)


class Unchanged(Case):
    def test_plain_calls_return_lists_at_the_limit(self):
        ids, events = self.memories(4), self.feed(4, 10, tag="needle")
        self.assertEqual([m["id"] for m in self.s.memory_search("fact", limit=3)], ids[:0:-1])
        self.assertEqual([e["id"] for e in self.s.event_search("needle", limit=3)], events[:0:-1])

    def test_since_pages_at_and_past_the_limit(self):
        self.memories(3)
        self.feed(3, 10, tag="needle")
        for search, query in ((self.s.memory_search, "fact"), (self.s.event_search, "needle")):
            for kw in ({}, {"max_bytes": BUDGET}):
                cut = search(query, since="2000-01-01T00:00:00Z", limit=2, **kw)
                whole = search(query, since="2000-01-01T00:00:00Z", limit=3, **kw)
                self.assertIs(cut["capped"], True)
                self.assertIsNotNone(cut["next_cursor"])
                self.assertIs(whole["capped"], False)
                self.assertIsNone(whole["next_cursor"])

    def test_descriptions_and_dream_guidance_say_it(self):
        for name in ("event_search", "memory_search"):
            self.assertIn("whichever of the two cut it", METHODS[name]["description"])
            self.assertNotIn("capped=true then means", METHODS[name]["description"])
        self.assertIn("ranked matches count too", METHODS["memory_search"]["description"])
        self.assertIn("A `capped` keyword lookup isn't part of them", routines.guidance("dream"))


class Receipts(E1Case):
    def test_query_mode_receipts_never_look_like_read_chain_pages(self):
        """ledgerline's read chain takes receipts whose since or cursor isn't null. Those calls always return a
        since= page; without both, a query-mode result has no since/next_cursor. Null and "" are refused."""
        self.store.entity_put("proj", "Proj", "project")
        for n in range(3):
            self.store.feed("priya", "needle %d" % n)
            self.store.memory_put("proj", "needle %d" % n, "v", source="test")
        for tool in ("event_search", "memory_search"):
            for arguments in ({}, {"query": "needle"}):
                self.respond(self.server, tool, dict(arguments, limit=2))
                receipt = self.receipts()[-1]
                self.assertTrue(receipt["success"], receipt)
                self.assertIs(receipt["result"]["capped"], True)
                self.assertNotIn("next_cursor", receipt["result"])
                self.assertIsNone(receipt["arguments"].get("since"))
            page = self.respond(self.server, tool, {"since": "1970-01-01T00:00:00Z", "limit": 2})
            cursor = json.loads(page["content"][0]["text"])["next_cursor"]
            for arguments in ({"cursor": cursor}, {"since": "1970-01-01T00:00:00Z"}):
                self.respond(self.server, tool, dict(arguments, limit=2))
                self.assertIn("since", self.receipts()[-1]["result"])
            for arguments in ({"since": ""}, {"cursor": ""}, {"since": None}, {"cursor": None}):
                self.respond(self.server, tool, arguments)
                self.assertFalse(self.receipts()[-1]["success"], arguments)


class Chain(i17.HomeCase):
    """A capped query-mode receipt anywhere among the dream's pages leaves its verdict as it was."""
    dream, both, page = i17.Verify.dream, i17.Verify.both, staticmethod(i17.Verify.page)

    def verdict(self, receipts):
        self.tearDown()
        self.setUp()  # a fresh home: the previous dream's checkpoint is part of the verdict
        return self.dream(receipts, through=i17.ts(50))[0]

    def test_capped_query_receipt_changes_no_verdict(self):
        ts, epoch = i17.ts, i17.EPOCH
        full = [self.page(epoch, ts(1), ts(5), True), self.page(None, ts(6), ts(9), False, cursor="c1")]
        gap = [self.page(epoch, ts(1), ts(5), True), self.page(ts(20), ts(20), ts(30), False)]
        facts = {"bytes": 90000, "sha256": "1" * 64, "ids": ["q"], "count": 1, "created_first": ts(5),
                 "created_last": ts(40), "capped": True}
        for events, expected in ((full, "consistent"), (gap, "overclaimed")):
            receipts = self.both(events, [self.page(epoch, ts(1), ts(8), False)])
            plain = self.verdict(receipts)
            self.assertEqual(plain["verdict"], expected, plain)
            for at in range(len(receipts) + 1):
                with_query = list(receipts)
                with_query[at:at] = [("event_search", {"query": "q", "limit": 1}, facts),
                                     ("memory_search", {"query": "q", "since": None, "cursor": None}, facts)]
                verdict = self.verdict(with_query)
                for key in ("verdict", "bound", "streams"):
                    self.assertEqual(verdict[key], plain[key], (expected, at))
