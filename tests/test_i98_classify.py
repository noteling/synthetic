"""#98 D1/D2: provider quota/outage classification and visible cause (fail-first on ecb11b3)."""
import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from synthetics import brain, ledgerline

# The observed 11-19 23:32Z result line (runs/run-2a66f4ff*.claude.json), identifying fields dropped.
OBSERVED = {
    "type": "result", "subtype": "success", "is_error": True, "num_turns": 1, "duration_ms": 181,
    "total_cost_usd": 0, "terminal_reason": "api_error", "api_error_status": 429,
    "api_error_type": "rate_limit_error", "api_error_origin": "first_party", "api_error": "usage_limit_reached",
    "result": "You've hit your weekly limit · resets 8am (America/New_York)",
}
# SYNTHETIC (Codex Q1): no outage sample was observed on this host.
OUTAGE_SYNTHETIC = {"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error",
                    "api_error_status": 529, "api_error_type": "overloaded_error",
                    "result": "Overloaded · resets 8am (America/New_York)"}
NOW = datetime(2026, 11, 19, 23, 32, tzinfo=timezone.utc)


class Proc:
    def __init__(self, returncode):
        self.returncode = returncode


class ResultPath(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        self.out = self.dir / "run-x.claude.json"
        self.err = self.dir / "run-x.stderr.txt"
        self.err.write_text("", encoding="utf-8")
        self.brain = brain.ClaudeBrain.__new__(brain.ClaudeBrain)
        self.brain.budget = 100.0

    def tearDown(self):
        self.tmp.cleanup()

    def result(self, raw, returncode=1):
        if raw is not None:
            self.out.write_text(raw if isinstance(raw, str) else json.dumps(raw), encoding="utf-8")
        return self.brain._result(Proc(returncode), self.out, self.err)

    def test_t1_observed_quota_raises_provider_unavailable(self):
        with self.assertRaises(brain.ProviderUnavailable) as caught:
            self.result(OBSERVED)
        error = caught.exception
        self.assertIsInstance(error, brain.BrainError)
        self.assertEqual(error.kind, "quota")
        self.assertEqual((error.status, error.api_error), (429, "usage_limit_reached"))
        self.assertIn("You've hit your weekly limit", str(error))
        self.assertTrue(str(error).startswith("provider_unavailable quota (usage_limit_reached/429): "))
        self.assertEqual(error.cost, 0.0)
        self.assertEqual(set(error.provider()), {"kind", "status", "api_error", "reset_at"})

    def test_t1_exit_zero_quota_also_classified(self):
        with self.assertRaises(brain.ProviderUnavailable):
            self.result(OBSERVED, returncode=0)

    def test_t1b_outage_synthetic_backoff_only(self):
        with self.assertRaises(brain.ProviderUnavailable) as caught:
            self.result(OUTAGE_SYNTHETIC)
        self.assertEqual(caught.exception.kind, "outage")
        self.assertIsNone(caught.exception.reset_at)  # outage never parses a reset

    def test_t1_text_bounded(self):
        raw = dict(OBSERVED, result="x" * 5000)
        with self.assertRaises(brain.ProviderUnavailable) as caught:
            self.result(raw)
        self.assertEqual(len(caught.exception.text), brain.ProviderUnavailable.TEXT_LIMIT)

    def test_t6_c1_absent_json_message_unchanged(self):
        with self.assertRaises(brain.BrainError) as caught:
            self.result(None)
        self.assertNotIsInstance(caught.exception, brain.ProviderUnavailable)
        self.assertEqual(str(caught.exception), "Claude CLI exited with status 1. See %s." % self.err)

    def test_t6_c1_invalid_json_message_unchanged(self):
        with self.assertRaises(brain.BrainError) as caught:
            self.result("{not json")
        self.assertNotIsInstance(caught.exception, brain.ProviderUnavailable)
        self.assertEqual(str(caught.exception), "Claude CLI exited with status 1. See %s." % self.err)

    def test_t6_unknown_api_errors_take_todays_path(self):
        cases = [
            dict(OBSERVED, api_error_status=400, api_error_type="invalid_request_error", api_error="bad_model"),
            {k: v for k, v in OBSERVED.items() if k not in ("api_error_status", "api_error_type", "api_error")},
            dict(OBSERVED, terminal_reason="completed"),
            {k: v for k, v in OBSERVED.items() if k != "terminal_reason"},
            dict(OBSERVED, api_error_status=True, api_error_type=None, api_error=None),
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                with self.assertRaises(brain.BrainError) as caught:
                    self.result(raw)
                self.assertNotIsInstance(caught.exception, brain.ProviderUnavailable)
                self.assertEqual(str(caught.exception), "Claude CLI exited with status 1. See %s." % self.err)

    def test_t6_budget_cap_unchanged(self):
        raw = {"type": "result", "subtype": "error_max_budget_usd", "is_error": False, "total_cost_usd": 100.0}
        with self.assertRaises(brain.BrainError) as caught:
            self.result(raw, returncode=0)
        self.assertNotIsInstance(caught.exception, brain.ProviderUnavailable)
        self.assertIn("budget cap", str(caught.exception))


class ResetHint(unittest.TestCase):
    def test_next_occurrence(self):
        self.assertEqual(brain.parse_reset_hint(OBSERVED["result"], NOW), "2026-11-20T13:00:00Z")
        self.assertEqual(brain.parse_reset_hint("resets 8:30pm (Europe/London)", NOW), "2026-11-20T20:30:00Z")

    def test_t5b_unparsed(self):
        for text in ("resets soon", "resets 8am (Mars/Base)", "resets 13pm (UTC)", "resets 8am", None, ""):
            with self.subTest(text=text):
                self.assertIsNone(brain.parse_reset_hint(text, NOW))

    def test_t5b_bound_more_than_8_days(self):
        old = brain.RESET_MAX_AHEAD_SECONDS
        try:
            brain.RESET_MAX_AHEAD_SECONDS = 3600  # 13 h ahead is now "too far"
            self.assertIsNone(brain.parse_reset_hint(OBSERVED["result"], NOW))
        finally:
            brain.RESET_MAX_AHEAD_SECONDS = old


class ReducerFacts(unittest.TestCase):
    def facts(self, data):
        reducer = ledgerline.StreamReducer.__new__(ledgerline.StreamReducer)
        return list(reducer.facts(data))

    def test_t7_result_fact_keeps_api_fields_only(self):
        (fact,) = self.facts(dict(OBSERVED, session_id="s", uuid="u", credential_expires_at="c"))
        self.assertEqual(fact["terminal_reason"], "api_error")
        self.assertEqual(fact["api_error"], "usage_limit_reached")
        self.assertEqual(fact["api_error_status"], 429)
        self.assertEqual(fact["api_error_type"], "rate_limit_error")
        self.assertEqual(fact["api_error_text"], OBSERVED["result"])
        self.assertTrue(set(fact) <= ledgerline.STREAM_ALLOWED["result"] | {"kind"})
        for key in ledgerline.STREAM_FORBIDDEN:
            self.assertNotIn(key, fact)

    def test_t7_text_bounded_and_only_for_api_error(self):
        (fact,) = self.facts(dict(OBSERVED, result="y" * 1000))
        self.assertEqual(len(fact["api_error_text"]), 300)
        (fact,) = self.facts({"type": "result", "subtype": "success", "terminal_reason": "completed",
                              "result": "the model's summary"})
        self.assertNotIn("api_error_text", fact)


if __name__ == "__main__":
    unittest.main()
