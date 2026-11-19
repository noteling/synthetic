"""G4: `doctor --home <other>` from a session (proposal reviews/G4/proposal.md ffc8e812, A+ approved by Codex
event-09909511 and the widened path-flag list event-a5fb1572; Rowan's review event-a17043fb).

A session runs doctor on its own home. On another home H only while it holds an active worker.manage,
adapter.manage or runtime.request delegation whose homes cover H (audited authority.used with the delegation ID);
otherwise own_home_only, audited. Host-path flags stay own-home only in a session. The owner is unchanged."""
import json
import unittest
from datetime import datetime, timedelta, timezone

from tests import test_authority as _warrant  # module import: a bare WarrantTest name would be collected here
from synthetics.store import Store, timestamp

WORKER = {"worker.manage": {"homes": ["employees"], "ops": ["restart"]}}


class DoctorFence(_warrant.WarrantTest):
    def doctor(self, home, *flags, session=None):
        return self.cli(home, "doctor", "--json", *flags, session=session)

    def assert_ran(self, result):
        code, out, err = result
        self.assertNotIn("own_home_only", err)
        self.assertIsInstance(out, dict, err)
        self.assertIn("findings", out)
        return out

    def rows(self, name, action):
        return [a["data"] if "data" in a else a for a in self.audit(name, action)]

    def doctor_rows(self, name, action):
        return [r for r in self.rows(name, action) if json.dumps(r).find('"doctor') >= 0]

    # 1
    def test_1_own_home_allowed_without_audit_noise(self):
        self.assert_ran(self.doctor("vesper", session="vesper"))
        self.assert_ran(self.doctor("vesper", "--key-file", str(self.root / "nokey"), session="vesper"))
        self.assertEqual(self.doctor_rows("vesper", "authority.refused"), [])

    # 2
    def test_2_other_home_without_delegation_refused_and_audited(self):
        code, out, err = self.doctor("rowan", session="vesper")
        self.assertNotEqual(code, 0)
        self.assertIn("own_home_only", err)
        self.assertEqual(out, "")  # nothing of rowan's report printed
        rows = self.doctor_rows("vesper", "authority.refused")
        self.assertTrue(rows, self.audit("vesper", "authority.refused"))
        self.assertIn(str(self.homes["rowan"].resolve()), json.dumps(rows))

    # 3
    def test_3_worker_manage_covering_allows_and_audits_delegation_id(self):
        d = self.root_delegation(actions=["worker.manage"], scope=WORKER)
        self.assert_ran(self.doctor("rowan", session="codex"))
        used = self.doctor_rows("codex", "authority.used")
        self.assertTrue(any(d in json.dumps(r) and "worker.manage" in json.dumps(r) for r in used), used)

    def test_3b_runtime_request_covering_allows(self):
        d = self.root_delegation(actions=["runtime.request"],
                                 scope={"runtime.request": {"homes": [self.ids["rowan"]]}})
        self.assert_ran(self.doctor("rowan", session="codex"))
        self.assertTrue(any(d in json.dumps(r) for r in self.doctor_rows("codex", "authority.used")))

    def test_3c_adapter_manage_is_label_scoped_so_covers_no_home(self):
        self.root_delegation(actions=["adapter.manage"], scope={"adapter.manage": {"labels": ["*"]}})
        self.refused("own_home_only", "rowan", "doctor", "--json", session="codex")

    # 4
    def test_4_delegation_for_a_different_home_does_not_count(self):
        self.root_delegation(actions=["worker.manage"],
                             scope={"worker.manage": {"homes": [self.ids["vesper"]], "ops": ["restart"]}})
        self.refused("own_home_only", "rowan", "doctor", "--json", session="codex")
        self.assert_ran(self.doctor("vesper", session="codex"))

    def test_4b_revoked_delegation_does_not_count(self):
        d = self.root_delegation(actions=["worker.manage"], scope=WORKER)
        self.assert_ran(self.doctor("rowan", session="codex"))
        self.ok("codex", "authority", "retract", d, "--reason", "test")
        self.refused("own_home_only", "rowan", "doctor", "--json", session="codex")

    def test_4c_lapsed_session_run_does_not_count(self):
        # Delegations carry no expiry field; a session lapses when its run ends, and then nothing is covered.
        self.root_delegation(actions=["worker.manage"], scope=WORKER)
        with Store(self.homes["codex"]) as s:
            s.run_finish(self.runs["codex"], "done")
        self.refused("run_inactive", "rowan", "doctor", "--json", session="codex")

    # 5
    def test_5_host_path_flags_refused_cross_home_even_when_delegated(self):
        self.root_delegation(actions=["worker.manage"], scope=WORKER)
        p = str(self.root / "x.json")
        for flags in (["--key-file", p], ["--collector-plist", p], ["--compare-legacy", p],
                      ["--relay-routes", p], ["--relay-channel", "telegram"], ["--collector-dir", str(self.root)],
                      ["--label", "com.example.any"]):
            code, out, err = self.doctor("rowan", *flags, session="codex")
            self.assertNotEqual(code, 0, flags)
            self.assertIn("own_home_only", err, flags)
            self.assertIn(flags[0], err)
            self.assertEqual(out, "", flags)

    # 6
    def test_6_owner_unchanged(self):
        before = len(self.audit("rowan", "authority.refused")) + len(self.audit("vesper", "authority.refused"))
        self.assert_ran(self.doctor("rowan"))
        self.assert_ran(self.doctor("rowan", "--key-file", str(self.root / "nokey")))
        after = len(self.audit("rowan", "authority.refused")) + len(self.audit("vesper", "authority.refused"))
        self.assertEqual(before, after)

    # 7 (Rowan): the quarantine notice's recovery.doctor hint still works for a delegated manager.
    def test_7_delegated_manager_sees_routine_quarantined(self):
        now = datetime.now(timezone.utc)
        with Store(self.homes["rowan"]) as s:
            s.run_finish(self.runs["rowan"], "done")  # setUp's idle run; only one run at a time
            out = s.routine_seed(["hourly-check"], every={"hourly-check": 60},
                                 now=timestamp(now - timedelta(minutes=55)))
            wid = out["created"][0]["work_id"]
            s.due_events(now=timestamp(now + timedelta(minutes=6)))
            run = s.run_begin()
            self.assertIn(wid, [e.get("work_id") for e in run["events"]])
            s.run_finish(run["id"], "done")
            self.assertTrue(s.work_get(wid)["routine"]["quarantine"])
        self.refused("own_home_only", "rowan", "doctor", "--json", session="codex")
        self.root_delegation(actions=["worker.manage"], scope=WORKER)
        report = self.assert_ran(self.doctor("rowan", session="codex"))
        self.assertIn("routine_quarantined", [f["code"] for f in report["findings"]])


# Only this file's tests run here; the inherited WarrantTest tests run in test_authority (unittest and pytest
# skip non-callable attributes).
for _name in dir(_warrant.WarrantTest):
    if _name.startswith("test_") and _name not in vars(DoctorFence):
        setattr(DoctorFence, _name, None)
