import json
import tempfile
import unittest
from pathlib import Path

from synthetics.mcp import Server
from synthetics.store import Store, _json

PROMPT = Path(__file__).resolve().parent.parent / "synthetics" / "prompts" / "employee.md"


def legacy_search(store, query="", subject=None, limit=30, include_superseded=False):
    # Main's memory_search before M24, kept verbatim for the C3 regression check.
    words = query.casefold().split()
    out = []
    for memory in reversed(store._all("memories")):
        if subject is not None and memory["subject"] != subject:
            continue
        if not include_superseded and memory.get("superseded_by"):
            continue
        haystack = _json(memory).casefold()
        if all(word in haystack for word in words):
            out.append(memory)
    return out[:limit]


class MemoryRecallTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(Path(self.tmp.name))
        self.employee = self.store.initialize("Vesper", "Maintainer", "Maintain Synthetics.")
        self.store.entity_put("synthetics", "Synthetics", "project")
        self.old = [
            self.store.memory_put("synthetics", "relay location", "Fieldpost relay code lives in fieldpost/relay.py.",
                                  source="ls fieldpost"),
            self.store.memory_put("synthetics", "M08_assigned", "Three-failure stop notifies David by Telegram.",
                                  source="event-1"),
            self.store.memory_put("synthetics", "budget cap", "Every employee runs with a $100 session cap.",
                                  source="event-2"),
        ]
        for number in range(70):
            self.store.memory_put("synthetics", "filler %d" % number, "Milestone note %d checked; tests passed." % number,
                                  source="run-%d" % number)

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def test_all_word_results_lead_unchanged(self):
        for query in ("", "relay", "tests passed", "milestone note 1", "synthetics", "fieldpost relay"):
            legacy = legacy_search(self.store, query, limit=500)
            self.assertEqual(self.store.memory_search(query, limit=500, match="all"), legacy)
            self.assertEqual(self.store.memory_search(query, limit=500)[:len(legacy)], legacy)

    def test_any_word_fill_ranks_reworded_queries_and_obeys_limit(self):
        self.assertEqual(self.store.memory_search("where does the peer relay code live", match="all"), [])
        results = self.store.memory_search("where does the peer relay code live", limit=5)
        self.assertEqual(results[0]["id"], self.old[0]["id"])
        self.assertLessEqual(len(results), 5)
        self.assertEqual(len(self.store.memory_search("milestone checked", limit=3)), 3)

    def test_stopwords_and_short_tokens_never_match_alone(self):
        self.assertEqual(self.store.memory_search("the of is to by at a"), [])
        self.assertEqual(self.store.memory_search("where does it go when", limit=500), [])

    def test_exact_or_prefix_id_returns_that_memory_first(self):
        target = self.old[1]
        citing = self.store.memory_put("synthetics", "note", "See %s for details." % target["id"], source=target["id"])
        for query in (target["id"], target["id"][:15], target["id"].upper()):
            results = self.store.memory_search(query)
            self.assertEqual(results[0]["id"], target["id"], query)
            self.assertIn(citing["id"], [memory["id"] for memory in results])
        self.assertEqual(self.store.memory_search(target["id"][:14])[0]["id"], citing["id"])

    def test_invalid_match_is_rejected(self):
        with self.assertRaises(ValueError):
            self.store.memory_search("relay", match="some")

    def test_index_lists_exactly_cut_active_memories_newest_first(self):
        self_memory = self.store.memory_put(self.employee["id"], "name", "Vesper", source="bootstrap")
        replaced = self.store.memory_put("synthetics", "budget cap", "Every employee runs with a $120 cap.",
                                         source="event-3", replaces=self.old[2]["id"])
        context = self.store.context()
        shown = {memory["id"] for memory in context["memories"]}
        shown |= {m["id"] for m in self.store._all("memories") if m["subject"] == self.employee["id"]}  # in `self` (C1)
        index_ids = [entry["id"] for entry in context["memory_index"]]
        active = [memory for memory in reversed(self.store._all("memories")) if not memory.get("superseded_by")]
        self.assertEqual(index_ids, [memory["id"] for memory in active if memory["id"] not in shown])
        self.assertNotIn(self.old[2]["id"], index_ids)
        self.assertNotIn(self_memory["id"], index_ids)
        self.assertNotIn(replaced["id"], index_ids)
        self.assertIn(self.old[1]["id"], index_ids)
        self.assertFalse(context["memory_index_truncated"])
        entry = context["memory_index"][index_ids.index(self.old[1]["id"])]
        self.assertEqual(set(entry), {"id", "subject", "predicate", "kind", "date", "gist"})
        self.assertEqual(entry["gist"], "Three-failure stop notifies David by Telegram.")

    def test_index_gist_is_collapsed_and_cut(self):
        long = self.store.memory_put("synthetics", "long", "word\n\n  " * 60, source="x")
        for number in range(60):
            self.store.memory_put("synthetics", "newer %d" % number, "n", source="x")
        entry = next(e for e in self.store.context()["memory_index"] if e["id"] == long["id"])
        self.assertEqual(entry["gist"], ("word " * 16)[:80] + "…")

    def test_index_cap_and_truncated_flag(self):
        self.store.MEMORY_INDEX_LIMIT = 5
        context = self.store.context()
        self.assertEqual(len(context["memory_index"]), 5)
        self.assertTrue(context["memory_index_truncated"])
        self.store.MEMORY_INDEX_LIMIT = 300
        self.store.MEMORY_INDEX_BYTES = 600
        context = self.store.context()
        self.assertLessEqual(len(json.dumps(context["memory_index"], ensure_ascii=False).encode()), 600 + 10)
        self.assertTrue(context["memory_index_truncated"])

    def test_small_store_has_empty_index(self):
        with tempfile.TemporaryDirectory() as tmp, Store(Path(tmp)) as store:
            store.initialize("Rowan", "Maintainer", "Maintain.")
            store.memory_put(store.identity()["id"], "name", "Rowan", source="bootstrap")
            context = store.context()
            self.assertEqual(context["memory_index"], [])
            self.assertFalse(context["memory_index_truncated"])

    def test_mcp_accepts_match_argument(self):
        server = Server(self.store)
        tools = {tool["name"]: tool for tool in server.tools()} if hasattr(server, "tools") else None
        result = server.call("memory_search", {"query": "relay", "match": "all"})
        text = json.dumps(result)
        self.assertIn(self.old[0]["id"], text)
        with self.assertRaises(Exception):
            server.call("memory_search", {"query": "relay", "match": "fuzzy"})
        if tools:
            self.assertEqual(tools["memory_search"]["inputSchema"]["properties"]["match"]["enum"], ["any", "all"])

    def test_prompt_explains_index_and_fetch_by_id(self):
        prompt = PROMPT.read_text()
        self.assertIn("memory_index", prompt)
        self.assertIn("The gist is a hint; fetch the memory before relying on it", prompt)
        self.assertIn("with its full ID returns that memory first", prompt)


if __name__ == "__main__":
    unittest.main()
