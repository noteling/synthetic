"""#100 P2 multi-home adapter through the real channel contract (subprocess), fake Bot API client.

T1 header per home, T2 multipart mapping, T4 reply to the other manager, T5/T6 tracked vs closed question,
T7 migrated legacy question, T9 re-feed duplicate, T11 ambiguous reply held (one Router notice each; a distinct
'not kept' notice at the T12 cap), T13/C3 no provider without the
feature, T14 not_before, T16 one home's pin fails alone, F2a no Store import in the adapter module."""
import json
import os
import tempfile
import unittest
from pathlib import Path

from synthetics import telegram_homes as th
from synthetics.store import Store

KEY = b"k" * 40
CHAT = 42
BOT = 99


class FakeClient:
    def __init__(self):
        self.sent = []
        self.actions = []

    def send_message(self, chat_id, text, **kwargs):
        self.sent.append((chat_id, text))
        return {"message_id": 1000 + len(self.sent)}

    def send_chat_action(self, chat_id, action="typing"):
        self.actions.append(action)
        return True

    def get_me(self):
        return {"id": BOT, "username": "testbot"}

    def get_webhook_info(self):
        return {}


def msg(update_id, text, reply_to=None, message_id=None, user=42, chat=CHAT):
    m = {"message_id": message_id or 5000 + update_id, "date": 1763650000, "text": text,
         "chat": {"id": chat, "type": "private"}, "from": {"id": user, "is_bot": False}}
    if reply_to is not None:
        m["reply_to_message"] = {"message_id": reply_to}
    return {"update_id": update_id, "message": m}


class HomesAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.stores = {}
        ids = {}
        for key, name in (("synthetics", "Codex"), ("noteling", "Quill")):
            store = Store(root / key)
            store.initialize(name, "manager")
            store.channel_register("telegram", Store.credential_hash(KEY), ["telegram:"], "test", routes=["david"])
            self.stores[key] = store
            ids[key] = store.identity()["id"]
        self.ids = ids
        keyfile = root / "telegram.key"
        keyfile.write_bytes(KEY)
        os.chmod(keyfile, 0o600)
        (root / "token").write_text("t")
        self.root = root
        self.config = dict(version=2, token_file="token", credential_file="telegram.key",
                           state_file="state.json", lock_file="tg.lock", homes=[
                               dict(key="synthetics", home="synthetics", employee_id=ids["synthetics"],
                                    colleague_id="david", label="🟦 Codex · Synthetics",
                                    button="Synthetics · Codex", thread="codex:1", primary=True),
                               dict(key="noteling", home="noteling", employee_id=ids["noteling"],
                                    colleague_id="david", label="🟨 Quill · Noteling",
                                    button="Noteling · Quill", thread=None)])

    def tearDown(self):
        for store in self.stores.values():
            store.close()
        self.tmp.cleanup()

    def adapter(self, paired=True, state=None, config=None):
        path = self.root / "telegram.json"
        path.write_text(json.dumps(config or self.config))
        if state is not None:
            (self.root / "state.json").write_text(json.dumps(state))
        self.client = FakeClient()
        a = th.HomesAdapter(str(path), client=self.client)
        if paired and not a.state.paired:
            a.state.bot_id, a.state.bot_username = BOT, "testbot"
            a.state.paired, a.state.bound_user_id, a.state.bound_chat_id = True, 42, CHAT
            a.state.save()
        a.start()
        return a

    def events(self, key):
        return [json.loads(r["data"]) for r in self.stores[key].db.execute("SELECT data FROM events ORDER BY rowid")]

    def channel_state(self, key, message_id):
        m = self.stores[key]._find("messages", message_id)
        return next(iter(m["channel_states"].values()))

    def test_t1_header_per_home_and_ack_in_own_home(self):
        a = self.adapter()
        c = self.stores["synthetics"].message_send("david", "from codex", key="c1", thread="codex:1")
        q = self.stores["noteling"].message_send("david", "from quill", key="q1")
        sent = a.process_outbox()
        self.assertEqual(sorted(sent), sorted([c["id"], q["id"]]))
        texts = [t for _chat, t in self.client.sent]
        self.assertIn("🟦 Codex · Synthetics\nfrom codex", texts)
        self.assertIn("🟨 Quill · Noteling\nfrom quill", texts)
        for key, m in (("synthetics", c), ("noteling", q)):
            st = self.channel_state(key, m["id"])
            self.assertEqual(st["state"], "delivered")
            self.assertRegex(st["receipt"], r"^telegram:42:\d+;header:[0-9a-f]{8}$")
        self.assertEqual(a.process_outbox(), [])
        self.assertEqual(len(self.client.sent), 2)

    def test_t2_multipart_every_part_mapped(self):
        a = self.adapter()
        q = self.stores["noteling"].message_send("david", ("word " * 1800).strip(), key="long")
        th_time = th.time.sleep
        th.time.sleep = lambda s: None
        try:
            a.process_outbox()
        finally:
            th.time.sleep = th_time
        self.assertGreater(len(self.client.sent), 1)
        for _chat, text in self.client.sent:
            self.assertTrue(text.startswith("🟨 Quill · Noteling\n"))
            self.assertLessEqual(len(text), 4096)
        entries = [v for v in a.route_map.values() if v.get("message") == q["id"]]
        self.assertEqual(len(entries), len(self.client.sent))
        self.assertEqual({e["home"] for e in entries}, {"noteling"})

    def test_t4_t5_reply_routing_and_tracked_answer(self):
        a = self.adapter()
        c = self.stores["synthetics"].message_send("david", "codex says", key="c", thread="codex:1")
        q = self.stores["noteling"].question_ask("david", "quill asks?", key="q")
        a.process_outbox()
        tg = {text.split("\n", 1)[1]: 1001 + i for i, (_c, text) in enumerate(self.client.sent)}
        # Reply to Codex: goes to Codex with in_reply_to = Codex's local message id; Quill unchanged.
        r = a.process_update(msg(1, "to codex", reply_to=tg["codex says"]))
        self.assertEqual(r["home"], "synthetics")
        ev = self.events("synthetics")[-1]
        self.assertEqual((ev["body"], ev["in_reply_to"], ev["thread"]), ("to codex", c["id"], "codex:1"))
        self.assertTrue(ev.get("auth"))
        self.assertEqual(self.events("noteling"), [])
        # Reply to Quill's question part: answers that question in Quill's home.
        r = a.process_update(msg(2, "the answer", reply_to=tg["quill asks?"]))
        self.assertEqual(r["home"], "noteling")
        self.assertEqual(self.stores["noteling"]._find("questions", q["question_id"] if "question_id" in q
                                                       else q["id"])["status"], "answered")
        # Not a reply: primary (P2).
        r = a.process_update(msg(3, "plain"))
        self.assertEqual(r["home"], "synthetics")

    def test_t6_closed_question_resent_as_ordinary(self):
        a = self.adapter()
        q = self.stores["noteling"].question_ask("david", "quill asks?", key="q")
        a.process_outbox()
        qid = q.get("question_id") or q["id"]
        a.process_update(msg(1, "first", reply_to=1001))
        r = a.process_update(msg(2, "second", reply_to=1001))
        self.assertTrue(r["processed"])
        evs = self.events("noteling")
        self.assertEqual(len(evs), 2)
        self.assertNotEqual(evs[1].get("reply_to"), qid)

    def test_t7_legacy_question_reply_goes_to_exact_question(self):
        q = self.stores["synthetics"].question_ask("david", "old question", key="old", thread="codex:1")
        qid = q.get("question_id") or q["id"]
        state = {"schema": 3, "bot_id": BOT, "bot_username": "testbot", "paired": True, "bound_user_id": 42,
                 "bound_chat_id": CHAT, "employee_id": self.ids["synthetics"], "bound_colleague_id": "david",
                 "bound_local_thread": "codex:1", "outbox_baseline_at": "", "deliveries": {},
                 "question_map": {"777": qid}, "pairing_rejections": [], "feed_rejections": []}
        a = self.adapter(state=state)
        self.assertEqual(a.d["schema"], 4)
        r = a.process_update(msg(1, "late answer", reply_to=777))
        self.assertEqual(r["home"], "synthetics")
        self.assertEqual(self.stores["synthetics"]._find("questions", qid)["status"], "answered")

    def test_t7_bindings_refuse_wrong_primary(self):
        state = {"schema": 3, "employee_id": self.ids["noteling"], "bound_colleague_id": "david",
                 "bound_local_thread": "codex:1", "deliveries": {}, "question_map": {}}
        with self.assertRaises(RuntimeError):
            self.adapter(state=state, paired=False)

    def test_only_paired_user_in_paired_chat(self):
        a = self.adapter()
        self.assertEqual(a.process_update(msg(1, "spoof", user=7))["reason"], "unrecognized_sender")
        self.assertEqual(a.process_update(msg(2, "other chat", chat=8))["reason"], "unrecognized_sender")
        bot = msg(3, "bot")
        bot["message"]["from"]["is_bot"] = True
        self.assertEqual(a.process_update(bot)["reason"], "bot_sender")
        group = msg(4, "group")
        group["message"]["chat"]["type"] = "group"
        self.assertEqual(a.process_update(group)["reason"], "non_private_chat")
        self.assertEqual((self.events("synthetics"), self.events("noteling")), ([], []))

    def test_t9_refeed_is_duplicate(self):
        a = self.adapter()
        a.process_update(msg(1, "once"))
        a.process_update(msg(1, "once"))
        self.assertEqual(len(self.events("synthetics")), 1)

    def test_t11_ambiguous_reply_held_with_two_homes(self):
        a = self.adapter()
        r = a.process_update(msg(1, "which one?", reply_to=12345))
        self.assertEqual(r["reason"], "held")
        self.assertEqual((self.events("synthetics"), self.events("noteling")), ([], []))
        self.assertEqual(a.d["held"][0]["update_id"], 1)

    def router_notices(self):
        return [t for _c, t in self.client.sent if t.startswith(th.ROUTER + "\n")]

    def test_hold_sends_one_router_notice_per_held_message(self):
        # Codex 18:35Z condition: a hold is never silent to David.
        a = self.adapter()
        a.process_update(msg(1, "which one?", reply_to=12345))
        self.assertEqual(self.router_notices(), [th.ROUTER + "\n" + th.HELD_NOTICE])
        a.process_update(msg(1, "which one?", reply_to=12345))   # redelivered update: no second notice
        a.process_update(msg(2, "and this?", reply_to=12346))
        notices = self.router_notices()
        self.assertEqual(notices, [th.ROUTER + "\n" + th.HELD_NOTICE] * 2)
        for text in notices:
            self.assertNotIn("/to", text)
            self.assertNotIn("Quill", text)

    def test_hold_notice_failure_only_logs(self):
        from synthetics.telegram import TelegramNetworkError
        a = self.adapter()

        def boom(chat_id, text, **kwargs):
            raise TelegramNetworkError("down")
        self.client.send_message = boom
        with self.assertLogs("synthetics.telegram_homes", level="WARNING"):
            r = a.process_update(msg(1, "which one?", reply_to=12345))
        self.assertEqual(r["reason"], "held")
        self.assertEqual(len(a.d["held"]), 1)

    def test_single_home_unmapped_reply_goes_to_primary(self):
        config = dict(self.config, homes=[self.config["homes"][0]])
        a = self.adapter(config=config)
        r = a.process_update(msg(1, "reply to something old", reply_to=12345))
        self.assertEqual(r["home"], "synthetics")
        self.assertEqual(self.events("synthetics")[0]["in_reply_to"], "tg:12345")

    def test_c3_provider_only_with_feature(self):
        a = self.adapter()
        a.process_update(msg(1, "with provider", reply_to=None, message_id=77))
        self.assertEqual(self.events("synthetics")[0].get("provider"), {"message_id": 77, "chat_id": CHAT})
        a.features = [f for f in a.features if f != "inbound.provider"]
        a.process_update(msg(2, "without"))
        self.assertIsNone(self.events("synthetics")[1].get("provider"))

    def test_t14_not_before(self):
        old = self.stores["noteling"].message_send("david", "old", key="old")
        config = json.loads(json.dumps(self.config))
        config["homes"][1]["not_before"] = old["created_at"]
        a = self.adapter(config=config)
        new = self.stores["noteling"].message_send("david", "new", key="new")
        self.assertEqual(a.process_outbox(), [new["id"]])

    def test_t16_one_home_pin_fails_alone(self):
        config = json.loads(json.dumps(self.config))
        config["homes"][1]["employee_id"] = "employee-" + "f" * 32
        a = self.adapter(config=config)
        self.assertIn("noteling", a.down)
        self.assertNotIn("synthetics", a.down)
        self.stores["noteling"].message_send("david", "not shown", key="n")
        c = self.stores["synthetics"].message_send("david", "shown", key="c", thread="codex:1")
        self.assertEqual(a.process_outbox(), [c["id"]])
        notices = [t for _c, t in self.client.sent if t.startswith("↪ Router\n")]
        self.assertEqual(len(notices), 1)
        self.assertIn("🟨 Quill · Noteling", notices[0])
        self.assertNotIn("/to", notices[0])
        a.process_outbox()   # once per stop, not per pass
        self.assertEqual(len([t for _c, t in self.client.sent if t.startswith("↪ Router")]), 1)
        # A Reply to the notice is "no reply": the current selection (P2: primary).
        notice_id = next(int(k) for k, v in a.route_map.items() if v.get("kind") == "notice")
        self.assertEqual(a.process_update(msg(9, "ok", reply_to=notice_id))["home"], "synthetics")

    def test_typing_from_status_json(self):
        a = self.adapter()
        a.process_update(msg(1, "hi quill?"))   # fed to primary
        shared = Path(self.stores["synthetics"].home) / "shared"
        shared.mkdir(exist_ok=True)
        (shared / "status.json").write_text(json.dumps({"employee": self.ids["synthetics"],
                                                        "session": {"state": "running"}}))
        a.service_activity()
        self.assertEqual(self.client.actions, ["typing"])

    def test_t8_restart_inflight_uncertain_in_its_home_never_resent(self):
        a = self.adapter()
        q = self.stores["noteling"].message_send("david", "crash mid-send", key="q")
        a.contract.one(str(self.root / "noteling"), "claim", "--message", q["id"])
        a.state.set_delivery(q["id"], {"status": "in-flight", "telegram_message_ids": [], "home": "noteling"})
        b = self.adapter()          # new instance: recovers in-flight, old claim turns uncertain in its home
        self.assertEqual(b.state.get_delivery(q["id"])["status"], "uncertain")
        self.assertEqual(b.process_outbox(), [])
        self.assertEqual(self.client.sent, [])
        rows = b.contract.call(str(self.root / "noteling"), "uncertain")
        self.assertEqual([r["message_id"] for r in rows], [q["id"]])
        self.assertEqual(b.contract.call(str(self.root / "synthetics"), "uncertain"), [])

    def test_t12_hold_cap(self):
        a = self.adapter()
        for i in range(1, 21):
            self.assertEqual(a.process_update(msg(i, "m%d" % i, reply_to=90000 + i))["reason"], "held")
        self.assertEqual(a.process_update(msg(21, "m21", reply_to=99999))["reason"], "held_cap")
        self.assertEqual(len(a.d["held"]), 20)
        self.assertEqual((self.events("synthetics"), self.events("noteling")), ([], []))
        # Codex 18:35Z condition: the 21st is dropped, and David gets a distinct notice saying it was NOT kept.
        notices = self.router_notices()
        self.assertEqual(notices[:20], [th.ROUTER + "\n" + th.HELD_NOTICE] * 20)
        self.assertEqual(len(notices), 21)
        self.assertNotEqual(notices[20], notices[0])
        self.assertIn("NOT", notices[20])
        self.assertIn("not saved", notices[20].lower())
        self.assertNotIn("/to", notices[20])

    def test_status_cli_reads_v2_config(self):
        import contextlib, io
        from synthetics.cli import main
        a = self.adapter()
        a.process_update(msg(1, "x", reply_to=4242))   # held
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            main(["--home", str(self.root / "synthetics"), "telegram", "status", "--config",
                  str(self.root / "telegram.json")])
        status = json.loads(out.getvalue())
        self.assertEqual([h["key"] for h in status["homes"]], ["synthetics", "noteling"])
        self.assertEqual((status["schema"], status["held"], status["employee"]), (4, 1, self.ids["synthetics"]))
        err = io.StringIO()
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            rc = main(["--home", str(self.root / "noteling"), "telegram", "status", "--config",
                       str(self.root / "telegram.json")])
        self.assertNotEqual(rc, 0)
        self.assertIn("another employee home", err.getvalue())

    def test_t13_serve_dispatch_v1_unchanged(self):
        from synthetics import telegram_cli
        v1 = self.root / "v1.json"
        v1.write_text(json.dumps({"token_file": "t", "colleague_id": "david", "employee_home": "x"}))
        self.assertFalse(telegram_cli._is_homes_config(v1))
        path = self.root / "telegram.json"
        path.write_text(json.dumps(self.config))
        self.assertTrue(telegram_cli._is_homes_config(path))
        self.assertFalse(telegram_cli._is_homes_config(self.root / "missing.json"))

    def test_t15_label_line_deduped_through_adapter(self):
        a = self.adapter()
        q = self.stores["noteling"].message_send("david", "🟨 Quill · Noteling\nbody", key="dup")
        a.process_outbox()
        self.assertEqual(self.client.sent[-1][1], "🟨 Quill · Noteling\nbody")
        self.assertTrue(a.state.get_delivery(q["id"])["header_deduped"])

    def test_f2a_module_never_imports_the_store(self):
        source = Path(th.__file__).read_text()
        self.assertNotIn("from .store", source)
        self.assertNotIn("import Store", source)


if __name__ == "__main__":
    unittest.main()
