import json
import os
from pathlib import Path
import tempfile
import time
import unittest

from synthetics.codex_history import CodexHistoryError, CodexHistoryExporter, write_snapshot
from synthetics.store import Store


THREAD = "explicit-thread-id"


def visible(item_id, kind, body, turn="turn-1", started=1000, completed=2000):
    item = {"id": item_id, "type": kind}
    if kind == "userMessage":
        item["content"] = [{"type": "text", "text": body}]
    else:
        item.update(text=body, phase="final_answer")
    return {"turnId": turn, "startedAtMs": started, "completedAtMs": completed, "item": item}


class CodexHistoryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.requests = self.root / "requests.jsonl"

    def tearDown(self):
        self.temporary.cleanup()

    def executable(self, pages=None, **options):
        fixture = self.root / "fixture.json"
        fixture.write_text(json.dumps(dict(options, pages=pages or [{"data": []}])))
        path = self.root / "fake-codex"
        path.write_text(
            "#!/usr/bin/env python3\n"
            "import json, os, subprocess, sys, time\n"
            "assert sys.argv[1:] == ['app-server', '--listen', 'stdio://']\n"
            "fixture = json.load(open(%r))\n" % str(fixture)
            + "page = 0\n"
            "for line in sys.stdin:\n"
            " request = json.loads(line)\n"
            " with open(%r, 'a') as log: log.write(json.dumps(request)+'\\n')\n" % str(self.requests)
            + " method = request['method']\n"
            " assert method in ['initialize', 'initialized', 'thread/read', 'thread/items/list']\n"
            " if method == 'initialized': continue\n"
            " if method == 'initialize': result = {'userAgent':'test'}\n"
            " elif method == 'thread/read':\n"
            "  assert request['params'] == {'threadId': %r, 'includeTurns':False}\n" % THREAD
            + "  result = {'thread':{'id': fixture.get('response_thread', %r), 'cwd':'/assigned', 'status':{'type':'notLoaded'}}}\n" % THREAD
            + " else:\n"
            "  assert request['params']['threadId'] == %r\n" % THREAD
            + "  assert request['params']['sortDirection'] == 'asc'\n"
            "  if fixture.get('hang'):\n"
            "   subprocess.Popen([sys.executable,'-c',fixture['child_code']])\n"
            "   time.sleep(10)\n"
            "  result = fixture['pages'][page]; page += 1\n"
            " if method == 'thread/items/list' and fixture.get('bad_json'):\n"
            "  print('not JSON', flush=True); continue\n"
            " if method == 'thread/items/list' and fixture.get('interaction'):\n"
            "  print(json.dumps({'id':999, 'method':'account/login', 'params':{}}),flush=True); continue\n"
            " response = {'id':request['id'], 'result':result}\n"
            " if fixture.get('wrong_id'): response['id'] = 999\n"
            " if fixture.get('provider_error'): response = {'id':request['id'], 'error':{'message':'secret-token-must-not-leak'}}\n"
            " print(json.dumps({'method':'thread/status/changed', 'params':{}}),flush=True)\n"
            " print(json.dumps(response), flush=True)\n"
        )
        path.chmod(0o700)
        return str(path)

    def test_pages_complete_visible_history_and_preserves_authorship_body_and_provenance(self):
        first = visible("u1", "userMessage", "  Why?\nDo investigate.  ")
        answer = visible("a1", "agentMessage", "\nI retained the answer.\n", started=3000)
        attachment = visible("u2", "userMessage", "", turn="turn-2", started=None, completed=None)
        attachment["item"]["content"] = [{"type": "localImage", "path": "/assigned/image.png"}]
        compaction = {"turnId": "turn-1", "item": {"type": "contextCompaction", "id": "c1"}}
        reasoning = {"turnId": "turn-1", "item": {"type": "reasoning", "id": "r1", "content": ["do not export"]}}
        executable = self.executable([
            {"data": [first, answer, reasoning], "nextCursor": "page-2"},
            {"data": [compaction, attachment], "nextCursor": None},
        ])
        snapshot = CodexHistoryExporter(executable).export(THREAD, "human-uuid", "manager-uuid")
        self.assertEqual(snapshot["thread"], "codex:" + THREAD)
        self.assertTrue(snapshot["available_history_exhausted"])
        self.assertEqual(snapshot["page_count"], 2)
        messages = snapshot["messages"]
        self.assertEqual([m["id"] for m in messages], ["u1", "a1", "u2"])
        self.assertEqual([m["sender"] for m in messages], ["human-uuid", "manager-uuid", "human-uuid"])
        self.assertEqual([m["direction"] for m in messages], ["incoming", "outgoing", "incoming"])
        self.assertEqual(messages[0]["body"], first["item"]["content"][0]["text"])
        self.assertEqual(messages[1]["body"], answer["item"]["text"])
        self.assertEqual(messages[0]["occurred_at"], "1970-01-01T00:00:01+00:00")
        self.assertIsNone(messages[2]["occurred_at"])
        self.assertEqual(messages[2]["provenance"]["content"], attachment["item"]["content"])
        self.assertEqual(messages[1]["provenance"]["phase"], "final_answer")
        self.assertNotIn("do not export", json.dumps(snapshot))
        self.assertEqual(snapshot["omitted_item_types"], {"reasoning": 1, "contextCompaction": 1})
        self.assertTrue(any("compaction" in warning for warning in snapshot["warnings"]))
        self.assertTrue(any("timestamp" in warning for warning in snapshot["warnings"]))
        requests = [json.loads(line) for line in self.requests.read_text().splitlines()]
        self.assertEqual([r["method"] for r in requests], ["initialize", "initialized", "thread/read", "thread/items/list", "thread/items/list"])
        self.assertEqual(requests[-1]["params"]["cursor"], "page-2")

    def test_private_atomic_snapshot_and_history_only_import_do_not_replay_messages(self):
        executable = self.executable([{"data": [visible("u1", "userMessage", "Original question"), visible("a1", "agentMessage", "Original answer")]}])
        path = self.root / "snapshot.json"
        path.write_text("old snapshot")
        snapshot = CodexHistoryExporter(executable).export_to(THREAD, path, "david")
        self.assertEqual(json.loads(path.read_text()), snapshot)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        store = Store(self.root / "employee")
        try:
            store.initialize("Manager", "manager", "Continuing work")
            before_pending = store.event_pending()
            before_outbox = store.outbox_list()
            imported = store.thread_import(snapshot["thread"], snapshot["messages"], "codex-app-server:" + THREAD)
            self.assertEqual(len(imported), 2)
            self.assertEqual(store.event_pending(), before_pending)
            self.assertEqual(store.outbox_list(), before_outbox)
            again = store.thread_import(snapshot["thread"], snapshot["messages"], "codex-app-server:" + THREAD)
            self.assertEqual(again, imported)
            history = store.thread_history(snapshot["thread"])
            self.assertEqual([m["body"] for m in history["items"]], ["Original question", "Original answer"])
            self.assertEqual(history["items"][0]["provenance"]["item_id"], "u1")
        finally:
            store.close()

    def test_failures_leave_prior_snapshot_unchanged_and_remove_temporary_files(self):
        cases = [
            ({"bad_json": True}, [{"data": []}], "invalid JSON"),
            ({"response_thread": "somebody-else"}, [{"data": []}], "different or invalid thread"),
            ({"interaction": True}, [{"data": []}], "read-only"),
            ({"wrong_id": True}, [{"data": []}], "unexpected response ID"),
            ({"provider_error": True}, [{"data": []}], "rejected initialize"),
            ({}, [{"data": "wrong"}], "page is invalid"),
            ({}, [{"data": [{"item": {"type": []}}]}], "invalid item type"),
            ({}, [{"data": [visible("u1", "agentMessage", 42)]}], "invalid text"),
            ({}, [{"data": [], "nextCursor": False}], "invalid or repeated page cursor"),
            ({}, [{"data": [], "nextCursor": "same"}, {"data": [], "nextCursor": "same"}], "invalid or repeated page cursor"),
            ({}, [{"data": [visible("u1", "userMessage", "a"), visible("u1", "userMessage", "a")]}], "repeated a visible item ID"),
        ]
        path = self.root / "snapshot.json"
        for options, pages, expected in cases:
            with self.subTest(expected=expected):
                path.write_text("prior accepted snapshot")
                executable = self.executable(pages, **options)
                with self.assertRaisesRegex(CodexHistoryError, expected) as raised:
                    CodexHistoryExporter(executable).export_to(THREAD, path, "david")
                self.assertNotIn("secret-token", str(raised.exception))
                self.assertEqual(path.read_text(), "prior accepted snapshot")
                self.assertFalse(list(self.root.glob(".snapshot.json.*")))

    def test_limits_fail_visibly_instead_of_publishing_truncated_history(self):
        executable = self.executable([{"data": [], "nextCursor": "more"}])
        with self.assertRaisesRegex(CodexHistoryError, "page limit"):
            CodexHistoryExporter(executable, max_pages=1).export(THREAD, "david")
        executable = self.executable([{"data": [visible("u1", "userMessage", "x" * 2000)]}])
        with self.assertRaisesRegex(CodexHistoryError, "size limit"):
            CodexHistoryExporter(executable, max_response_bytes=1000).export(THREAD, "david")

    def test_timeout_kills_the_cli_and_its_helpers(self):
        effect = self.root / "late-helper-effect"
        child_code = "import pathlib,time; time.sleep(0.6); pathlib.Path(%r).write_text('late')" % str(effect)
        executable = self.executable(hang=True, child_code=child_code)
        started = time.monotonic()
        with self.assertRaisesRegex(CodexHistoryError, "timed out"):
            CodexHistoryExporter(executable, timeout=0.15).export(THREAD, "david")
        self.assertLess(time.monotonic() - started, 3)
        time.sleep(0.7)
        self.assertFalse(effect.exists())

    def test_requires_explicit_thread_and_rejects_incomplete_snapshot(self):
        for thread in (None, "", " ", 123):
            with self.subTest(thread=thread), self.assertRaises(ValueError):
                CodexHistoryExporter().export(thread, "david")
        for options in ({"timeout": float("inf")}, {"timeout": 0}, {"max_pages": True}, {"max_response_bytes": 0}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                CodexHistoryExporter(**options)
        with self.assertRaises(ValueError):
            write_snapshot({"available_history_exhausted": False}, self.root / "partial.json")
        with self.assertRaisesRegex(CodexHistoryError, "Could not start"):
            CodexHistoryExporter(str(self.root / "absent")).export(THREAD, "david")


if __name__ == "__main__":
    unittest.main()
