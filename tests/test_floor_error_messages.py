"""Known operational diagnostics may cross the meeting channel; arbitrary error text may not."""
import json
import io
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from synthetics.floorsession import MeetingSession, provider_error_message


class SafeProviderMessages(unittest.TestCase):
    def test_usage_limit_with_provider_clock(self):
        text = "You've hit your weekly limit · resets 8am (America/New_York)"
        self.assertEqual(provider_error_message(text), "The employee's Claude executor has reached its usage limit. "
                         "Provider reset: 8am (America/New_York).")

    def test_limit_diagnostic_never_copies_other_result_text(self):
        value = provider_error_message("usage limit; PRIVATE-CREDENTIAL-DO-NOT-COPY; resets whenever")
        self.assertIn("usage limit", value)
        self.assertNotIn("PRIVATE", value)
        self.assertNotIn("whenever", value)

    def test_authentication_diagnostic_never_copies_credentials(self):
        value = provider_error_message("Invalid API key PRIVATE-CREDENTIAL-DO-NOT-COPY")
        self.assertIn("valid login", value)
        self.assertNotIn("PRIVATE", value)

    def test_unknown_and_non_text_errors_have_no_raw_content(self):
        for raw in ("PRIVATE-CREDENTIAL-DO-NOT-COPY", None, {"secret": "PRIVATE"}):
            self.assertEqual(provider_error_message(raw), "The employee's Claude executor rejected the request; "
                             "no answer was produced.")

    def test_invalid_reset_time_is_not_echoed(self):
        self.assertNotIn("Provider reset", provider_error_message("weekly limit resets 99am (Secret/Unknown)"))

    def test_unknown_timezone_cannot_echo_arbitrary_identifiers(self):
        value = provider_error_message("weekly limit resets 8am (Secret/Credential)")
        self.assertNotIn("Secret", value)
        self.assertNotIn("Provider reset", value)

    def test_cli_error_stops_the_queue_but_retains_reported_usage(self):
        with tempfile.TemporaryDirectory() as temporary:
            answers, errors = [], []
            session = MeetingSession(Path(temporary), "run-test", "m1", "brief",
                                     lambda *args: answers.append(args), on_error=errors.append)
            session.inflight = {"id": "first"}
            session.queue.append({"id": "second"})
            class Process:
                stdout = io.StringIO("\n".join([
                    json.dumps({"type": "result", "is_error": True, "result": "weekly limit", "total_cost_usd": 0.125}),
                    json.dumps({"type": "result", "result": "must not become another answer"})]))
            process = Process()
            with patch.object(session, "_next") as next_request:
                session._read(process, session.generation)
            next_request.assert_not_called()
            self.assertEqual(answers, [("first", None)])
            self.assertEqual(len(errors), 1)
            self.assertIn("usage limit", errors[0])
            self.assertEqual(list(session.queue), [{"id": "second"}])
            self.assertEqual(session.cost(), 0.125)
            self.assertTrue(process.stdout.closed, "the reader owns its pipe even on early error exit")
