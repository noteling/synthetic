"""A real Floor process must surface a safe provider failure, not an empty-answer symptom."""
import json
import os
from pathlib import Path
import queue
import subprocess
import sys
import threading

from synthetics import floorjournal, runtime
from synthetics.store import Store
from tests import test_mtg_f3_gate as fixture


class ProviderErrorTransport(fixture.Base):
    def test_quota_failure_reaches_client_and_meeting_closes_cleanly(self):
        fake = self.root / "quota-cli"
        fake.write_text("#!" + sys.executable + "\n" + '''import json,sys
for raw in sys.stdin:
    print(json.dumps({"type":"result","is_error":True,
        "result":"You've hit your weekly limit · resets 8am (America/New_York)",
        "total_cost_usd":0.125}),flush=True)
''')
        fake.chmod(0o700)
        runtime.write_json(self.home / runtime.STATE_FILE, {"policy": {}, "effective": {
            "brain": "claude", "claude": str(fake), "model": "fixture", "budget": 100}})
        env = dict(os.environ)
        env.pop("SYNTHETICS_SESSION_RUN", None)
        errors = (self.root / "attach.stderr").open("w")
        self.addCleanup(errors.close)
        child = subprocess.Popen([sys.executable, "-m", "synthetics", "--home", str(self.home),
                                  "floor", "attach", "--key-file", self.key_file],
                                 stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                                 text=True, env=env)
        def cleanup():
            if child.poll() is None:
                child.terminate()
            child.wait(timeout=10)
            child.stdin.close()
            child.stdout.close()
        self.addCleanup(cleanup)
        lines = queue.Queue()
        reader = threading.Thread(target=lambda: [lines.put(json.loads(raw)) for raw in child.stdout], daemon=True)
        reader.start()
        def send(item):
            child.stdin.write(json.dumps(dict(v=1, **item)) + "\n")
            child.stdin.flush()
        send({"type": "begin", "meeting": "provider-limit", "goal": "Introduce yourself"})
        ready = lines.get(timeout=10)
        self.assertEqual(ready["type"], "ready", ready)
        send({"type": "ask", "id": "a1", "rev": 0, "question": "Who are you?"})
        failure = lines.get(timeout=10)
        self.assertEqual(failure["type"], "error", failure)
        self.assertIn("usage limit", failure["message"].lower(), failure)
        self.assertIn("8am (America/New_York)", failure["message"])
        while True:
            ended = lines.get(timeout=10)
            self.assertNotEqual(ended["type"], "answer", ended)
            if ended["type"] == "ended":
                break
        child.stdin.close()
        self.assertEqual(child.wait(timeout=10), 0)
        reader.join(timeout=2)
        with Store(self.home) as store:
            self.assertIsNone(store._hold_row())
            self.assertIsNone(store.meeting_pending())
            run = store.run_get(ready["run"])
            self.assertEqual(run["cost"], 0.125)
            wrap = next(e for e in store.event_search() if e.get("source") == "stand:meeting")
            body = json.loads(wrap["body"])
            self.assertEqual(body["reason"], "session_failed")
            self.assertEqual(floorjournal.verify(self.home, "provider-limit", body["head"])["chain"], "chain_ok")
