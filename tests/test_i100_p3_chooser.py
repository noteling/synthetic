"""#100 P3 chooser: persistent reply keyboard, selection by tap, optional `for <shortcut>` directives, inline chooser
for held messages, callback validation (C2), held bodies deleted once fed or refused (C1), config clash stops.

Real channel contract (subprocess) into two temporary homes, fake Bot API client, as test_i100_p2_adapter.
T10 T12/C1 T17 T18 T19 T20 T21 T22 + C2 + the P0 r1 carry items (T19 "For q, noteling, …", T17 Quill's outbox
carries the keyboard with ✓ = saved selection, button/directive clash is a config error)."""
import json
import unittest
from pathlib import Path

from synthetics import telegram_homes as th
from tests import test_i100_p2_adapter as p2

CHAT = p2.CHAT
CODEX = "🟦 Synthetics · Codex"
QUILL = "🟨 Noteling · Quill"


class FakeClient(p2.FakeClient):
    def __init__(self):
        super().__init__()
        self.markups = []
        self.answers = []
        self.edits = []
        self.updates = []
        self.allowed = []

    def send_message(self, chat_id, text, **kwargs):
        self.markups.append(kwargs.get("reply_markup"))
        return super().send_message(chat_id, text)

    def answer_callback_query(self, qid, text=None):
        self.answers.append((qid, text))
        return True

    def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((message_id, text))
        return True

    def get_updates(self, offset=None, timeout=0, allowed_updates=None):
        self.allowed.append(allowed_updates)
        out, self.updates = self.updates, []
        return out


def callback(update_id, data, qid=None, user=42, chat=CHAT, is_bot=False):
    return {"update_id": update_id, "callback_query": {
        "id": qid or "cb%d" % update_id, "data": data, "from": {"id": user, "is_bot": is_bot},
        "message": {"message_id": 1, "chat": {"id": chat, "type": "private"}}}}


