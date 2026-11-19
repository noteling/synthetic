"""Independent service acceptance: publish only after both processes are listening."""

from datetime import datetime, timezone
import http.server
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse

from synthetics.store import Store


class TelegramOperationTests(unittest.TestCase):
    def test_running_channel_and_worker_continue_questions_memory_and_proactive_work_after_restart(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            home = root / "employee"
            thread = "continuing-human-conversation"
            with Store(home) as store:
                employee = store.initialize("Aster", "Maintainer")
                store.entity_put("david", "David", "person")
                store.thread_import(thread, [dict(id="old-human", sender="david", direction="incoming",
                                                  body="We started this conversation elsewhere.")], source="handoff-test")
                old = store.message_send("david", "HISTORICAL: never replay this", key="old", thread=thread)
                # M22: the adapter authenticates its inputs with a channel key file.
                channel_key = root / "telegram.key"
                channel_key.write_text("c" * 64 + "\n")
                channel_key.chmod(0o600)
                store.channel_register("telegram", Store.credential_hash(channel_key.read_bytes()), ["telegram:"], "test",
                                       routes=["david"])
            token = root / "token"
            token.write_text("123:FAKE_TEST_TOKEN")
            token.chmod(0o600)
            state_path = home / "channel.state.json"
            state_path.write_text(json.dumps(dict(schema=1, pairing_nonce="test-pairing-nonce", paired=False,
                                                  bot_id=123, bot_username="test_bot", bound_user_id=None,
                                                  bound_chat_id=None, update_cursor=None, outbox_baseline_at=None,
                                                  deliveries={}, question_map={})))
            state_path.chmod(0o600)
            config = home / "channel.json"
            config.write_text(json.dumps(dict(token_file=str(token), employee_home=str(home), colleague_id="david",
                                              local_thread=thread, state_file=str(state_path),
                                              credential_file=str(channel_key))))
            updates, sent, guard = [], [], threading.Lock()
            listening = threading.Event()

            class Provider(http.server.BaseHTTPRequestHandler):
                def log_message(self, *args):
                    pass

                def respond(self):
                    size = int(self.headers.get("Content-Length", 0))
                    payload = urllib.parse.parse_qs(self.rfile.read(size).decode())
                    method = self.path.rsplit("/", 1)[-1]
                    if method == "getMe":
                        value = dict(id=123, username="test_bot", is_bot=True)
                    elif method == "getWebhookInfo":
                        value = dict(url="")
                    elif method == "getUpdates":
                        listening.set()
                        time.sleep(0.05)
                        offset = int(payload.get("offset", [0])[0])
                        with guard:
                            value = [item for item in updates if item["update_id"] >= offset]
                    elif method == "sendMessage":
                        with guard:
                            value = dict(message_id=100 + len(sent), chat=dict(id=42),
                                         text=payload["text"][0], date=int(time.time()))
                            sent.append(value)
                    elif method == "sendChatAction":
                        value = True
                    else:
                        raise AssertionError("Unexpected provider method " + method)
                    encoded = json.dumps(dict(ok=True, result=value)).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(encoded)))
                    self.end_headers()
                    self.wfile.write(encoded)

                do_GET = respond
                do_POST = respond

            server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Provider)
            server_thread = threading.Thread(target=server.serve_forever, daemon=True)
            server_thread.start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
            base = "http://127.0.0.1:%s" % server.server_port
            brain = root / "test_brain"
            brain.write_text("#!" + sys.executable + "\n" + '''
import json,os,sys,time
from datetime import datetime,timedelta,timezone
from synthetics.store import Store
raw=sys.stdin.read(); c=json.loads(raw[raw.index('{'):])
with Store(os.environ['SYNTHETICS_HOME'],run_id=c['run']['id']) as s:
 for e in c['events']:
  t=e.get('thread') or 'continuing-human-conversation'
  if e['kind']=='reminder':
   s.message_send('david','PROACTIVE amber',thread=t,key='proactive')
   time.sleep(0.3)  # keep the session open after a delivered send: stop() must still wait for run_finish
  elif 'pairing confirmed' in e['body']:
   assert any('elsewhere' in i['body'] for i in c['threads'][t]['items'])
   s.message_send('david','READY',thread=t,key='ready')
  elif e['body']=='Remember amber':
   s.memory_put('david','choice','amber',source=e['id'],key='choice')
   w=s.work_put('Confirm choice',key='confirm-work')
   s.question_ask('david','Is amber final?',work_id=w['id'],thread=t,key='confirm-question')
  elif e['body']=='yes':
   assert e['reply_to']
   assert e['auth']['channel']=='telegram'
   for w in s.work_list('open'): s.work_put(id=w['id'],status='done',note='Human confirmed',evidence=[{'level':'verified','ref':e['id']}])
   s.message_send('david','CONFIRMED amber',thread=t,key='confirmed')
   s.reminder_set((datetime.now(timezone.utc)+timedelta(seconds=1)).isoformat(),'test proactive follow-up',key='followup')
  elif e['body']=='What did I choose?':
   assert s.memory_search(subject='david')[0]['value']=='amber'
   h=s.thread_history(t)
   assert any(i['body']=='Is amber final?' for i in h['items'])
   assert any(i['body']=='yes' for i in h['items'])
   s.message_send('david','CONTEXT amber',thread=t,key='context-after-restart')
   time.sleep(0.3)
print(json.dumps({'result':'Handled captured input through durable employee state.','total_cost_usd':0}))
''')
            brain.chmod(0o700)
            package = str(Path(__file__).resolve().parent.parent)
            env = dict(os.environ, PYTHONPATH=package)
            processes, logs = [], []

            def start():
                listening.clear()
                # Change only the HTTP endpoint. The actual CLI, adapter loop,
                # client, Store, work engine and process lifecycle all execute.
                runner = ("from unittest.mock import patch; from synthetics.telegram import TelegramAdapter,TelegramClient; "
                          "from synthetics.cli import main; "
                          "factory=lambda self:TelegramClient(self.config.load_token(),api_base=%r); "
                          "p=patch.object(TelegramAdapter,'_make_client',factory);p.start(); "
                          "raise SystemExit(main(%r))") % (base, ["--home", str(home), "telegram", "serve", "--config", str(config)])
                commands = [[sys.executable, "-c", runner],
                            [sys.executable, "-m", "synthetics", "--home", str(home), "serve", "--claude", str(brain),
                             "--poll", "0.05", "--timeout", "10", "--budget", "1"]]
                for command in commands:
                    log = tempfile.TemporaryFile()
                    logs.append(log)
                    processes.append(subprocess.Popen(command, cwd=package, env=env, stdout=log, stderr=log, start_new_session=True))
                self.assertTrue(listening.wait(5), "Adapter did not reach its polling loop")

            def stop():
                for process in processes:
                    if process.poll() is None:
                        os.killpg(process.pid, signal.SIGTERM)
                for process in processes:
                    try:
                        process.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
                processes.clear()

            def await_idle():
                # A delivered send happens inside the session; its inputs are marked processed only by run_finish
                # after the brain exits. Stopping earlier interrupts the run and (by design) keeps them pending.
                # Interrupted-run replay is the documented at-least-once contract, so wait for quiescence, never race it.
                deadline = time.monotonic() + 15
                pending, running, exited = [], [], []
                while time.monotonic() < deadline:
                    with Store(home) as store:
                        pending = store.event_pending()
                        running = [run["id"] for run in store.run_list(limit=50) if run["status"] == "running"]
                    if not pending and not running:
                        return
                    exited = [(process.pid, process.returncode) for process in processes if process.poll() is not None]
                    if exited:
                        break
                    time.sleep(0.05)
                self.fail("Worker not idle before stop (15 s bound): pending events %s; running runs %s; exited (pid, rc) %s" % (
                    [(event.get("id"), event.get("kind"), (event.get("body") or "")[:80]) for event in pending],
                    running, exited))

            self.addCleanup(stop)
            for_log_cleanup = lambda: [log.close() for log in logs]
            self.addCleanup(for_log_cleanup)

            def publish(body, sender=42, reply=None):
                with guard:
                    number = len(updates) + 1
                    message = dict(message_id=number, date=int(time.time()), text=body,
                                   chat=dict(id=sender, type="private"), **{"from": dict(id=sender, is_bot=False)})
                    if reply is not None:
                        message["reply_to_message"] = dict(message_id=reply)
                    updates.append(dict(update_id=number, message=message))

            def await_text(text):
                deadline = time.monotonic() + 15
                while time.monotonic() < deadline:
                    with guard:
                        matches = [message for message in sent if message["text"] == text]
                    if matches:
                        return matches[0]
                    if any(process.poll() is not None for process in processes):
                        break
                    time.sleep(0.05)
                details = []
                for log in logs:
                    log.seek(0)
                    details.append(log.read().decode(errors="replace")[-3000:])
                self.fail("Expected %r; service logs: %s" % (text, details))

            start()
            publish("/start test-pairing-nonce")
            await_text("READY")
            publish("Remember amber")
            question = await_text("Is amber final?")
            publish("yes", reply=question["message_id"])
            await_text("CONFIRMED amber")
            await_text("PROACTIVE amber")  # No incoming message caused this send.
            await_idle()
            stop()
            with guard:
                before_restart = len(sent)
            start()
            publish("Other person", sender=999)
            publish("What did I choose?")
            await_text("CONTEXT amber")
            await_idle()
            stop()
            with guard:
                self.assertEqual(len(sent), before_restart + 1)
                self.assertFalse(any("HISTORICAL" in item["text"] for item in sent))
            with Store(home) as store:
                self.assertEqual(store.identity()["id"], employee["id"])
                self.assertFalse(any(event["body"] == "Other person" for event in store.event_search()))
                self.assertEqual(store.question_list()[0]["status"], "answered")
                self.assertEqual(store.work_list()[0]["status"], "done")
                self.assertEqual(store.event_pending(), [])


if __name__ == "__main__":
    unittest.main()
