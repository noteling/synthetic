"""M25 test 11: the three-failure stop alert is one outbox message to limit_notify; the core makes no network call.

Replaces the pre-M25 tests of the direct Telegram POST (cli._send_stop_notification, removed in M25 S6)."""
import io
import tempfile
import unittest
import urllib.request
from contextlib import redirect_stderr
from pathlib import Path
from unittest import mock

from synthetics import cli
from synthetics.engine import Engine, WorkerStopped
from synthetics.store import Store


class Boom:
    def run(self, context):
        raise RuntimeError("model unavailable")


def no_network(*args, **kwargs):
    raise AssertionError("the stop alert must not touch the network")


class StopAlertTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        with Store(self.home) as store:
            store.initialize("Rowan", "Maintainer")
            store.feed("codex-manager", "work", source="test")

    def outbox(self):
        with Store(self.home) as store:
            return [m for m in store.outbox_list() if m["to"] == "codex-manager" and "stopped" in m["body"]]

    def test_three_failures_queue_one_alert_and_no_network(self):
        engine = Engine(self.home, Boom(), limit_notify="codex-manager")
        with mock.patch.object(urllib.request, "urlopen", no_network):
            with self.assertRaises(WorkerStopped):
                engine.serve(poll_seconds=0.01, retry_seconds=0.01,
                             on_stop=lambda reason, failures: cli._queue_stop_alert(
                                 self.home, reason, failures, engine.limit_notify))
        sent = self.outbox()
        self.assertEqual(len(sent), 1)
        self.assertIn("Rowan stopped after 3 failed sessions", sent[0]["body"])
        self.assertIn("model unavailable", sent[0]["body"])

    def test_idempotent_per_last_run(self):
        first = cli._queue_stop_alert(self.home, "x", 3, "codex-manager")
        again = cli._queue_stop_alert(self.home, "x", 3, "codex-manager")
        self.assertEqual(first["id"], again["id"])
        self.assertEqual(len(self.outbox()), 1)

    def test_no_notify_logs_and_queues_nothing(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertIsNone(cli._queue_stop_alert(self.home, "x", 3, None))
        self.assertIn("no limit_notify", err.getvalue())
        self.assertEqual(self.outbox(), [])

    def test_outbox_failure_logs_to_stderr_without_fallback(self):  # C6
        err = io.StringIO()
        with mock.patch.object(Store, "message_send", side_effect=OSError("disk full")), \
                mock.patch.object(urllib.request, "urlopen", no_network), redirect_stderr(err):
            self.assertIsNone(cli._queue_stop_alert(self.home, "x", 3, "codex-manager"))
        self.assertIn("not queued", err.getvalue())

    def test_no_telegram_in_core_cli(self):
        self.assertFalse(hasattr(cli, "_send_stop_notification"))
        self.assertFalse(hasattr(cli, "_TELEGRAM_API_BASE"))


if __name__ == "__main__":
    unittest.main()