class ChooserTest(unittest.TestCase):
    setUp = p2.HomesAdapterTest.setUp
    tearDown = p2.HomesAdapterTest.tearDown
    events = p2.HomesAdapterTest.events

    def adapter(self, paired=True, state=None, config=None):
        if config is None:
            config = json.loads(json.dumps(self.config))
            config["homes"][0].update(button=CODEX, shortcut="c")
            config["homes"][1].update(button=QUILL, shortcut="q")
        path = self.root / "telegram.json"
        path.write_text(json.dumps(config))
        if state is not None:
            (self.root / "state.json").write_text(json.dumps(state))
        self.client = FakeClient()
        a = th.HomesAdapter(str(path), client=self.client)
        if paired and not a.state.paired:
            a.state.bot_id, a.state.bot_username = p2.BOT, "testbot"
            a.state.paired, a.state.bound_user_id, a.state.bound_chat_id = True, 42, CHAT
            a.state.save()
        a.start()
        return a

    def texts(self):
        return [t for _c, t in self.client.sent]

    def bodies(self, key):
        return [e["body"] for e in self.events(key)]

    def selection(self, a):
        return (a.d.get("selection") or {}).get("key")

    # ---- T17: the keyboard ----

    def test_t17_every_send_carries_keyboard_with_saved_selection(self):
        a = self.adapter()
        a.process_update(p2.msg(1, CODEX))
        board = self.client.markups[-1]
        self.assertTrue(board["is_persistent"])
        self.assertFalse(board["one_time_keyboard"])
        self.assertEqual(board["keyboard"], [[{"text": th.MARK + CODEX}], [{"text": QUILL}]])
        self.assertEqual(board["input_field_placeholder"], "To: " + CODEX)
        # Quill's outbox message (carry T17): keyboard attached, ✓ still on Codex (the saved selection).
        self.stores["noteling"].message_send("david", "from quill", key="q1")
        a.process_outbox()
        self.assertTrue(self.texts()[-1].startswith("🟨 Quill · Noteling\n"))
        self.assertEqual(self.client.markups[-1]["keyboard"][0], [{"text": th.MARK + CODEX}])
        self.assertEqual(self.client.markups[-1]["keyboard"][1], [{"text": QUILL}])

    def test_t17_multipart_keyboard_on_last_part_only(self):
        a = self.adapter()
        self.stores["noteling"].message_send("david", ("word " * 1800).strip(), key="long")
        sleep, th.time.sleep = th.time.sleep, (lambda s: None)
        try:
            a.process_outbox()
        finally:
            th.time.sleep = sleep
        self.assertGreater(len(self.client.markups), 1)
        self.assertTrue(all(m is None for m in self.client.markups[:-1]))
        self.assertEqual(self.client.markups[-1]["input_field_placeholder"], th.PLACEHOLDER_NONE)

    def test_t17_router_texts_never_teach_syntax(self):
        a = self.adapter()
        a.process_update(p2.msg(1, "no selection yet"))           # held + inline chooser
        a.process_update(p2.msg(2, QUILL))                        # selection + release
        a.down["noteling"] = "test"
        a.process_outbox()                                        # down notice
        for i in range(3, 25):
            a.process_update(p2.msg(i, "x%d" % i, reply_to=80000 + i))   # held, then the cap notice
        notices = [t for t in self.texts() if t.startswith(th.ROUTER)]
        self.assertGreater(len(notices), 5)
        for text in notices:
            self.assertNotIn("/to", text)
            self.assertNotIn("for ", text.lower())

    def test_single_home_v2_sends_no_keyboard(self):
        config = json.loads(json.dumps(self.config))
        config["homes"] = [config["homes"][0]]
        a = self.adapter(config=config)
        self.stores["synthetics"].message_send("david", "hi", key="s", thread="codex:1")
        a.process_outbox()
        self.assertEqual(self.client.markups, [None])
        self.assertEqual(a.process_update(p2.msg(1, "Synthetics · Codex"))["reason"], "fed")   # not a selection
        a.poll_once()
        self.assertEqual(self.client.allowed[-1], None)

    # ---- T18: selection messages ----

    def test_t18_exact_button_text_selects_never_feeds(self):
        a = self.adapter()
        for i, text in enumerate([QUILL, "  " + QUILL + " ", th.MARK + QUILL], 1):
            r = a.process_update(p2.msg(i, text))
            self.assertEqual(r["reason"], "selected")
            self.assertEqual(self.selection(a), "noteling")
        self.assertEqual((self.events("synthetics"), self.events("noteling")), ([], []))
        self.assertEqual(self.texts(), [th.ROUTER + "\n" + th.TALKING_TO % QUILL] * 3)
        self.assertEqual(a.route_map[str(5001)]["kind"], "select")
        self.assertNotIn("text", a.route_map[str(5001)])
        a.process_update(p2.msg(9, "plain"))
        self.assertEqual(self.bodies("noteling"), ["plain"])

    def test_t18_lookalikes_are_not_selections(self):
        a = self.adapter()
        a.process_update(p2.msg(1, CODEX))
        fwd = p2.msg(2, QUILL)
        fwd["message"]["forward_origin"] = {"type": "user"}
        cap = p2.msg(3, QUILL)
        cap["message"]["caption"] = "c"
        for update in (fwd, cap, p2.msg(4, QUILL + " please"), p2.msg(5, QUILL.lower())):
            self.assertEqual(a.process_update(update)["reason"], "fed")
        self.assertEqual(self.selection(a), "synthetics")
        self.assertEqual(len(self.events("synthetics")), 4)
        self.assertEqual(a.process_update(p2.msg(6, QUILL, user=7))["reason"], "unrecognized_sender")
        self.assertEqual(a.process_update(p2.msg(7, QUILL, chat=8))["reason"], "unrecognized_sender")
        bot = p2.msg(8, QUILL)
        bot["message"]["from"]["is_bot"] = True
        self.assertEqual(a.process_update(bot)["reason"], "bot_sender")
        self.assertEqual(self.selection(a), "synthetics")

    # ---- T19 / T20: directives ----

    def test_t19_standalone_directives_select(self):
        a = self.adapter()
        for i, (text, key) in enumerate([("for c", "synthetics"), ("For Q.", "noteling"), ("FOR c", "synthetics")], 1):
            self.assertEqual(a.process_update(p2.msg(i, text))["reason"], "selected")
            self.assertEqual(self.selection(a), key)
        self.assertEqual((self.events("synthetics"), self.events("noteling")), ([], []))

    def test_t19_directive_with_content_strips_and_records(self):
        a = self.adapter()
        a.process_update(p2.msg(1, CODEX))
        a.process_update(p2.msg(2, "for q: please look at #136"))
        a.process_update(p2.msg(3, "for q\nsecond line"))
        # Carry T19: David's first use, "For q, noteling, …" (comma separator) goes to Quill.
        a.process_update(p2.msg(4, "For q, noteling, how is the build?"))
        a.process_update(p2.msg(5, "sticks"))
        events = self.events("noteling")
        self.assertEqual([e["body"] for e in events],
                         ["please look at #136", "second line", "noteling, how is the build?", "sticks"])
        self.assertEqual([e.get("provider", {}).get("directive") for e in events],
                         ["for q: ", "for q\n", "For q, ", None])
        self.assertEqual(self.events("synthetics"), [])
        self.assertEqual(self.selection(a), "noteling")
        talking = [t for t in self.texts() if "Talking to" in t]
        self.assertEqual(len(talking), 2)          # the tap, then the one change to Quill (not per message)

    def test_t20_near_misses_route_nothing(self):
        a = self.adapter()
        a.process_update(p2.msg(1, CODEX))
        texts = ["ask for c later", "for quality", "for c reasons", "for x: unknown", "for", "q", "c"]
        for i, text in enumerate(texts, 2):
            self.assertEqual(a.process_update(p2.msg(i, text))["reason"], "fed")
        self.assertEqual(self.bodies("synthetics"), texts)
        self.assertEqual(self.events("noteling"), [])
        self.assertEqual(self.selection(a), "synthetics")

    # ---- T21: Reply + directive ----

    def test_t21_reply_conflict_holds_until_tap(self):
        a = self.adapter()
        a.process_update(p2.msg(1, QUILL))
        c = self.stores["synthetics"].question_ask("david", "codex asks?", key="cq", thread="codex:1")
        qid = c.get("question_id") or c["id"]
        a.process_outbox()
        codex_tg = 1001 + next(i for i, t in enumerate(self.texts()) if t.endswith("codex asks?"))
        r = a.process_update(p2.msg(2, "for q: this is for quill", reply_to=codex_tg))
        self.assertEqual(r["reason"], "held")
        self.assertEqual((self.events("synthetics"), self.events("noteling")), ([], []))
        inline = self.client.markups[-1]
        self.assertIn("inline_keyboard", inline)
        self.assertEqual(self.texts()[-1], th.ROUTER + "\n" + th.CHOOSE_NOTICE)
        data = inline["inline_keyboard"][1][0]["callback_data"]
        self.assertTrue(data.startswith("sel:noteling:"))
        self.assertEqual(a.process_update(callback(3, data))["reason"], "selected")
        self.assertEqual(self.bodies("noteling"), ["this is for quill"])
        self.assertEqual(self.stores["synthetics"]._find("questions", qid)["status"], "open")
        self.assertEqual(self.events("synthetics"), [])

    def test_t21_agreeing_directive_answers_the_question(self):
        a = self.adapter()
        a.process_update(p2.msg(1, QUILL))
        c = self.stores["synthetics"].question_ask("david", "codex asks?", key="cq", thread="codex:1")
        qid = c.get("question_id") or c["id"]
        a.process_outbox()
        codex_tg = 1001 + next(i for i, t in enumerate(self.texts()) if t.endswith("codex asks?"))
        a.process_update(p2.msg(2, "for c: yes", reply_to=codex_tg))
        events = self.events("synthetics")
        self.assertEqual([e["body"] for e in events], ["yes"])
        self.assertEqual(self.stores["synthetics"]._find("questions", qid)["status"], "answered")
        self.assertEqual(self.selection(a), "synthetics")

    # ---- T10 / C2: callbacks ----

    def held_with_chooser(self, a, n=1):
        for i in range(1, n + 1):
            a.process_update(p2.msg(100 + i, "held %d" % i))
        return self.client.markups[-1]["inline_keyboard"]

    def test_t10_unauthorized_callbacks_refused(self):
        a = self.adapter()
        buttons = self.held_with_chooser(a)
        data = buttons[0][0]["callback_data"]
        cases = [(callback(2, data, user=7), "unrecognized_sender"),
                 (callback(3, data, chat=8), "unrecognized_sender"),
                 (callback(4, data, is_bot=True), "unrecognized_sender"),
                 (callback(5, "sel:nobody:%d" % a._gen()), "callback_unknown"),
                 (callback(6, "garbage"), "callback_unknown"),
                 (callback(7, "sel:synthetics:%d" % (a._gen() + 5)), "callback_stale")]
        for update, reason in cases:
            self.assertEqual(a.process_update(update)["reason"], reason)
        self.assertIsNone(a.d.get("selection"))
        self.assertEqual(len(a.d["held"]), 1)
        self.assertEqual((self.events("synthetics"), self.events("noteling")), ([], []))
        # A valid tap, then the same callback id again: replay refused, nothing fed twice.
        self.assertEqual(a.process_update(callback(8, data, qid="once"))["reason"], "selected")
        self.assertEqual(a.process_update(callback(9, data, qid="once"))["reason"], "callback_replayed")
        self.assertEqual(self.bodies("synthetics"), ["held 1"])
        self.assertIn(("once", None), self.client.answers)

    def test_c2_update_at_or_below_cursor_never_processed(self):
        a = self.adapter()
        buttons = self.held_with_chooser(a)
        data = buttons[1][0]["callback_data"]
        tap = callback(500, data, qid="tap-1")
        self.client.updates = [tap]
        a.poll_once()
        self.assertEqual(self.bodies("noteling"), ["held 1"])
        # Push the id out of the 200-id window, hold a new message (same gen would now be current again only
        # after a release, so build one), then redeliver the old update: the cursor refuses it.
        for i in range(th.CALLBACK_WINDOW + 5):
            a.process_update(callback(600 + i, "garbage", qid="filler-%d" % i))
        self.assertNotIn("tap-1", a.d["callbacks"])
        a.state.update_cursor = 900
        a.state.save()
        a.process_update(p2.msg(901, "held again", reply_to=777777))
        # The redelivered update carries a fresh callback id and the CURRENT gen, so neither the id window nor the
        # gen check refuses it: only the update cursor does (kills P3-M6).
        fresh = callback(500, "sel:noteling:%d" % a._gen(), qid="tap-new")
        self.assertEqual(a.d["held"][0]["update_id"], 901)
        self.client.updates = [fresh]
        a.poll_once()
        self.assertEqual(self.bodies("noteling"), ["held 1"])
        self.assertEqual(len(a.d["held"]), 1)
        self.assertEqual(self.client.allowed[-1], ["message", "callback_query"])

    # ---- T12 / C1 / T8: held bodies ----

    def test_c1_held_bodies_leave_state_once_fed(self):
        a = self.adapter()
        self.held_with_chooser(a, n=3)
        raw = (self.root / "state.json").read_text()
        self.assertIn("held 2", raw)
        a.process_update(p2.msg(10, CODEX))      # a keyboard tap releases held messages too
        self.assertEqual(self.bodies("synthetics"), ["held 1", "held 2", "held 3"])
        self.assertEqual(a.d["held"], [])
        done = a.d["held_done"]
        self.assertEqual([d["update_id"] for d in done], [101, 102, 103])
        self.assertTrue(all(set(d) == {"update_id", "home", "event_id", "outcome", "at"} for d in done))
        raw = (self.root / "state.json").read_text()
        for i in (1, 2, 3):
            self.assertNotIn("held %d" % i, raw)
        # The held notices lose their inline buttons and say where the message went.
        self.assertEqual(len(self.client.edits), 3)
        self.assertTrue(all(text.endswith(th.SENT_TO % CODEX) for _m, text in self.client.edits))

    def test_c1_refused_held_body_also_deleted(self):
        a = self.adapter()
        self.held_with_chooser(a)
        a.contract.call = (lambda orig: (lambda home, name, *args, **kw: [
            {"v": 1, "result": "rejected", "error": {"code": "bad_line"}}] if name == "inbound"
            else orig(home, name, *args, **kw)))(a.contract.call)
        a.process_update(p2.msg(10, QUILL))
        self.assertEqual(a.d["held"], [])
        self.assertEqual(a.d["held_done"][0]["outcome"], "rejected")
        self.assertNotIn("held 1", (self.root / "state.json").read_text())

    def test_t8_t22_restart_keeps_selection_and_held(self):
        a = self.adapter()
        a.process_update(p2.msg(1, CODEX))
        c = self.stores["synthetics"].message_send("david", "codex says", key="c", thread="codex:1")
        a.process_outbox()
        codex_tg = next(int(k) for k, v in a.route_map.items() if v.get("message") == c["id"])
        a.process_update(p2.msg(2, QUILL))
        a.d["selection"] = None                  # simulate: held while nothing selected
        a.state.save()
        a.process_update(p2.msg(3, "waiting"))
        a.process_update(p2.msg(4, QUILL))
        self.assertEqual(self.bodies("noteling"), ["waiting"])
        b = self.adapter()                       # restart
        self.assertEqual(self.selection(b), "noteling")
        self.assertEqual(self.client.sent, [])   # nothing sent by itself on restart
        b.process_update(p2.msg(5, "after restart"))
        b.process_update(p2.msg(6, "to codex", reply_to=codex_tg))
        self.assertEqual(self.bodies("noteling"), ["waiting", "after restart"])
        self.assertEqual(self.bodies("synthetics"), ["to codex"])
        self.assertEqual(self.selection(b), "noteling")   # F6: a Reply does not change the selection

    def test_stale_selection_after_pin_change_holds(self):
        a = self.adapter()
        a.process_update(p2.msg(1, QUILL))
        a.d["selection"]["employee_id"] = "employee-" + "0" * 32
        a.state.save()
        self.assertEqual(a.process_update(p2.msg(2, "who?"))["reason"], "held")

    def test_optional_to_command(self):
        a = self.adapter()
        self.assertEqual(a.process_update(p2.msg(1, "/to"))["reason"], "command")
        self.assertIsNone(a.d.get("selection"))
        self.assertEqual(a.process_update(p2.msg(2, "/to q"))["reason"], "selected")
        self.assertEqual(self.selection(a), "noteling")
        self.assertEqual((self.events("synthetics"), self.events("noteling")), ([], []))

    # ---- config ----

    def test_config_button_directive_clash_stops_adapter(self):
        config = json.loads(json.dumps(self.config))
        config["homes"][0].update(button="for q: Codex", shortcut="c")
        config["homes"][1].update(button=QUILL, shortcut="q")
        (self.root / "telegram.json").write_text(json.dumps(config))
        with self.assertRaisesRegex(th.HomesConfigError, "directive"):
            th.HomesAdapter(str(self.root / "telegram.json"), client=FakeClient())

    def test_config_shortcut_rules(self):
        for bad in ("C", "toolongxx", "a b", 5):
            config = json.loads(json.dumps(self.config))
            config["homes"][0]["shortcut"] = bad
            with self.assertRaises(th.HomesConfigError):
                th.load_homes(config["homes"], self.root)
        config = json.loads(json.dumps(self.config))
        config["homes"][0]["shortcut"] = config["homes"][1]["shortcut"] = "c"
        with self.assertRaisesRegex(th.HomesConfigError, "unique"):
            th.load_homes(config["homes"], self.root)
        config = json.loads(json.dumps(self.config))
        config["homes"][0]["button"] = "x" * 61
        with self.assertRaisesRegex(th.HomesConfigError, "placeholder"):
            th.load_homes(config["homes"], self.root)
        config = json.loads(json.dumps(self.config))
        config["homes"][0]["button"] = "✓ Codex"
        with self.assertRaises(th.HomesConfigError):
            th.load_homes(config["homes"], self.root)


if __name__ == "__main__":
    unittest.main()
