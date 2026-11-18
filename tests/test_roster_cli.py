"""M26 P1 step 6: `synthetics roster` CLI. Design tests 3 (company writes/session rules) and 13 (seed)."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics import cli
from synthetics.roster import CompanyStore
from synthetics.store import Store

ROWAN = "employee-28ed6d90e0fe4b42b64bae0e41e82cba"


def run_cli(home, *argv, env=None):
    buf = io.StringIO()
    environ = {k: v for k, v in os.environ.items() if k != "SYNTHETICS_SESSION_RUN"}
    environ.update(env or {})
    with mock.patch.dict(os.environ, environ, clear=True), contextlib.redirect_stdout(buf):
        code = cli.main(["--home", str(home), *argv])
    text = buf.getvalue()
    return code, (json.loads(text) if text.strip().startswith(("{", "[")) else text)


class RosterCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = self.root / "manager"
        with Store(self.home) as s:
            self.me = s.initialize("Codex", "manager")["id"]
        code, out = run_cli(self.home, "roster", "company", "put", ROWAN, "--name", "Rowan", "--kind", "employee",
                            "--role", "Noteling maintainer", "--by", "operator", "--source", "identity row")
        self.assertEqual(code, 0, out)
        run_cli(self.home, "roster", "company", "put", self.me, "--name", "Codex", "--kind", "employee",
                "--role", "Synthetics manager", "--by", "operator", "--source", "identity row")

    def tearDown(self):
        self.tmp.cleanup()

    def state(self):
        with CompanyStore.at_root(self.root) as co:
            return co.state()

    def test_operator_put_alias_retire_and_render(self):
        code, out = run_cli(self.home, "roster", "company", "alias", ROWAN, "rowan", "--by", "operator",
                            "--source", "M02/live-routes.json")
        self.assertEqual(code, 0, out)
        st = self.state()
        self.assertEqual(st["people"][ROWAN]["role"], "Noteling maintainer")
        self.assertIn("rowan", st["people"][ROWAN]["aliases"])
        self.assertTrue((self.root / "company" / "people").is_dir())  # writes re-render
        code, out = run_cli(self.home, "roster", "company", "unalias", "rowan", "--by", "operator",
                            "--source", "test")
        self.assertEqual(code, 0, out)
        code, out = run_cli(self.home, "roster", "company", "retire", ROWAN, "--reason", "left", "--by",
                            "operator", "--source", "test")
        self.assertEqual(code, 0, out)
        code, out = run_cli(self.home, "roster", "render")
        self.assertEqual(code, 0, out)
        self.assertTrue((self.home / "roster" / "self.md").exists())

    def test_session_needs_grant_is_fenced_and_audited(self):
        with Store(self.home) as s:
            run = s.run_begin()["id"]
        env = {"SYNTHETICS_SESSION_RUN": run, "SYNTHETICS_HOME": str(self.home)}  # M28: a session has both
        code, out = run_cli(self.home, "roster", "company", "put", ROWAN, "--description", "maintains Noteling",
                            "--by", self.me, "--source", "test", env=env)
        self.assertNotEqual(code, 0)  # no company grant yet
        for argv in (("grant", self.me, "--mode", "write", "--by", "x"), ("revoke", self.me, "--by", "x"),
                     ("import", "x.jsonl")):
            code, _ = run_cli(self.home, "roster", "company", *argv, env=env)
            self.assertNotEqual(code, 0, argv)
        self.assertNotEqual(run_cli(self.home, "roster", "show", env=env)[0], 0)
        self.assertNotEqual(run_cli(self.home, "roster", "seed", "--apply", env=env)[0], 0)
        code, out = run_cli(self.home, "roster", "company", "grant", self.me, "--mode", "write", "--by", "operator")
        self.assertEqual(code, 0, out)
        code, out = run_cli(self.home, "roster", "company", "put", ROWAN, "--description", "maintains Noteling",
                            "--by", "operator:x", "--source", "test", env=env)
        self.assertNotEqual(code, 0)  # M28 R4: --by must name the session actor (by_mismatch)
        code, out = run_cli(self.home, "roster", "company", "put", ROWAN, "--description", "maintains Noteling",
                            "--by", self.me, "--source", "test", env=env)
        self.assertEqual(code, 0, out)
        self.assertEqual(self.state()["people"][ROWAN]["description"], "maintains Noteling")
        with Store(self.home) as s:
            self.assertTrue(any(a["action"] == "roster.company.put" for a in s.audit_list(20)))
            # A4: an unauthenticated role change from a session is still refused.
        code, _ = run_cli(self.home, "roster", "company", "put", ROWAN, "--role", "other", "--by", self.me,
                          "--source", "test", env=env)
        self.assertNotEqual(code, 0)
        with Store(self.home) as s:
            s.run_finish(run, "done")
        code, _ = run_cli(self.home, "roster", "company", "put", ROWAN, "--description", "again", "--by", self.me,
                          "--source", "test", env=env)
        self.assertNotEqual(code, 0)  # run fence: finished run can't write

    def test_seed_dry_run_deterministic_and_apply_idempotent(self):
        with Store(self.home) as s:
            s.memory_put(ROWAN, "routes design questions", "via Codex, always", kind="procedure", source="event-1")
            s.memory_put(ROWAN, "may prefer IDs", "seems to like linked IDs", kind="belief", source="event-2")
            legacy = s.memory_put(ROWAN, "unsourced", "no source here", kind="fact", source="x")["id"]
            # Older homes hold memories with an empty source; the API now refuses them, so simulate one.
            s.db.execute("UPDATE memories SET data=json_set(data,'$.source','') WHERE id=?", (legacy,))
            s.db.commit()
            s.memory_put(self.me, "check tests first", "grep tests", kind="procedure", source="run-1")
            s.memory_put(self.me, "a fact about me", "skipped kind", kind="fact", source="run-1")
            s.entity_put("noteling", "Noteling", "project", "a project")
            s.memory_put("noteling", "project fact", "not a person", kind="fact", source="doc")
        _, a = run_cli(self.home, "roster", "seed", "--dry-run")
        _, b = run_cli(self.home, "roster", "seed", "--dry-run")
        self.assertEqual(a, b)
        self.assertEqual(a["counts"], {"views": {ROWAN: 2}, "skipped_self": 2, "skipped_unsourced": 1,
                                       "skipped_unresolved": 1})
        with Store(self.home) as s:
            self.assertEqual(s.roster_entries(), [])  # dry run writes nothing
        code, out = run_cli(self.home, "roster", "seed", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual((out["written"], out["replayed"]), (2, 0))
        _, again = run_cli(self.home, "roster", "seed", "--apply")
        self.assertEqual((again["written"], again["replayed"]), (0, 2))
        with Store(self.home) as s:
            entries = s.roster_entries()
            state = s.roster_state()
        self.assertEqual(len(entries), 2)
        self.assertTrue(all(e["author"] == "roster-seed" for e in entries))
        claims = state["views"][ROWAN]["claims"]
        by_text = {c["text"].split(":")[0]: c for c in claims.values()}
        self.assertEqual(by_text["may prefer IDs"]["certainty"], "inferred")  # never strengthened
        self.assertEqual(by_text["routes design questions"]["kind"], "working")
        self.assertTrue(all("memory-" in c["source"] and "roster-seed" in c["source"] for c in claims.values()))
        view = (self.home / "roster" / "views").iterdir().__next__().read_text()
        self.assertIn("(inferred)", view)
        code, shown = run_cli(self.home, "roster", "show", "Rowan")
        self.assertEqual(code, 0)
        self.assertIn("PRIVATE VIEW", shown["view"])

    def test_r2_seed_proposes_no_self_entries_and_self_memories_appear_once(self):
        # review-P1 R1 / C1: seeding never turns self memories into self notes; each self memory is in the
        # snapshot exactly once (the `self` index or memory_index), even when there are many.
        with Store(self.home) as s:
            mine = [s.memory_put(self.me, "how I work %d" % n, "Procedure %d " % n + "y" * 120,
                                 kind=("procedure", "preference", "fact", "belief")[n % 4], source="run-%d" % n)["id"]
                    for n in range(40)]
            s.memory_put(ROWAN, "routes design questions", "via Codex", kind="procedure", source="event-1")
        code, out = run_cli(self.home, "roster", "seed", "--apply")
        self.assertEqual(code, 0, out)
        self.assertEqual(out["counts"]["skipped_self"], 40)
        self.assertNotIn("self", out["counts"])
        self.assertEqual({p["kind"] for p in out["proposals"]}, {"view"})
        with Store(self.home) as s:
            self.assertEqual([e for e in s.roster_entries() if e["kind"] == "self"], [])
            self.assertEqual(s.roster_state()["self"], {} if isinstance(s.roster_state()["self"], dict) else [])
            ctx = s.context()
        dumped = json.dumps(ctx)
        for mid in mine:
            self.assertEqual(dumped.count(mid), 1, mid)


if __name__ == "__main__":
    unittest.main()
