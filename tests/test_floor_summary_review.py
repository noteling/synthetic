"""Review regressions for the retained F4 draft; exercise actual MCP writes and receipts."""
from synthetics import flooraccept
from tests import test_mtg_f3_gate as meeting_fixture


class SummaryReview(meeting_fixture.Base):
    def verify_calls(self, calls):
        run, _ = meeting_fixture.G2Verify.run_with(self, calls, summary=False)
        return flooraccept.verify(self.home, run)

    def test_retry_of_one_memory_operation_is_one_summary(self):
        self.promise_meeting()
        tool, args = self.summary_call()
        args = dict(args, key="meeting-summary-retry")
        result = self.verify_calls([(tool, args), (tool, args)])
        self.assertEqual(result["meetings"][0]["summary"], "present", result)
        self.assertEqual(result["verdict"], "clean", result)

    def test_journal_filename_suffix_is_not_the_retained_transcript(self):
        self.promise_meeting()
        tool, args = self.summary_call()
        args["value"] = args["value"].replace("journal.jsonl", "journal.jsonl.backup")
        result = self.verify_calls([(tool, args)])
        self.assertEqual(result["meetings"][0]["summary"], "unanchored", result)

    def test_another_directory_with_same_name_suffix_is_not_the_journal(self):
        self.promise_meeting()
        tool, args = self.summary_call()
        args["value"] = args["value"].replace("meetings/", "old-meetings/")
        result = self.verify_calls([(tool, args)])
        self.assertEqual(result["meetings"][0]["summary"], "unanchored", result)
