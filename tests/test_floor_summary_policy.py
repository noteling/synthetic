"""Summary matching policy, independent of the employee store and meeting transport."""
import unittest

from synthetics.flooraccept import _cites, _summary_state


class SummaryPolicy(unittest.TestCase):
    def state(self, values):
        wrap = "event-" + "a" * 32
        head = {"head_sha256": "b" * 64}
        records = []
        for number, (path, memory_id) in enumerate(values, 1):
            record = {"tool": "memory_put", "arguments": {
                "predicate": "meeting summary", "source": wrap,
                "value": "m1: %s lines 1-8, head %s" % (path, head["head_sha256"])}}
            if memory_id:
                record["result"] = {"id": memory_id}
            records.append((number, record))
        return _summary_state("m1", wrap, head, _cites("m1", wrap), records)

    def test_absolute_and_relative_journal_links(self):
        for path in ("meetings/m1/journal.jsonl", "/tmp/home/meetings/m1/journal.jsonl",
                     "`meetings/m1/journal.jsonl`", "meetings/m1/journal.jsonl#L1"):
            self.assertEqual(self.state([(path, "memory-1")]), ("present", [1]), path)

    def test_incorrect_path_components(self):
        for path in ("old-meetings/m1/journal.jsonl", "meetings/m1/journal.jsonl.bak",
                     "meetings/m1/journal.jsonl/child", "meetings/m1/journal.jsonl-other"):
            self.assertEqual(self.state([(path, "memory-1")]), ("unanchored", [1]), path)

    def test_same_memory_id_retries_keep_all_receipt_lines(self):
        self.assertEqual(self.state([("meetings/m1/journal.jsonl", "memory-1")] * 2), ("present", [1, 2]))

    def test_separate_memories_and_receipts_without_ids_count_separately(self):
        for ids in (("memory-1", "memory-2"), (None, None)):
            self.assertEqual(self.state([("meetings/m1/journal.jsonl", i) for i in ids]), ("duplicate", [1, 2]))
