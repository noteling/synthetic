"""M21 test 1 (revocation race, r6 5.1) and project grants (D7.2)."""
import sqlite3
import tempfile
import threading
import time
import unittest
from pathlib import Path

from synthetics.projects import ProjectError, ProjectStore
from synthetics.store import Store


def lesson(text="l"):
    return {"kind": "lesson", "body": {"statement": text, "lesson_kind": "discovery", "evidence": [],
                                        "applies_when": "x", "uncertainty": "none known"}}


class FenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.home = root / "home"
        self.emp = Store(self.home)
        self.run = self.emp.run_begin()["id"]
        self.ps = ProjectStore(root / "projects" / "synthetics" / "memory.sqlite3", "synthetics")

    def tearDown(self):
        self.ps.close()
        self.emp.close()
        self.tmp.cleanup()

    def append(self, **kw):
        return self.ps.append([lesson()], author="employee-x", run=self.run,
                              fence=self.emp.path, **kw)

    def test_active_run_appends(self):
        out = self.append()
        self.assertEqual(out["seq_to"], 1)

    def test_revoked_before_is_refused_and_writes_nothing(self):
        self.emp.run_fail(self.run, "revoked")
        with self.assertRaises(ProjectError) as cm:
            self.append()
        self.assertEqual(cm.exception.code, "run_inactive")
        self.assertEqual(self.ps.head_seq(), 0)
        self.assertEqual(self.ps.db.execute("SELECT COUNT(*) FROM audit").fetchone()[0], 0)
        self.assertEqual(self.ps.db.execute("PRAGMA database_list").fetchall()[-1][1], "main")  # detached

    def test_missing_run_refused(self):
        with self.assertRaises(ProjectError) as cm:
            self.ps.append([lesson()], author="employee-x", run=None, fence=self.emp.path)
        self.assertEqual(cm.exception.code, "not_fenced")

    def test_revocation_blocked_during_project_write(self):
        """While the fenced append holds its transaction, the employee Store cannot be written."""
        probe = {}

        def guard(_db):
            other = sqlite3.connect(str(self.emp.path), timeout=0, isolation_level=None)
            try:
                other.execute("BEGIN IMMEDIATE")
                probe["locked"] = False
                other.execute("ROLLBACK")
            except sqlite3.OperationalError as exc:
                probe["locked"] = "locked" in str(exc)
            finally:
                other.close()

        self.append(guard=guard)
        self.assertTrue(probe["locked"])

    def test_race_never_commits_after_revocation(self):
        """A revocation racing the append ends as committed-before or refused; never a write after it."""
        outcomes = set()
        for i in range(12):
            run = self.emp.run_begin()["id"] if i else self.run
            started = threading.Event()
            revoked_at = {}

            def revoke(run=run):
                started.wait()
                time.sleep(0.002 * (i % 4))
                with Store(self.home) as other:
                    other.run_fail(run, "revoked")
                    revoked_at["seq"] = other.db.execute("SELECT MAX(seq) FROM audit").fetchone()[0]

            t = threading.Thread(target=revoke)
            t.start()

            def guard(_db):
                started.set()
                time.sleep(0.004 * (i % 3))

            before = self.ps.head_seq()
            try:
                self.ps.append([lesson(str(i))], author="employee-x", run=run, fence=self.emp.path, guard=guard)
                outcome = "committed"
            except ProjectError as exc:
                self.assertEqual(exc.code, "run_inactive")
                outcome = "refused"
            t.join()
            outcomes.add(outcome)
            after = self.ps.head_seq()
            self.assertEqual(after - before, 1 if outcome == "committed" else 0)
            # The run's failure always lands after the append decided: the run is failed now either way.
            self.assertEqual(self.emp.run_get(run)["status"], "failed")
        self.assertIn("committed", outcomes)

    def test_refused_when_revoked_while_waiting(self):
        """Revocation that commits first makes the append refuse (guard runs after the check, so revoke first)."""
        self.emp.run_fail(self.run, "revoked")
        run2 = self.emp.run_begin()["id"]
        self.ps.append([lesson()], author="employee-x", run=run2, fence=self.emp.path)
        with self.assertRaises(ProjectError):
            self.ps.append([lesson()], author="employee-x", run=self.run, fence=self.emp.path)
        self.assertEqual(self.ps.head_seq(), 1)


class GrantTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.ps = ProjectStore(Path(self.tmp.name) / "p" / "memory.sqlite3", "synthetics")

    def tearDown(self):
        self.ps.close()
        self.tmp.cleanup()

    def test_grants(self):
        self.ps.append([lesson()], author="a")  # unprovisioned project: grants not yet enforced
        self.ps.grant("writer", "write", by="operator")
        self.ps.grant("reader", "read", by="operator")
        self.ps.append([lesson()], author="writer")
        for who in ("reader", "stranger"):
            with self.assertRaises(ProjectError) as cm:
                self.ps.append([lesson()], author=who)
            self.assertEqual(cm.exception.code, "no_grant")
        self.assertTrue(self.ps.may_read("reader"))
        self.assertFalse(self.ps.may_read("stranger"))
        self.ps.append([lesson()], author="operator:x", operator=True)
        self.assertTrue(self.ps.revoke("writer", by="operator"))
        with self.assertRaises(ProjectError):
            self.ps.append([lesson()], author="writer")
        actions = [r[0] for r in self.ps.db.execute("SELECT action FROM audit ORDER BY seq")]
        self.assertEqual(actions.count("grant"), 2)
        self.assertIn("revoke", actions)
        self.assertEqual(self.ps.head_seq(), 3)


if __name__ == "__main__":
    unittest.main()
