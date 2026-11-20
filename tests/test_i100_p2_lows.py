"""Rowan's #104 review lows (L1-L3 and nits) on the #100 P2 multi-home adapter.

L1 a refused start leaves the state file unchanged; L2 honest notice for a manager that can't be reached, and a
single-home v2 config keeps v1's retry; L3 serve dispatch, primary thread-binding refusal, a home down for a
non-identity reason, the C4 skip-down-home guard and the outbox sender pin tested apart; nits: fullmatch on keys
and employee ids, `install --with-worker` on v2 takes the primary's colleague, route_map is bounded."""
import contextlib
import io
import json
import unittest
from pathlib import Path
from unittest import mock

from synthetics import telegram_homes as th
from tests import test_i100_p2_adapter as p2


class GetUpdatesClient(p2.FakeClient):
    def __init__(self, updates):
        super().__init__()
        self.updates = updates

    def get_updates(self, offset=None, timeout=None, **kwargs):
        return [u for u in self.updates if offset is None or u["update_id"] >= offset]


def _identity_fails_for(key):
    real = th.Contract.one

    def one(self, home, name, *args):
        if name == "identity" and Path(home).name == key:
            raise th.ContractError("transport", "channel identity exited 1")
        return real(self, home, name, *args)
    return one


class LowsTest(unittest.TestCase):
    setUp = p2.HomesAdapterTest.setUp
    tearDown = p2.HomesAdapterTest.tearDown
    adapter = p2.HomesAdapterTest.adapter
    events = p2.HomesAdapterTest.events
    router_notices = p2.HomesAdapterTest.router_notices

    # ---- L1 ----

    def _refused(self, state):
        path = self.root / "state.json"
        with self.assertRaises(RuntimeError) as caught:
            self.adapter(state=state, paired=False)
        return path, caught.exception

    def test_l1_refused_start_leaves_state_file_unchanged(self):
        state = {"schema": 3, "employee_id": self.ids["noteling"], "bound_colleague_id": "david",
                 "bound_local_thread": "codex:1", "deliveries": {"m": {"status": "accepted"}},
                 "question_map": {"700": "question-" + "a" * 32}}
        raw = json.dumps(state)
        path, error = self._refused(state)
        self.assertIn("identity mismatch", str(error))
        self.assertEqual(path.read_text(), raw)          # no schema 4, no route_map written
        self.assertFalse((self.root / "state.json.guard").exists())

    def test_l3_primary_thread_binding_refusal(self):
        state = {"schema": 3, "employee_id": self.ids["synthetics"], "bound_colleague_id": "david",
                 "bound_local_thread": "codex:OTHER", "deliveries": {}, "question_map": {}}
        raw = json.dumps(state)
        path, error = self._refused(state)
        self.assertIn("Local thread mismatch", str(error))
        self.assertEqual(path.read_text(), raw)

    def test_l1_colleague_refusal_also_writes_nothing(self):
        state = {"schema": 3, "employee_id": self.ids["synthetics"], "bound_colleague_id": "someone-else",
                 "bound_local_thread": "codex:1", "deliveries": {}, "question_map": {}}
        raw = json.dumps(state)
        path, error = self._refused(state)
        self.assertIn("Colleague ID mismatch", str(error))
        self.assertEqual(path.read_text(), raw)

    def test_l1_accepted_start_still_migrates_and_binds(self):
        a = self.adapter(paired=False)
        saved = json.loads((self.root / "state.json").read_text())
        self.assertEqual((saved["schema"], saved["employee_id"], saved["bound_local_thread"]),
                         (4, self.ids["synthetics"], "codex:1"))
        self.assertEqual(a.state.bound_colleague_id, "david")

    # ---- L2 / L3 home down for a non-identity reason ----

    def test_l3_l2_home_down_for_non_identity_reason_held_with_honest_notice(self):
        with mock.patch.object(th.Contract, "one", _identity_fails_for("noteling")):
            a = self.adapter()
        self.assertIn("identity check failed", a.down["noteling"])
        self.assertNotIn("synthetics", a.down)
        a.route_map["777"] = dict(home="noteling", employee_id=self.ids["noteling"], message=None, question=None,
                                  kind="out")
        result = a.process_update(p2.msg(1, "for quill", reply_to=777))
        self.assertEqual(result["reason"], "held")
        self.assertEqual(a.d["held"][0]["reason"], "home_down")
        notice = self.router_notices()[-1]
        self.assertIn("🟨 Quill · Noteling", notice)
        self.assertIn("can't be reached", notice)
        self.assertNotIn("chooser", notice)
        self.assertEqual(self.events("noteling"), [])

    def test_l2_single_home_v2_down_retries_like_v1(self):
        config = json.loads(json.dumps(self.config))
        config["homes"] = [config["homes"][0]]
        config["homes"][0]["employee_id"] = "employee-" + "f" * 32
        a = self.adapter(config=config)
        self.assertIn("synthetics", a.down)
        for i in range(1, 26):   # past the hold cap: nothing is held, nothing dropped
            self.assertEqual(a.process_update(p2.msg(i, "m%d" % i))["reason"], "store_error")
        self.assertEqual(a.d.get("held") or [], [])
        self.assertEqual(self.router_notices(), [])
        a.client = GetUpdatesClient([p2.msg(30, "hello")])
        self.assertFalse(a.poll_once())
        self.assertIsNone(a.state.update_cursor)        # the cursor never moves past the update
        self.assertEqual(self.events("synthetics"), [])

    # ---- L3 C4 guard and sender pin, apart ----

    def test_l3_c4_down_home_outbox_skipped(self):
        # Pin of noteling fails for a transport reason; its rows carry the right sender, so only the
        # skip-down-home guard keeps them from being sent.
        with mock.patch.object(th.Contract, "one", _identity_fails_for("noteling")):
            a = self.adapter()
        n = self.stores["noteling"].message_send("david", "from a down home", key="n")
        c = self.stores["synthetics"].message_send("david", "shown", key="c", thread="codex:1")
        self.assertEqual(a.process_outbox(), [c["id"]])
        self.assertFalse(any("from a down home" in t for _c, t in self.client.sent))
        self.assertIsNone(a.state.get_delivery(n["id"]))
        self.assertEqual([r["message_id"] for r in a.contract.call(str(self.root / "noteling"), "pending")],
                         [n["id"]])

    def test_l3_outbox_sender_pin_stops_home(self):
        a = self.adapter()
        self.assertEqual(a.down, {})
        # The home now holds another employee than the pin says (e.g. the directory was replaced after start).
        a.homes[1]["employee_id"] = "employee-" + "e" * 32
        n = self.stores["noteling"].message_send("david", "wrong sender", key="n")
        self.assertEqual(a.process_outbox(), [])
        self.assertIn("outbox sender", a.down["noteling"])
        self.assertFalse(any("wrong sender" in t for _c, t in self.client.sent))
        self.assertIsNone(a.state.get_delivery(n["id"]))
        self.assertEqual(a.contract.call(str(self.root / "noteling"), "uncertain"), [])   # never claimed

    # ---- L3 serve dispatch ----

    def test_l3_serve_dispatch(self):
        from synthetics.cli import main
        ran = []

        class Fake:
            def __init__(self, name):
                self.name = name

            def __call__(self, path, *a, **k):
                ran.append((self.name, path))
                return self

            def run(self):
                return None

        v2 = self.root / "telegram.json"
        v2.write_text(json.dumps(self.config))
        v1 = self.root / "v1.json"
        v1.write_text(json.dumps({"token_file": str(self.root / "token"), "colleague_id": "david",
                                  "employee_home": str(self.root / "synthetics"), "local_thread": "codex:1",
                                  "state_file": str(self.root / "v1.state.json")}))
        with mock.patch("synthetics.telegram_homes.HomesAdapter", Fake("homes")), \
                mock.patch("synthetics.telegram.TelegramAdapter", Fake("single")), \
                contextlib.redirect_stdout(io.StringIO()):
            for config in (v2, v1):
                rc = main(["--home", str(self.root / "synthetics"), "telegram", "serve", "--config", str(config)])
                self.assertEqual(rc, 0)
        self.assertEqual([name for name, _p in ran], ["homes", "single"])

    # ---- nits ----

    def test_nit_key_and_employee_fullmatch(self):
        for field, value in (("key", "noteling\n"), ("employee_id", self.ids["noteling"] + "\n")):
            entries = json.loads(json.dumps(self.config["homes"]))
            entries[1][field] = value
            with self.assertRaises(th.HomesConfigError):
                th.load_homes(entries, self.root)

    def test_nit_install_worker_colleague_from_primary(self):
        from synthetics import telegram_cli
        v2 = self.root / "telegram.json"
        config = json.loads(json.dumps(self.config))
        config["homes"][0]["colleague_id"] = config["homes"][1]["colleague_id"] = "alex"
        v2.write_text(json.dumps(config))
        self.assertEqual(telegram_cli._worker_colleague(v2), "alex")
        v1 = self.root / "v1.json"
        v1.write_text(json.dumps({"colleague_id": "david"}))
        self.assertEqual(telegram_cli._worker_colleague(v1), "david")
        self.assertEqual(telegram_cli._worker_colleague(self.root / "missing.json"), "")

    def test_nit_route_map_bounded_oldest_first(self):
        a = self.adapter()
        a.route_map.clear()
        for i in range(th.ROUTE_MAP_CAP + 5):
            a.route_map[str(i)] = dict(kind="notice", at="x")
        a.state.save()
        saved = json.loads((self.root / "state.json").read_text())["route_map"]
        self.assertEqual(len(saved), th.ROUTE_MAP_CAP)
        self.assertNotIn("4", saved)
        self.assertIn("5", saved)
        self.assertIn(str(th.ROUTE_MAP_CAP + 4), saved)
        # A Reply to a dropped target is unmapped: held, never guessed.
        self.assertEqual(a.process_update(p2.msg(1, "late", reply_to=0))["reason"], "held")


if __name__ == "__main__":
    unittest.main()
