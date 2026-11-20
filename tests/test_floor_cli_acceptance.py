"""Actual CLI and stdio MCP processes, scratch homes, and no model calls."""
import json
import os
from pathlib import Path
import subprocess
import sys

from synthetics import flooraccept
from synthetics.store import SESSION_ENV, Store
from tests import test_mtg_f3_gate as meeting_fixture


class FloorProcessAcceptance(meeting_fixture.Base):
    def command(self, *args, text=""):
        env = dict(os.environ)
        env.pop(SESSION_ENV, None)
        return subprocess.run([sys.executable, "-m", "synthetics", "--home", str(self.home), *args],
                              input=text, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              env=env, timeout=20)

    def test_disabled_attach_cannot_yield_a_running_session(self):
        (self.home / "floor.json").unlink()
        with Store(self.home) as store:
            run = store.run_begin()["id"]
        result = self.command("floor", "attach", "--key-file", self.key_file,
                              text=json.dumps({"v": 1, "type": "begin", "meeting": "off"}) + "\n")
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(json.loads(result.stdout)["code"], "not_enabled")
        with Store(self.home) as store:
            self.assertIsNone(store.meeting_pending())
            self.assertIsNone(store._hold_row())
            self.assertEqual(store.run_get(run)["status"], "running")
        self.assertEqual(self.audits()[0][2]["code"], "not_enabled")

    def test_enabled_cli_attach_completes_without_a_model(self):
        lines = [{"v": 1, "type": "begin", "meeting": "enabled"}, {"v": 1, "type": "end"}]
        result = self.command("floor", "attach", "--key-file", self.key_file,
                              text="".join(json.dumps(line) + "\n" for line in lines))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([json.loads(line)["type"] for line in result.stdout.splitlines()], ["ready", "ended"])
        with Store(self.home) as store:
            self.assertIsNone(store.meeting_pending())
            self.assertIsNone(store._hold_row())

    def test_mcp_summary_retry_then_cli_verify_reads_the_same_durable_result(self):
        cid, _ = self.promise_meeting()
        run = self.wrap_run()
        tool, args = self.summary_call()
        args["key"] = "summary-m1"
        calls = [("floor_accept", {"meeting": "m1", "cid": cid, "title": "Send review notes"}),
                 (tool, args), (tool, args)]
        messages = [{"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-11-25"}}]
        messages += [{"jsonrpc": "2.0", "id": i, "method": "tools/call",
                      "params": {"name": name, "arguments": arguments}}
                     for i, (name, arguments) in enumerate(calls, 2)]
        env = dict(os.environ)
        env.pop(SESSION_ENV, None)
        process = subprocess.run([sys.executable, "-m", "synthetics.mcp", "--home", str(self.home),
                                  "--run-id", run], input="".join(json.dumps(m) + "\n" for m in messages),
                                 stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=env, timeout=20)
        self.assertEqual(process.returncode, 0, process.stderr)
        replies = [json.loads(line) for line in process.stdout.splitlines()]
        self.assertEqual(len(replies), 4)
        for reply in replies:
            self.assertNotIn("error", reply)
            self.assertFalse(reply["result"].get("isError"), reply)
        saved = [json.loads(reply["result"]["content"][0]["text"])["id"] for reply in replies[-2:]]
        self.assertEqual(saved[0], saved[1])
        with Store(self.home) as store:
            store.run_finish(run, "Meeting wrap retained")
        result = self.command("floor", "verify", run)
        self.assertEqual(result.returncode, 0, result.stderr)
        verification = json.loads(result.stdout)
        self.assertEqual(verification, flooraccept.verify(self.home, run))
        self.assertEqual(verification["verdict"], "clean", verification)
        self.assertEqual(verification["meetings"][0]["summary"], "present")
