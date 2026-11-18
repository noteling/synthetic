"""M28 P1a "Warrant": delegated authority (tests T1-T13 of design e34ca0d6 + addendum 067201c2, and D1 of
management/M28/review-P0.md)."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics import authority, capture
from synthetics.cli import main
from synthetics.projects import ProjectStore
from synthetics.roster import CompanyStore
from synthetics.store import Store

KEY = b"f" * 64
GRANT_ALL = ["project.grant", "project.redact", "company.grant", "runtime.request", "delegation.grant"]
SCOPE_ALL = {"project.grant": {"projects": ["synthetics"], "max_mode": "post"},
             "project.redact": {"projects": ["synthetics"]},
             "company.grant": {"max_mode": "read"},
             "runtime.request": {"homes": ["employees"]}}
WORDS = "the team may run deployment and internal development work"


def pids(*values):
    """service_pid stand-in: yields VALUES in turn, then repeats the last one."""
    seq = list(values)
    return lambda label: seq.pop(0) if len(seq) > 1 else seq[0]


class WarrantTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.homes, self.ids = {}, {}
        for name in ("codex", "rowan", "vesper"):
            home = self.root / name
            with Store(home) as s:
                self.ids[name] = s.initialize(name.title(), "employee")["id"]
            self.homes[name] = home
        for name, pid in self.ids.items():
            self.ok("codex", "roster", "company", "put", pid, "--name", name.title(), "--kind", "employee",
                    "--by", "operator", "--source", "test")
        self.ok("codex", "roster", "company", "put", "david", "--name", "David", "--kind", "human",
                "--by", "operator", "--source", "test")
        self.ok("codex", "roster", "company", "grant", self.ids["codex"], "--mode", "write", "--by", "operator")
        capture.write_humans(self.homes["codex"], {"default_human": "david", "senders": ["david"],
                                                   "channel_prefixes": ["fieldpost"],
                                                   "operators": {"senders": ["codex-desktop"],
                                                                 "source_prefixes": []}})
        with Store(self.homes["codex"]) as s:
            s.channel_register("fieldpost", Store.credential_hash(KEY), ["fieldpost"], "test", routes=["david"])
            self.ev_ok = s.feed("david", "Sure: " + WORDS + ".", source="fieldpost", external_id="a",
                                credential=KEY)["id"]
            self.ev_hello = s.feed("david", "hello", source="fieldpost", external_id="b", credential=KEY)["id"]
            self.ev_unauth = s.feed("david", WORDS, source="manual", external_id="c")["id"]
            self.ev_sec = s.feed("codex-desktop", WORDS, source="fieldpost", external_id="d", credential=KEY)["id"]
        self.runs = {}
        for name, home in self.homes.items():
            with Store(home) as s:
                self.runs[name] = s.run_begin()["id"]
        with ProjectStore(self.root / "projects" / "synthetics" / "memory.sqlite3", "synthetics"):
            pass

    def tearDown(self):
        self.tmp.cleanup()

    # ---- helpers ----
    def cli(self, home, *argv, session=None, env_home=None, env_only_run=False):
        environ = {k: v for k, v in os.environ.items() if k not in (authority.SESSION_ENV, authority.HOME_ENV)}
        if session:
            environ[authority.SESSION_ENV] = self.runs[session]
            if not env_only_run:
                environ[authority.HOME_ENV] = str(self.homes[env_home or session])
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, environ, clear=True), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = main(["--home", str(self.homes.get(home, home)), *argv])
        text = out.getvalue()
        return code, (json.loads(text) if text.strip().startswith(("{", "[")) else text), err.getvalue()

    def ok(self, home, *argv, **kw):
        code, out, err = self.cli(home, *argv, **kw)
        self.assertEqual(code, 0, err)
        return out

    def refused(self, code_name, home, *argv, **kw):
        code, out, err = self.cli(home, *argv, **kw)
        self.assertNotEqual(code, 0, out)
        self.assertIn(code_name, err)
        return err

    def root_delegation(self, actions=GRANT_ALL, scope=SCOPE_ALL, grantee=None, source=None, excerpt=WORDS):
        return self.ok("codex", "authority", "delegate", "--grantee", grantee or self.ids["codex"],
                       *sum((["--action", a] for a in actions), []), "--scope", json.dumps(scope),
                       "--source", source or self.ev_ok, "--excerpt", excerpt, "--by", "david-desktop")["ids"][0]

    def audit(self, name, action):
        with Store(self.homes[name]) as s:
            return [a for a in s.audit_list(200) if a["action"] == action]

    def grants(self, project="synthetics"):
        with ProjectStore(self.root / "projects" / project / "memory.sqlite3", project) as ps:
            return {g["employee"]: g["mode"] for g in ps.grants()}

    def pgrant(self, actor, grantee, mode, project="synthetics", home="codex", **kw):
        return self.cli(home, "project", "grant", project, self.ids.get(grantee, grantee), "--mode", mode,
                        "--by", self.ids[actor], session=actor, **kw)

    # ---- T1 actor ----
    def test_t1_actor(self):
        self.assertTrue(authority.actor({})["owner"])
        self.assertTrue(authority.actor({authority.HOME_ENV: str(self.homes["codex"])})["owner"])  # P1a-D1
        with self.assertRaises(authority.AuthorityError) as cm:
            authority.actor({authority.SESSION_ENV: self.runs["codex"]})
        self.assertEqual(cm.exception.code, "actor_unverified")
        env = {authority.SESSION_ENV: self.runs["codex"], authority.HOME_ENV: str(self.homes["codex"])}
        self.assertEqual(authority.actor(env)["id"], self.ids["codex"])
        with self.assertRaises(authority.AuthorityError) as cm:  # T1b: another home with my run
            authority.actor(dict(env, **{authority.HOME_ENV: str(self.homes["rowan"])}))
        self.assertEqual(cm.exception.code, "run_inactive")
        with Store(self.homes["codex"]) as s:
            s.run_finish(self.runs["codex"], "done")
        with self.assertRaises(authority.AuthorityError) as cm:
            authority.actor(env)
        self.assertEqual(cm.exception.code, "run_inactive")

    # ---- T2 root validation, D1 root_requires_owner ----
    def test_t2_root_validation(self):
        base = ["codex", "authority", "delegate", "--grantee", self.ids["codex"], "--action", "project.redact",
                "--scope", json.dumps({"project.redact": {"projects": ["synthetics"]}})]
        self.refused("not_human_source", *base, "--source", self.ev_unauth, "--excerpt", WORDS)
        self.refused("not_human_source", *base, "--source", self.ev_sec, "--excerpt", WORDS)  # R3 secretary
        self.refused("excerpt_mismatch", *base, "--source", self.ev_ok, "--excerpt", "all the money")
        self.refused("bad_scope", "codex", "authority", "delegate", "--grantee", self.ids["codex"],
                     "--action", "roster.show", "--source", self.ev_ok, "--excerpt", WORDS)
        dry = self.ok(*base, "--source", self.ev_ok, "--excerpt", WORDS, "--dry-run")
        self.assertTrue(dry["dry_run"])
        self.assertEqual(authority.covering(self.company(), self.ids["codex"], "project.redact", lambda s: True), None)
        rid = self.ok(*base, "--source", self.ev_ok, "--excerpt", WORDS, "--by", "d")["ids"][0]
        d = self.company()["delegations"][rid]
        self.assertEqual(d["sources"][0]["auth"]["channel"], "fieldpost")  # copied from the event
        self.assertEqual(d["by"], "operator:d")

    def test_d1_session_cannot_mint_root_from_innocuous_event(self):
        self.refused("root_requires_owner", "codex", "authority", "delegate", "--grantee", self.ids["codex"],
                     "--action", "project.grant", "--scope", json.dumps({"project.grant": SCOPE_ALL["project.grant"]}),
                     "--source", self.ev_hello, "--excerpt", "hello", "--by", self.ids["codex"], session="codex")
        self.assertEqual(self.company()["delegations"], {})
        self.assertTrue(any(r["data"].get("code") == "root_requires_owner"
                            for r in self.audit("codex", "authority.refused")))
        # Even straight through the log API a non-operator root is refused.
        with CompanyStore.at_root(self.root) as cs:
            with self.assertRaises(Exception) as cm:
                cs.append([{"kind": "delegation", "body": {"grantee": self.ids["codex"], "actions": ["project.redact"],
                                                            "scope": {"project.redact": {"projects": ["x"]}},
                                                            "sources": [{"event": "e", "excerpt": "x",
                                                                         "auth": {"channel": "forged"}}]}}],
                          author=self.ids["codex"])
        self.assertEqual(cm.exception.code, "root_requires_owner")

    def company(self):
        with CompanyStore.at_root(self.root) as cs:
            return cs.state()

    # ---- T3 project.grant, T1b actor independent of --home, positive case ----
    def test_t3_project_grant(self):
        self.refused_pg("not_delegated", "codex", "rowan", "post")  # no delegation yet
        self.root_delegation()
        self.assertEqual(self.pgrant("codex", "rowan", "post")[0], 0)
        self.assertEqual(self.grants()[self.ids["rowan"]], "post")
        used = self.audit("codex", "authority.used")
        self.assertTrue(used and used[0]["data"]["action"] == "project.grant" and used[0]["data"]["delegation"])
        self.refused_pg("not_delegated", "codex", "rowan", "write")           # above max_mode
        self.refused_pg("not_delegated", "codex", "rowan", "read", project="noteling")  # other project
        self.refused_pg("self_promotion", "codex", "codex", "post")
        self.refused_pg("not_delegated", "rowan", "vesper", "read")
        # T1b: --home names Rowan's home, the actor is still Codex (audit lands in Codex's home, not Rowan's).
        before = len(self.audit("codex", "authority.used"))
        self.assertEqual(self.pgrant("codex", "vesper", "read", home="rowan")[0], 0)
        self.assertEqual(len(self.audit("codex", "authority.used")), before + 1)
        self.assertEqual(self.audit("rowan", "authority.used"), [])

    def _pg(self, actor, grantee, mode, project="synthetics"):
        return ("codex", "project", "grant", project, self.ids[grantee], "--mode", mode, "--by", self.ids[actor])

    # Wrap refused() for the _pg tuples, which need the session kwarg.
    def refused_pg(self, code, actor, grantee, mode, **kw):
        return self.refused(code, *self._pg(actor, grantee, mode, **kw), session=actor)

    # ---- T4 retract; D1 child rules ----
    def test_t4_d1_children_and_retraction(self):
        root = self.root_delegation()
        child_scope = {"project.grant": {"projects": ["synthetics"], "max_mode": "read"}}
        base = ["codex", "authority", "delegate", "--parent", root, "--action", "project.grant"]
        broader = {"project.grant": {"projects": ["synthetics"], "max_mode": "write"}}
        self.refused("exceeds_parent", *base, "--grantee", self.ids["vesper"], "--scope", json.dumps(broader),
                     session="codex")
        self.refused("exceeds_parent", *base, "--action", "delegation.grant", "--grantee", self.ids["vesper"],
                     "--scope", json.dumps(child_scope), session="codex")
        self.refused("self_promotion", *base, "--grantee", self.ids["codex"], "--scope", json.dumps(child_scope),
                     session="codex")
        # A company writer without any delegation cannot write a child.
        self.ok("codex", "roster", "company", "grant", self.ids["rowan"], "--mode", "write", "--by", "operator")
        self.refused("not_delegated", "codex", "authority", "delegate", "--parent", root, "--action", "project.grant",
                     "--grantee", self.ids["vesper"], "--scope", json.dumps(child_scope), session="rowan")
        child = self.ok(*base, "--grantee", self.ids["vesper"], "--scope", json.dumps(child_scope),
                        session="codex")["ids"][0]
        self.assertEqual(self.cli("codex", "project", "grant", "synthetics", self.ids["rowan"], "--mode", "read",
                                  "--by", self.ids["vesper"], session="vesper")[0], 0)
        self.refused("not_delegated", "codex", "project", "grant", "synthetics", self.ids["rowan"], "--mode", "post",
                     "--by", self.ids["vesper"], session="vesper")
        self.refused("no_grant", "codex", "authority", "retract", child, "--reason", "x", session="vesper")
        self.ok("codex", "roster", "company", "grant", self.ids["vesper"], "--mode", "write", "--by", "operator")
        self.refused("not_author", "codex", "authority", "retract", child, "--reason", "x", session="vesper")
        self.refused("not_author", "codex", "authority", "retract", root, "--reason", "x", session="codex")
        # Owner retracts the root: the child dies with it.
        self.ok("codex", "authority", "retract", root, "--reason", "test")
        self.assertEqual(self.company()["delegations"], {})
        self.refused("not_delegated", "codex", "project", "grant", "synthetics", self.ids["rowan"], "--mode", "read",
                     "--by", self.ids["vesper"], session="vesper")
        self.refused("not_delegated", *self._pg("codex", "rowan", "read"), session="codex")

    # ---- T5 runtime.request ----
    def test_t5_runtime_request(self):
        req = ["runtime", "request", "--set", "timeout=300", "--reason", "deploy"]
        self.refused("own_home", "codex", *req, "--by", self.ids["codex"], session="codex")
        self.refused("not_delegated", "rowan", *req, "--by", self.ids["codex"], session="codex")
        self.root_delegation()
        self.refused("by_mismatch", "rowan", *req, "--by", "codex-desktop", session="codex")
        # Authority passes; the temp home has no runtime config, so the store explains that instead.
        err = self.refused("runtime-config", "rowan", *req, "--by", self.ids["codex"], session="codex")
        self.assertNotIn("not_delegated", err)
        self.assertTrue(self.audit("codex", "authority.used"))  # the actor home records the allow row
        # D4: the target home gets a receipt only inside a write that happened; this one failed before writing.
        self.assertEqual(self.audit("rowan", "authority.receipt"), [])
        with Store(self.homes["rowan"]) as s:
            with self.assertRaises(ValueError):
                s.runtime_request({"timeout": 300}, "r", self.ids["codex"], origin="delegated")  # needs delegation

    # ---- T6 holes, T6b forged --by, T6c self-grant, T12 operator author, T13 ----
    def test_t6_owner_only_holes_from_a_session(self):
        self.root_delegation()
        for home in ("codex", "rowan"):
            self.refused("owner_only", home, "access", "grant", str(self.root), "--mode", "write", "--purpose", "x",
                         "--by", "david", session="codex")
        with Store(self.homes["codex"]) as s:
            self.assertEqual(s.access_list(), [])
        self.refused("owner_only", "codex", "access", "revoke", "access-x", "--by", "david", session="codex")
        self.refused("owner_only", "codex", "limit", "raise", "100", "--date", "2099-01-01", "--by", "david",
                     session="codex")
        with Store(self.homes["codex"]) as s:
            wid = s.work_put("x")["id"]
        self.refused("owner_only", "codex", "work", "update", wid, "--status", "done", "--override", "trust me",
                     session="codex")
        self.refused("owner_only", "codex", "handoff-codex", "x", "--snapshot", str(self.root / "s.json"),
                     session="codex")
        self.refused("owner_only", str(self.root / "newhome"), "init", "--name", "N", "--role", "r", "--human", "me",
                     session="codex")
        self.assertFalse((self.root / "newhome").exists())
        self.refused("owner_only", "codex", "project", "import", "synthetics", str(self.root / "x.jsonl"),
                     session="codex")
        self.refused("by_mismatch", "codex", "roster", "company", "put", self.ids["rowan"], "--description", "d",
                     "--by", "operator:x", "--source", "test", session="codex")
        self.refused("self_promotion", "codex", "roster", "company", "grant", self.ids["codex"], "--mode", "read",
                     "--by", self.ids["codex"], session="codex")
        self.assertTrue(self.audit("codex", "authority.refused"))
        # T12: a session company write is authored by the actor, never operator:* nor the --home target.
        self.ok("rowan", "roster", "company", "put", self.ids["vesper"], "--description", "builds tools",
                "--by", self.ids["codex"], "--source", "test", session="codex")
        with CompanyStore.at_root(self.root) as cs:
            last = cs.entries()[-1]
        self.assertEqual(last["author"], self.ids["codex"])
        # The owner path still works and is labelled operator:<by>.
        self.ok("codex", "access", "grant", str(self.root), "--mode", "read", "--purpose", "x", "--by", "david")

    def test_t8_t13_credential_boundary_with_every_delegation(self):
        self.root_delegation()
        key = self.root / "k.key"
        key.write_text("a" * 64 + "\n")
        for argv in (("channel", "provision", "x", "--source-prefix", "x:", "--home", str(self.homes["codex"]),
                      "--key-dir", str(self.root / "keys")),
                     ("channel", "register", "x", "--source-prefix", "x:", "--key-file", str(key)),
                     ("feed", "hi", "--from", "david", "--credential-file", str(key)),
                     ("question-routed", "question-x", "--to-id", "david", "--by", "me")):
            code, _, err = self.cli("codex", *argv, session="codex")
            self.assertNotEqual(code, 0, argv)
            self.assertRegex(err, "refused inside an employee session|credential_boundary", argv)
        self.assertFalse((self.root / "keys").exists())

    # ---- T11 access grants are never authority ----
    def test_t11_access_grants_never_authority(self):
        self.ok("rowan", "access", "grant", str(self.root), "--mode", "write", "--purpose", "all", "--by", "david")
        self.refused("not_delegated", *self._pg("rowan", "vesper", "read"), session="rowan")
        self.refused("not_delegated", "codex", "authority", "check", "runtime.request",
                     "--target-home", str(self.homes["vesper"]), session="rowan")

    def test_check_command_and_static_ceiling(self):
        self.root_delegation()
        out = self.ok("codex", "authority", "check", "project.grant", "--project", "synthetics", "--mode", "post",
                      session="codex")
        self.assertTrue(out["allowed"] and out["delegation"])
        self.refused("not_delegated", "codex", "authority", "check", "project.grant", "--project", "synthetics",
                     "--mode", "write", session="codex")
        # D1 ceiling: owner-only and credential items never enter the catalogue; nothing names views.
        # (adapter.install left this list in P1c-b: D13 delegates reviewed Synthetics-owned adapters.)
        forbidden = {"project.import", "company.import", "company.redact", "access.grant", "access.revoke",
                     "work.override", "history.import", "init", "channel.provision",
                     "channel.register", "question.routed", "auth.backfill", "postern.run", "roster.show",
                     "roster.seed", "view.read"}
        self.assertFalse(set(authority.CATALOGUE) & forbidden)
        for action in authority.CATALOGUE:
            self.assertNotIn("view", action)
            self.assertNotIn("roster", action)
        shown = self.ok("codex", "authority", "show")
        self.assertEqual(len(shown), 1)
        self.assertIn("delegation", (self.root / "company" / "people" / (self.ids["codex"] + ".md")).read_text())

    # ---- D4 transactional re-check ----
    def _warrant(self, action="project.grant", check=lambda sc: True):
        env = {authority.SESSION_ENV: self.runs["codex"], authority.HOME_ENV: str(self.homes["codex"])}
        return authority.require(action, home=self.homes["codex"], check=check, env=env)["warrant"]

    def _project(self):
        return ProjectStore(self.root / "projects" / "synthetics" / "memory.sqlite3", "synthetics")

    def test_d4_run_finished_between_check_and_write(self):
        self.root_delegation()
        w = self._warrant()
        with Store(self.homes["codex"]) as s:
            s.run_finish(self.runs["codex"], "done")
        with self._project() as ps:
            with self.assertRaises(authority.AuthorityError) as cm:
                authority.fenced(w, lambda: ps.grant(self.ids["rowan"], "post", by=self.ids["codex"], warrant=w))
        self.assertEqual(cm.exception.code, "run_inactive")
        self.assertNotIn(self.ids["rowan"], self.grants())
        refused = [r for r in self.audit("codex", "authority.refused") if r["data"].get("stage") == "write"]
        self.assertEqual(refused[-1]["data"]["code"], "run_inactive")

    def test_d4_retracted_between_check_and_write_and_receipt(self):
        root = self.root_delegation()
        w = self._warrant()
        with self._project() as ps:  # positive: the receipt lands in the target DB in the same transaction
            authority.fenced(w, lambda: ps.grant(self.ids["vesper"], "read", by=self.ids["codex"], warrant=w))
            rows = [json.loads(d) for (d,) in ps.db.execute("SELECT detail FROM audit WHERE action='warrant'")]
        self.assertEqual(rows[-1]["actor"], self.ids["codex"])
        self.assertEqual(rows[-1]["run"], self.runs["codex"])
        self.assertEqual(rows[-1]["delegation"], root)
        self.assertEqual(rows[-1]["action"], "project.grant")
        self.ok("codex", "authority", "retract", root, "--reason", "test")
        for fn in (lambda ps: ps.grant(self.ids["rowan"], "post", by=self.ids["codex"], warrant=w),
                   lambda ps: ps.revoke(self.ids["vesper"], by=self.ids["codex"], warrant=w)):
            with self._project() as ps:
                with self.assertRaises(authority.AuthorityError) as cm:
                    authority.fenced(w, lambda: fn(ps))
            self.assertEqual(cm.exception.code, "delegation_retracted")
        self.assertEqual(self.grants(), {self.ids["vesper"]: "read"})
        codes = [r["data"]["code"] for r in self.audit("codex", "authority.refused") if r["data"].get("stage")]
        self.assertEqual(codes, ["delegation_retracted", "delegation_retracted"])

    def test_d4_company_grant_rechecks_in_company_db(self):
        root = self.root_delegation()
        w = self._warrant("company.grant")
        self.ok("codex", "authority", "retract", root, "--reason", "test")
        with CompanyStore.at_root(self.root) as cs:
            with self.assertRaises(authority.AuthorityError):
                cs.grant(self.ids["rowan"], "read", by=self.ids["codex"], warrant=w)
            self.assertNotIn(self.ids["rowan"], {g["employee"] for g in cs.grants()})

    def test_d4_runtime_request_receipt_and_recheck(self):
        self.root_delegation()
        w = self._warrant("runtime.request")
        state = {"policy": {"allowed_models": None, "allowed_claude": None, "baseline": {"budget": 1}},
                 "effective": {"timeout": 600.0}}
        with mock.patch("synthetics.runtime.read_state", return_value=state), \
                mock.patch("synthetics.runtime.validate", side_effect=lambda c, *a: c), \
                mock.patch("synthetics.runtime.merge", side_effect=lambda e, s: dict(e, **s)), \
                mock.patch("synthetics.runtime.digest", return_value="d"):
            with Store(self.homes["rowan"]) as s:
                rec = s.runtime_request({"timeout": 300.0}, "deploy", self.ids["codex"], key="k1",
                                        origin="delegated", delegation=w["delegation"], warrant=w)
                self.assertEqual(rec["delegation"], w["delegation"])
            got = self.audit("rowan", "authority.receipt")
            self.assertEqual(got[0]["data"]["key"], "k1")
            self.assertEqual(got[0]["data"]["actor"], self.ids["codex"])
            with Store(self.homes["codex"]) as s:
                s.run_finish(self.runs["codex"], "done")
            with Store(self.homes["rowan"]) as s:
                with self.assertRaises(authority.AuthorityError):
                    authority.fenced(w, lambda: s.runtime_request({"timeout": 200.0}, "again", self.ids["codex"],
                                                                  key="k2", origin="delegated",
                                                                  delegation=w["delegation"], warrant=w))
                self.assertEqual(len(s.runtime_requests()), 1)

    # ---- D5 bootstrap sources ----
    def test_d5_bootstrap_source_must_be_authenticated_context_refs_never_count(self):
        args = ["codex", "authority", "delegate", "--grantee", self.ids["codex"], "--action", "project.redact",
                "--scope", json.dumps({"project.redact": {"projects": ["synthetics"]}})]
        err = self.refused("not_human_source", *args, "--source", self.ev_unauth, "--excerpt", WORDS,
                           "--context-ref", self.ev_ok)
        self.assertIn("auth None", err)
        rid = self.ok(*args, "--source", self.ev_ok, "--excerpt", WORDS, "--context-ref", self.ev_unauth,
                      "--context-ref", "event-1f3412ea")["ids"][0]
        d = self.company()["delegations"][rid]
        self.assertEqual(d["context_refs"], [self.ev_unauth, "event-1f3412ea"])
        self.assertEqual([x["event"] for x in d["sources"]], [self.ev_ok])
        self.refused("bad_entry", "codex", "authority", "delegate", "--parent", rid, "--grantee", self.ids["vesper"],
                     "--action", "project.redact", "--scope", json.dumps({"project.redact": {"projects": ["synthetics"]}}),
                     "--context-ref", self.ev_ok, session="codex")

    # ---- D6 ----
    def test_d6a_runtime_request_never_sets_daily_limit(self):
        self.root_delegation()
        for value in ("0", "151", "100"):
            self.refused("use_limit_raise", "rowan", "runtime", "request", "--set", "daily_limit=" + value,
                         "--reason", "x", "--by", self.ids["codex"], session="codex")
        self.refused("use_limit_raise", "rowan", "runtime", "request", "--set", "daily_limit=120",
                     "--set", "timeout=300", "--reason", "persistent", "--by", self.ids["codex"], session="codex")
        with Store(self.homes["rowan"]) as s:
            self.assertEqual(s.runtime_requests(), [])

    def test_d6b_employees_means_member_homes_of_my_company(self):
        self.root_delegation()
        other = Path(tempfile.mkdtemp(dir=self.tmp.name)) / "elsewhere" / "stranger"
        with Store(other) as s:  # another company's home, with an identity
            s.initialize("Stranger", "employee")
        stray = Path(tempfile.mkdtemp())  # a stray temp home with an identity
        self.addCleanup(lambda: __import__("shutil").rmtree(stray, ignore_errors=True))
        with Store(stray / "h") as s:
            s.initialize("Stray", "employee")
        link = self.root / "linked"
        link.symlink_to(other, target_is_directory=True)  # symlink inside the root pointing outside it
        actor_home = self.homes["codex"]
        self.assertTrue(authority.homes_cover(["employees"], self.homes["rowan"], actor_home))
        for target in (other, stray / "h", link, self.root / "company"):
            self.assertFalse(authority.homes_cover(["employees"], target, actor_home), target)
            self.assertFalse(authority.homes_cover(["*"], target, actor_home), target)
        self.refused("not_delegated", "codex", "authority", "check", "runtime.request",
                     "--target-home", str(link), session="codex")
        self.ok("codex", "authority", "check", "runtime.request", "--target-home", str(self.homes["rowan"]),
                session="codex")

    def test_d6c_semantic_attenuation(self):
        parent = {"actions": ["runtime.request", "project.grant"],
                  "scope": {"runtime.request": {"homes": ["employees"]},
                            "project.grant": {"projects": ["*"], "max_mode": "post"}}}
        member = {"runtime.request": {"homes": [self.ids["rowan"]]}}
        self.assertIsNone(authority.child_within(["runtime.request"], member, parent))
        named = {"actions": ["runtime.request"], "scope": {"runtime.request": {"homes": [self.ids["rowan"]]}}}
        self.assertIn("exceeds", authority.child_within(["runtime.request"],
                                                        {"runtime.request": {"homes": ["employees"]}}, named))
        for child in ({"runtime.request": {"homes": ["*"]}},):
            self.assertIsNotNone(authority.child_within(["runtime.request"], child, parent))
        self.assertIsNotNone(authority.child_within(
            ["project.grant"], {"project.grant": {"projects": ["*"], "max_mode": "read"}}, parent))
        self.assertIsNone(authority.child_within(
            ["project.grant"], {"project.grant": {"projects": ["synthetics"], "max_mode": "read"}}, parent))

    # ---- D8a read-only company commands have no --by ----
    def test_d8a_company_read_commands_owner_and_session(self):
        for cmd in ("grants", "state", "export"):
            for session in (None, "codex", "vesper"):
                code, out, err = self.cli("codex", "roster", "company", cmd, session=session)
                self.assertEqual(code, 0, (cmd, session, err))
                self.assertNotIn("AttributeError", err)

    # ---- D8b member = registered, non-retired employee of the actor's company ----
    def test_d8b_member_must_be_registered_active_employee(self):
        actor_home = self.homes["codex"]
        self.assertTrue(authority.member_home(self.homes["rowan"], actor_home))
        fresh = self.root / "fresh"  # same root, identity, never registered in the company
        with Store(fresh) as s:
            s.initialize("Fresh", "employee")
        fake = self.root / "fake"  # same root, identity row naming an unknown ID
        with Store(fake) as s:
            s.initialize("Fake", "employee")
        with Store(fake) as s:
            s.db.execute("UPDATE identity SET data=json_set(data, '$.id', 'employee-unknown')")
            s.db.commit()
        human = self.root / "human"  # registered, but kind human
        with Store(human) as s:
            hid = s.initialize("Human", "employee")["id"]
        self.ok("codex", "roster", "company", "put", hid, "--name", "Human", "--kind", "human",
                "--by", "operator", "--source", "test")
        self.ok("codex", "roster", "company", "retire", self.ids["vesper"], "--reason", "test",
                "--by", "operator", "--source", "test")
        outside = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(outside, ignore_errors=True))
        link = outside / "rowan-link"  # outside symlink to a registered home
        link.symlink_to(self.homes["rowan"], target_is_directory=True)
        out_home = outside / "x" / "rowan"  # outside-root home with the same name
        with Store(out_home) as s:
            s.initialize("Rowan", "employee")
        for target in (fresh, fake, human, self.homes["vesper"], out_home):
            self.assertFalse(authority.member_home(target, actor_home), target)
            self.assertFalse(authority.homes_cover(["employees"], target, actor_home), target)
            self.assertFalse(authority.homes_cover(["*"], target, actor_home), target)
        # a symlink outside the root resolves to the registered home: containment is by resolved path, so it is
        # the same member, never a different home
        self.assertEqual(link.resolve(), self.homes["rowan"].resolve())
        self.root_delegation()
        for target in (fresh, fake, self.homes["vesper"], out_home):
            self.refused("not_delegated", "codex", "authority", "check", "runtime.request",
                         "--target-home", str(target), session="codex")

    # ---- P1b D9 worker.manage ----
    def _worker_setup(self, ops=("install", "start", "stop", "restart", "remove")):
        self.root_delegation(actions=["worker.manage"],
                             scope={"worker.manage": {"homes": ["employees"], "ops": list(ops)}})
        self.claude = self.root / "bin" / "claude"
        self.claude.parent.mkdir()
        self.claude.write_text("#!/bin/sh\n")
        self.claude.chmod(0o755)
        (self.homes["rowan"] / "runtime.state.json").write_text(json.dumps(
            {"policy": {"allowed_models": ["claude-opus-5-5"], "allowed_claude": [str(self.claude)]}}))
        with Store(self.homes["rowan"]) as s:
            s.run_finish(self.runs["rowan"], "done")
        self.agents = self.root / "agents"

    def winstall(self, *extra, home="rowan"):
        return self.cli(home, "worker", "install", "--claude", str(self.claude), "--launch-agents-dir",
                        str(self.agents), "--no-start", *extra, session="codex")

    def test_d9b_delegated_install_standard_plist_only(self):
        import plistlib
        from synthetics import install
        self._worker_setup()
        for code, extra in (("foreign_executable", ("--claude", "/bin/sh")),
                            ("budget_exceeds", ("--budget", "101")),
                            ("limit_exceeds", ("--daily-limit", "151")),
                            ("limit_exceeds", ("--daily-limit", "0")),
                            ("label_not_standard", ("--label", "com.synthetics.worker.other")),
                            ("model_not_allowed", ("--model", "claude-other")),
                            ("allow_list_widened", ("--allow-model", "claude-other")),
                            ("limit_allow_owner_only", ("--limit-allow", "ana")),
                            ("agents_dir", ("--launch-agents-dir", "/private/var/empty/agents"))):
            c, out, err = self.winstall(*extra)
            self.assertNotEqual(c, 0, (extra, out))
            self.assertIn(code, err, extra)
        self.refused("own_home", "codex", "worker", "install", "--claude", str(self.claude),
                     "--launch-agents-dir", str(self.agents), "--no-start", session="codex")
        self.refused("no_policy", "vesper", "worker", "install", "--claude", str(self.claude),
                     "--launch-agents-dir", str(self.agents), "--no-start", session="codex")
        self.assertFalse(self.agents.exists() and any(self.agents.iterdir()))
        refused = [a["data"]["code"] for a in self.audit("codex", "authority.refused")]
        self.assertIn("foreign_executable", refused)
        self.assertIn("budget_exceeds", refused)
        out = self.ok("rowan", "worker", "install", "--claude", str(self.claude), "--launch-agents-dir",
                      str(self.agents), "--no-start", "--budget", "100", "--daily-limit", "150", session="codex")
        self.assertEqual(out["outcome"], "confirmed")
        label = install.worker_label({"id": self.ids["rowan"]})
        self.assertEqual(out["label"], label)
        with open(self.agents / (label + ".plist"), "rb") as handle:
            argv = plistlib.load(handle)["ProgramArguments"]
        home = str(self.homes["rowan"].resolve())
        self.assertEqual(argv[1:6], ["-u", "-m", "synthetics", "--home", home])
        self.assertEqual(argv[6], "serve")
        self.assertIn(str(self.claude), argv)
        rows = self.audit("rowan", "authority.worker")
        self.assertEqual(sorted(r["data"]["stage"] for r in rows), ["outcome", "requested"])
        self.assertTrue(all(r["data"]["run"] == self.runs["codex"] for r in rows))

    def test_d9a_ops_are_worker_subcommands(self):
        from synthetics import install
        self.refused("bad_scope", "codex", "authority", "delegate", "--grantee", self.ids["codex"],
                     "--action", "worker.manage", "--scope",
                     json.dumps({"worker.manage": {"homes": ["employees"], "ops": ["bootout"]}}),
                     "--source", self.ev_ok, "--excerpt", WORDS, "--by", "david-desktop")
        self._worker_setup(ops=["kickstart"])
        self.assertTrue(install.op_covered("restart", ["kickstart"]))
        self.assertFalse(install.op_covered("kickstart", ["*"]))
        with mock.patch("synthetics.install._control", return_value={"restart_requested": True}) as ctl, \
                mock.patch("synthetics.install.service_pid", side_effect=pids(100, 200)):
            out = self.ok("rowan", "worker", "restart", session="codex")
            self.refused("not_delegated", "rowan", "worker", "stop", session="codex")
            self.refused("not_delegated", "rowan", "worker", "install", session="codex")
        self.assertEqual(out["outcome"], "confirmed")
        self.assertEqual(ctl.call_count, 1)
        self.refused("label_not_standard", "rowan", "worker", "restart", "--label", "com.synthetics.worker.x",
                     session="codex")

    def test_d9c_idle_gate_and_outcomes(self):
        self._worker_setup()
        with Store(self.homes["rowan"]) as s:
            busy = s.run_begin()["id"]
        with mock.patch("synthetics.install._control") as ctl:
            for op in ("stop", "remove"):
                self.refused("not_idle", "rowan", "worker", op, session="codex")
            self.assertEqual(ctl.call_count, 0)
        with Store(self.homes["rowan"]) as s:
            s.run_finish(busy, "done")
        with mock.patch("synthetics.install._control", return_value={"stopped": True}), \
                mock.patch("synthetics.install.loaded", return_value=False):
            self.assertEqual(self.ok("rowan", "worker", "stop", session="codex")["outcome"], "confirmed")
        with mock.patch("synthetics.install._control", return_value={"start_requested": True}), \
                mock.patch("synthetics.install.service_pid", return_value=False):
            self.assertEqual(self.ok("rowan", "worker", "start", session="codex")["outcome"], "uncertain")
        with mock.patch("synthetics.install._control", side_effect=RuntimeError("launchctl said no")), \
                mock.patch("synthetics.install.service_pid", return_value=None):
            out = self.ok("rowan", "worker", "restart", session="codex")  # D16c: raised after the effect began
            self.assertEqual(out["outcome"], "uncertain")
        outcomes = [r["data"].get("outcome") for r in self.audit("rowan", "authority.worker")
                    if r["data"]["stage"] == "outcome"]
        self.assertEqual(sorted(outcomes), ["confirmed", "uncertain", "uncertain"])
        # remove of a written, never-loaded plist: real removal, confirmed by read-back
        self.ok("rowan", "worker", "install", "--claude", str(self.claude), "--launch-agents-dir",
                str(self.agents), "--no-start", session="codex")
        out = self.ok("rowan", "worker", "remove", "--never-loaded", "--launch-agents-dir", str(self.agents),
                      session="codex")
        self.assertEqual(out["outcome"], "confirmed")
        self.assertEqual(list(self.agents.iterdir()), [])
        # D4: a run that ended before the write executes nothing
        with Store(self.homes["codex"]) as s:
            s.run_finish(self.runs["codex"], "done")
        self.refused("run_inactive", "rowan", "worker", "stop", session="codex")


    def test_p1b_adapter_manage_existing_label_unchanged_args(self):
        """D2: start/restart/reinstall an existing adapter label with unchanged ProgramArguments; anything new
        (label, executable, arguments) and stop/remove stay owner-only."""
        import plistlib
        label, other = "com.synthetics.adapter.postern", "com.synthetics.adapter.other"
        agents = self.root / "agents"
        argv = ["/usr/bin/true", "--x"]
        self.ok("codex", "adapter", "install", "--label", label, "--launch-agents-dir", str(agents), "--no-start",
                "--", *argv)  # owner
        self.refused("not_delegated", "codex", "adapter", "restart", "--label", label, session="codex")
        self.root_delegation(actions=["adapter.manage"], scope={"adapter.manage": {"labels": [label]}})
        A = ("--launch-agents-dir", str(agents))
        for code, cmd in (("argv_changed", ("install", "--label", label, *A, "--no-start", "--", "/bin/sh", "-c", "x")),
                          ("argv_changed", ("install", "--label", label, *A, "--no-start", "--keep-alive", "never",
                                            "--", *argv)),
                          ("not_delegated", ("restart", "--label", other)),
                          ("owner_only", ("stop", "--label", label)),
                          ("owner_only", ("remove", "--label", label, *A))):
            c, out, err = self.cli("codex", "adapter", *cmd, session="codex")
            self.assertNotEqual(c, 0, (cmd, out))
            self.assertIn(code, err, cmd)
        with mock.patch("synthetics.install._installed_plist", return_value=(agents / "x.plist", None)):
            self.refused("unknown_label", "codex", "adapter", "start", "--label", label, session="codex")
        with open(agents / (label + ".plist"), "rb") as handle:
            self.assertEqual(plistlib.load(handle)["ProgramArguments"], argv)
        with mock.patch("synthetics.install._control", return_value={}) as ctl, \
                mock.patch("synthetics.install.READY_WAIT_SECONDS", 0), \
                mock.patch("synthetics.install.service_pid", side_effect=pids(100, 200)):
            out = self.ok("codex", "adapter", "restart", "--label", label, *A, session="codex")
        self.assertEqual((out["outcome"], out["pid"], ctl.call_count), ("confirmed", 200, 1))
        with mock.patch("synthetics.install._control", return_value={}), \
                mock.patch("synthetics.install.READY_WAIT_SECONDS", 0), \
                mock.patch("synthetics.install.service_pid", side_effect=pids(100, 100)):
            self.assertEqual(self.ok("codex", "adapter", "start", "--label", label, *A, session="codex")["outcome"],
                             "uncertain")
        out = self.ok("codex", "adapter", "install", "--label", label, *A, "--no-start", "--", *argv, session="codex")
        self.assertEqual(out["outcome"], "confirmed")
        rows = [r["data"] for r in self.audit("codex", "authority.adapter")]
        self.assertEqual([r["outcome"] for r in rows if r["stage"] == "outcome"], ["confirmed", "uncertain", "confirmed"])
        self.assertTrue(all(r["run"] == self.runs["codex"] for r in rows))
        refused = [a["data"]["code"] for a in self.audit("codex", "authority.refused")]
        self.assertIn("argv_changed", refused)
        with Store(self.homes["codex"]) as s:
            s.run_finish(self.runs["codex"], "done")
        with mock.patch("synthetics.install._control", return_value={}) as ctl:
            self.refused("run_inactive", "codex", "adapter", "restart", "--label", label, *A, session="codex")
        self.assertEqual(ctl.call_count, 0)

    def test_t10_kit_run_path_scoped(self):
        """T10: a kit guard passes for a delegate on a kit file under a scoped directory and refuses otherwise;
        a child narrows by path containment."""
        mgmt = self.root / "management"
        (mgmt / "M28").mkdir(parents=True)
        kit = mgmt / "M28" / "kit.sh"
        kit.write_text("#!/bin/sh\n")
        outside = self.root / "elsewhere.sh"
        outside.write_text("#!/bin/sh\n")
        self.refused("not_delegated", "codex", "authority", "check", "kit.run", "--item", str(kit), session="codex")
        self.refused("bad_scope", "codex", "authority", "delegate", "--grantee", self.ids["codex"],
                     "--action", "kit.run", "--scope", json.dumps({"kit.run": {"kits": ["management"]}}),
                     "--source", self.ev_ok, "--excerpt", WORDS, "--by", "david-desktop")
        root = self.root_delegation(actions=["kit.run", "delegation.grant"],
                                    scope={"kit.run": {"kits": [str(mgmt)]}})
        out = self.ok("codex", "authority", "check", "kit.run", "--item", str(kit), session="codex")
        self.assertEqual(out["delegation"], root)
        self.refused("not_delegated", "codex", "authority", "check", "kit.run", "--item", str(outside), session="codex")
        sneaky = mgmt / ".." / "elsewhere.sh"
        self.refused("not_delegated", "codex", "authority", "check", "kit.run", "--item", str(sneaky), session="codex")
        self.refused("bad_item", "codex", "authority", "check", "kit.run", session="codex")
        self.refused("bad_item", "codex", "authority", "check", "kit.run", "--item", str(mgmt / "nope.sh"),
                     session="codex")
        child = lambda kits: self.cli("codex", "authority", "delegate", "--grantee", self.ids["rowan"],
                                      "--action", "kit.run", "--scope", json.dumps({"kit.run": {"kits": kits}}),
                                      "--parent", root, "--by", self.ids["codex"], session="codex")
        c, out, err = child([str(self.root)])
        self.assertNotEqual(c, 0)
        self.assertIn("exceeds_parent", err)
        c, out, err = child([str(mgmt / "M28")])
        self.assertEqual(c, 0, err)
        self.ok("rowan", "authority", "check", "kit.run", "--item", str(kit), session="rowan")

    def test_d10b_start_restart_need_new_live_pid(self):
        self._worker_setup()
        cases = [((100, 100), "uncertain"), ((100, None), "uncertain"), ((False, 300), "uncertain"),
                 ((None, 300), "confirmed"), ((100, 300), "confirmed")]
        for op in ("start", "restart"):
            for seq, want in cases:
                with mock.patch("synthetics.install._control", return_value={}), \
                        mock.patch("synthetics.install.loaded", return_value=True), \
                        mock.patch("synthetics.install.READY_WAIT_SECONDS", 0), \
                        mock.patch("synthetics.install.service_pid", side_effect=pids(*seq)):
                    out = self.ok("rowan", "worker", op, session="codex")
                self.assertEqual(out["outcome"], want, (op, seq))
        rows = [r["data"] for r in self.audit("rowan", "authority.worker") if r["data"]["stage"] == "outcome"]
        self.assertEqual(sorted(str(r.get("pid")) for r in rows).count("300"), 4)

    def test_d14_worker_ledger_failures(self):
        """D14a/b/c (worker): a request-write failure executes nothing; an outcome-write failure after the effect is
        uncertain + pending, a repeat call is refused (pending_op) without executing, and settle reconciles."""
        self._worker_setup()
        with mock.patch("synthetics.store.Store.launch_op_begin", side_effect=OSError("disk full")), \
                mock.patch("synthetics.install._control", return_value={}) as ctl:
            self.refused("ledger_unavailable", "rowan", "worker", "restart", session="codex")
        self.assertEqual(ctl.call_count, 0)
        with mock.patch("synthetics.store.Store.launch_op_settle", side_effect=OSError("disk full")), \
                mock.patch("synthetics.install._control", return_value={}) as ctl, \
                mock.patch("synthetics.install.loaded", return_value=True), \
                mock.patch("synthetics.install.READY_WAIT_SECONDS", 0), \
                mock.patch("synthetics.install.service_pid", side_effect=pids(100, 300)):
            out = self.ok("rowan", "worker", "restart", session="codex")
        self.assertEqual((out["outcome"], ctl.call_count), ("uncertain", 1))
        op_id = out["pending_op"]
        pending = self.ok("rowan", "authority", "ops")
        self.assertEqual([p["id"] for p in pending], [op_id])
        with mock.patch("synthetics.install._control", return_value={}) as ctl:
            self.refused("pending_op", "rowan", "worker", "restart", session="codex")
        self.assertEqual(ctl.call_count, 0)
        with mock.patch("synthetics.install.service_pid", return_value=300):
            settled = self.ok("rowan", "authority", "settle", op_id, session="codex")
        self.assertEqual((settled["outcome"], settled["status"], settled["reconciled"]), ("confirmed", "settled", True))
        self.assertEqual(self.ok("rowan", "authority", "ops"), [])
        self.refused("already_settled", "rowan", "authority", "settle", op_id)
        with mock.patch("synthetics.install._control", return_value={}), \
                mock.patch("synthetics.install.loaded", return_value=True), \
                mock.patch("synthetics.install.READY_WAIT_SECONDS", 0), \
                mock.patch("synthetics.install.service_pid", side_effect=pids(300, 400)):
            out = self.ok("rowan", "worker", "restart", session="codex")
        self.assertEqual(out["outcome"], "confirmed")
        self.assertTrue(out["op_id"].startswith("op-"))

    def test_d14_adapter_ledger_failures(self):
        """D14c (adapter): ledger in the actor home; same failure-injection guarantees."""
        label = "com.synthetics.adapter.postern"
        agents = self.root / "agents"
        A = ("--launch-agents-dir", str(agents))
        self.ok("codex", "adapter", "install", "--label", label, *A, "--no-start", "--", "/usr/bin/true")
        self.root_delegation(actions=["adapter.manage"], scope={"adapter.manage": {"labels": [label]}})
        with mock.patch("synthetics.store.Store.launch_op_begin", side_effect=OSError("disk full")), \
                mock.patch("synthetics.install._control", return_value={}) as ctl:
            self.refused("ledger_unavailable", "codex", "adapter", "restart", "--label", label, *A, session="codex")
        self.assertEqual(ctl.call_count, 0)
        with mock.patch("synthetics.store.Store.launch_op_settle", side_effect=OSError("disk full")), \
                mock.patch("synthetics.install._control", return_value={}) as ctl, \
                mock.patch("synthetics.install.READY_WAIT_SECONDS", 0), \
                mock.patch("synthetics.install.service_pid", side_effect=pids(100, 200)):
            out = self.ok("codex", "adapter", "restart", "--label", label, *A, session="codex")
        self.assertEqual((out["outcome"], ctl.call_count), ("uncertain", 1))
        with mock.patch("synthetics.install._control", return_value={}) as ctl:
            self.refused("pending_op", "codex", "adapter", "restart", "--label", label, *A, session="codex")
        self.assertEqual(ctl.call_count, 0)
        with mock.patch("synthetics.install.service_pid", return_value=100):  # no new PID: uncertain, but settled
            settled = self.ok("codex", "authority", "settle", out["pending_op"], session="codex")
        self.assertEqual(settled["outcome"], "uncertain")
        self.assertEqual(self.ok("codex", "authority", "ops"), [])

    # ---- D16 (P1c-a-r3) ----
    def _pending_op(self, name="vesper", **row):
        with Store(self.homes[name]) as s:
            return s.launch_op_begin(dict({"kind": "worker", "op": "restart", "home": str(self.homes[name]),
                                           "label": "com.synthetics.worker.d16" + name,
                                           "plist": str(self.root / "agents" / "x.plist"), "pid_before": 100},
                                          **row))["id"]

    def _op_status(self, name, op_id):
        with Store(self.homes[name]) as s:
            return s.launch_op(op_id)["status"]

    def test_d16a_settle_applies_homes_cover(self):
        root = self.root_delegation(actions=["worker.manage", "delegation.grant"],
                                    scope={"worker.manage": {"homes": ["employees"], "ops": ["restart"]}})
        child = lambda homes, key: self.ok(
            "codex", "authority", "delegate", "--parent", root, "--action", "worker.manage", "--grantee",
            self.ids["rowan"], "--scope", json.dumps({"worker.manage": {"homes": homes, "ops": ["restart"]}}),
            "--key", key, session="codex")
        child([self.ids["rowan"]], "d16a-1")
        op_id = self._pending_op("vesper")
        with mock.patch("synthetics.install.service_pid", return_value=300):
            self.refused("not_delegated", "vesper", "authority", "settle", op_id, session="rowan")
        self.assertEqual(self._op_status("vesper", op_id), "pending")
        child([self.ids["vesper"]], "d16a-2")
        with mock.patch("synthetics.install.service_pid", return_value=300):
            settled = self.ok("vesper", "authority", "settle", op_id, session="rowan")
        self.assertEqual((settled["outcome"], settled["reconciled_by"]), ("confirmed", self.ids["rowan"]))

    def test_d16a_settle_fenced_against_run_end_and_retract(self):
        root = self.root_delegation(actions=["worker.manage"],
                                    scope={"worker.manage": {"homes": ["employees"], "ops": ["restart"]}})
        from synthetics import install
        real = install.op_evidence
        op_id = self._pending_op("vesper")

        def end_run(record):
            with Store(self.homes["codex"]) as s:
                s.run_finish(self.runs["codex"], "done")
            return real(record)
        with mock.patch("synthetics.install.op_evidence", side_effect=end_run), \
                mock.patch("synthetics.install.service_pid", return_value=300):
            self.refused("run_inactive", "vesper", "authority", "settle", op_id, session="codex")
        self.assertEqual(self._op_status("vesper", op_id), "pending")
        with Store(self.homes["codex"]) as s:
            self.runs["codex"] = s.run_begin()["id"]

        def retract(record):
            self.ok("codex", "authority", "retract", root, "--reason", "d16a")
            return real(record)
        with mock.patch("synthetics.install.op_evidence", side_effect=retract), \
                mock.patch("synthetics.install.service_pid", return_value=300):
            self.refused("delegation_retracted", "vesper", "authority", "settle", op_id, session="codex")
        self.assertEqual(self._op_status("vesper", op_id), "pending")
        self.assertTrue([a for a in self.audit("codex", "authority.refused") if a["data"].get("stage") == "write"])

    def test_d16a_adapter_settle_only_in_own_ledger(self):
        self.root_delegation(actions=["adapter.manage"],
                             scope={"adapter.manage": {"labels": ["com.synthetics.adapter.x"]}})
        op_id = self._pending_op("vesper", kind="adapter", label="com.synthetics.adapter.x")
        self.refused("not_own_ledger", "vesper", "authority", "settle", op_id, session="codex")
        self.assertEqual(self._op_status("vesper", op_id), "pending")

    def test_d16b_install_evidence_needs_new_pid(self):
        import plistlib
        from synthetics import install
        plist = self.root / "w.plist"
        argv = ["/x", "serve"]
        with open(plist, "wb") as handle:
            plistlib.dump({"Label": "L", "ProgramArguments": argv}, handle)
        rec = {"op": "install", "label": "L", "plist": str(plist), "argv": argv}
        cases = [(dict(no_start=True, pid_before=None), 100, "confirmed"),
                 (dict(no_start=False, pid_before=100), 100, "uncertain"),  # plist alone, same PID
                 (dict(no_start=False, pid_before=100), 200, "confirmed"),
                 (dict(no_start=False, pid_before=None), 200, "confirmed"),  # absent before, live after
                 (dict(no_start=False, pid_before="unknown"), 200, "uncertain"),
                 (dict(pid_before=100), 200, "confirmed"),  # no_start missing: judged as install+start
                 (dict(pid_before=100), 100, "uncertain"),
                 ({}, 200, "uncertain")]  # no pre-op PID recorded at all: unknown
        for extra, pid, want in cases:
            with mock.patch("synthetics.install.service_pid", return_value=pid):
                self.assertEqual(install.op_evidence(dict(rec, **extra))["outcome"], want, (extra, pid))
        self.assertEqual(install.pid_intent(False), "unknown")
        self.assertIsNone(install.pid_intent(None))
        self.assertEqual(install.pid_intent(42), 42)
        # the worker ledger row records the full intent
        self._worker_setup()
        with mock.patch("synthetics.install.service_pid", return_value=False), \
                mock.patch("synthetics.install.loaded", return_value=False):
            code, out, err = self.winstall()
        self.assertEqual(code, 0, err)
        with Store(self.homes["rowan"]) as s:
            row = s.launch_op(out["op_id"])
        self.assertEqual((row["no_start"], row["pid_before"]), (True, "unknown"))

    def test_d16c_effect_raises_partway(self):
        """Plist written, then launchctl raises: uncertain (never failed), with and without a failing settle."""
        self._worker_setup()
        from synthetics.store import Store as S
        with mock.patch("synthetics.install.start_if_needed", side_effect=RuntimeError("launchctl boom")), \
                mock.patch("synthetics.install.service_pid", return_value=None), \
                mock.patch("synthetics.install.loaded", return_value=False):
            code, out, err = self.cli("rowan", "worker", "install", "--claude", str(self.claude),
                                      "--launch-agents-dir", str(self.agents), session="codex")
        self.assertEqual(code, 0, err)
        self.assertEqual(out["outcome"], "uncertain")
        self.assertNotIn("pending_op", out)
        self.assertIn("launchctl boom", out["error"])
        with Store(self.homes["rowan"]) as s:
            row = s.launch_op(out["op_id"])
        self.assertEqual((row["status"], row["outcome"]), ("settled", "uncertain"))
        self.assertTrue(list(self.agents.glob("*.plist")))
        # again, with the settle write failing too: pending + uncertain + op_id, no retry
        with mock.patch("synthetics.install.start_if_needed", side_effect=RuntimeError("launchctl boom")) as st, \
                mock.patch.object(S, "launch_op_settle", side_effect=OSError("disk full")), \
                mock.patch("synthetics.install.service_pid", return_value=None), \
                mock.patch("synthetics.install.loaded", return_value=False):
            code, out, err = self.cli("rowan", "worker", "install", "--claude", str(self.claude),
                                      "--launch-agents-dir", str(self.agents), session="codex")
        self.assertEqual(code, 0, err)
        self.assertEqual((out["outcome"], out["pending_op"], st.call_count), ("uncertain", out["op_id"], 1))
        self.assertEqual(self._op_status("rowan", out["op_id"]), "pending")
        with mock.patch("synthetics.install.start_if_needed") as st, \
                mock.patch("synthetics.install.loaded", return_value=False):
            self.refused("pending_op", "rowan", "worker", "install", "--claude", str(self.claude),
                         "--launch-agents-dir", str(self.agents), session="codex")
        self.assertEqual(st.call_count, 0)

    def test_d16c_adapter_effect_raises(self):
        label = "com.synthetics.adapter.postern"
        agents = self.root / "agents"
        A = ("--launch-agents-dir", str(agents))
        self.ok("codex", "adapter", "install", "--label", label, *A, "--no-start", "--", "/usr/bin/true")
        self.root_delegation(actions=["adapter.manage"], scope={"adapter.manage": {"labels": [label]}})
        with mock.patch("synthetics.install._control", side_effect=RuntimeError("kickstart boom")), \
                mock.patch("synthetics.install.service_pid", return_value=100):
            out = self.ok("codex", "adapter", "restart", "--label", label, *A, session="codex")
        self.assertEqual(out["outcome"], "uncertain")
        with Store(self.homes["codex"]) as s:
            self.assertEqual(s.launch_op(out["op_id"])["outcome"], "uncertain")
        # a failure before any effect (fence refusal) stays "failed" and is raised
        with Store(self.homes["codex"]) as s:
            s.run_finish(self.runs["codex"], "done")
        with mock.patch("synthetics.install._control") as ctl, \
                mock.patch("synthetics.install.service_pid", return_value=100):
            self.refused("run_inactive", "codex", "adapter", "restart", "--label", label, *A, session="codex")
        self.assertEqual(ctl.call_count, 0)

    def test_d10c_idle_hold_race(self):
        from synthetics.store import WorkerHeld
        self._worker_setup()
        rowan = self.homes["rowan"]
        # run first → the hold is refused (busy); hold first → run_begin is refused
        with Store(rowan) as s:
            busy = s.run_begin()["id"]
            self.assertIsNone(s.worker_hold_acquire({"op": "stop"}))
            s.run_finish(busy, "done")
            hold = s.worker_hold_acquire({"op": "stop"})
            self.assertIsNotNone(hold)
            with self.assertRaises(WorkerHeld):
                s.run_begin()
            self.assertIsNone(s.worker_hold_acquire({"op": "stop"}))
            self.assertTrue(s.worker_hold_release(hold["token"]))
            short = s.worker_hold_acquire({"op": "stop"}, seconds=-1)  # an expired hold (crashed holder)
            s.run_finish(s.run_begin()["id"], "done")
            s.worker_hold_release(short["token"])
        # the worker tries to start a run while the delegated op runs: refused; free again afterwards
        seen = []

        def control(kind, op, label, args):
            with Store(rowan) as t:
                try:
                    t.run_begin()
                    seen.append("started")
                except WorkerHeld:
                    seen.append("held")
            return {}
        with mock.patch("synthetics.install._control", side_effect=control), \
                mock.patch("synthetics.install.service_pid", side_effect=pids(100, 200)):
            self.assertEqual(self.ok("rowan", "worker", "restart", session="codex")["outcome"], "confirmed")
        self.assertEqual(seen, ["held"])
        with Store(rowan) as s:
            self.assertIsNone(s.worker_hold())
        # a crash inside the op still releases the hold and records uncertain (D16c; no retry)
        with mock.patch("synthetics.install._control", side_effect=RuntimeError("boom")), \
                mock.patch("synthetics.install.service_pid", return_value=None):
            self.assertEqual(self.ok("rowan", "worker", "stop", session="codex")["outcome"], "uncertain")
        with Store(rowan) as s:
            self.assertIsNone(s.worker_hold())

    def test_d10c_busy_restart_is_deferred(self):
        self._worker_setup()
        with Store(self.homes["rowan"]) as s:
            busy = s.run_begin()["id"]
        state = {"policy": {"allowed_models": None, "allowed_claude": None, "baseline": {"budget": 1}},
                 "effective": {"daily_limit": 100.0}}
        with mock.patch("synthetics.install._control") as ctl, \
                mock.patch("synthetics.runtime.read_state", return_value=state), \
                mock.patch("synthetics.runtime.validate", side_effect=lambda c, *a: c), \
                mock.patch("synthetics.runtime.merge", side_effect=lambda e, s: dict(e, **s)), \
                mock.patch("synthetics.runtime.digest", return_value="d"):
            out = self.ok("rowan", "worker", "restart", session="codex")
        self.assertEqual(ctl.call_count, 0)
        self.assertEqual(out["outcome"], "deferred")
        with Store(self.homes["rowan"]) as s:
            req = s.runtime_requests(1)[0]
            self.assertEqual((req["id"], req["origin"], req["settings"]), (out["request"], "delegated", {}))
            s.run_finish(busy, "done")
        stages = [r["data"]["stage"] for r in self.audit("rowan", "authority.worker")]
        self.assertIn("deferred", stages)

    def test_d11_busy_stop_remove_refusal_changes_nothing(self):
        """D11a/b: a busy target asked to stop/remove is refused (audited with actor, run, target, op; message
        "target busy; ask again when idle"); no hold left, no runtime request, plist untouched, no retry."""
        self._worker_setup()
        self.ok("rowan", "worker", "install", "--claude", str(self.claude), "--launch-agents-dir",
                str(self.agents), "--no-start", session="codex")
        plist = next(self.agents.iterdir())
        before = (plist.read_bytes(), plist.stat().st_mtime_ns)
        with Store(self.homes["rowan"]) as s:
            busy = s.run_begin()["id"]
            requests = len(s.runtime_requests(50))
        with mock.patch("synthetics.install._control") as ctl:
            for op, extra in (("stop", ()), ("remove", ("--launch-agents-dir", str(self.agents)))):
                c, out, err = self.cli("rowan", "worker", op, *extra, session="codex")
                self.assertNotEqual(c, 0, out)
                self.assertIn("not_idle", err)
                self.assertIn("target busy; ask again when idle", err)
            self.assertEqual(ctl.call_count, 0)
        with Store(self.homes["rowan"]) as s:
            self.assertIsNone(s.worker_hold())
            self.assertEqual(len(s.runtime_requests(50)), requests)
            s.run_finish(busy, "done")
        self.assertEqual((plist.read_bytes(), plist.stat().st_mtime_ns), before)
        refused = [r["data"] for r in self.audit("codex", "authority.refused")
                   if r["data"].get("code") == "not_idle"]
        self.assertEqual(sorted(r["op"] for r in refused), ["remove", "stop"])
        for r in refused:
            self.assertEqual((r["actor"], r["run"], r["target"]),
                             (self.ids["codex"], self.runs["codex"], str(self.homes["rowan"].resolve())))
        stages = [r["data"]["stage"] for r in self.audit("rowan", "authority.worker")]
        self.assertEqual(stages.count("requested"), 1)  # only the install; the refusals executed nothing

    def test_d12_controller_label(self):
        """D12: worker ops label the target engine: current (state pid live + worker_hold), old_controller
        (state pid live, no capability), unknown (no/stale state or unreadable PID), not_running."""
        from synthetics import install, runtime
        self.assertIn("worker_hold", runtime.CAPABILITIES)
        home = self.homes["rowan"]
        state = home / "runtime.state.json"
        policy = {"allowed_models": ["claude-opus-5-5"], "allowed_claude": []}
        state.write_text(json.dumps({"policy": policy, "pid": 42}))
        self.assertEqual(install.target_controller(home, 42), "old_controller")
        self.assertEqual(install.target_controller(home, 43), "unknown")
        self.assertEqual(install.target_controller(home, None), "not_running")
        self.assertEqual(install.target_controller(home, False), "unknown")
        state.write_text(json.dumps({"policy": policy, "pid": 42, "capabilities": ["worker_hold"]}))
        self.assertEqual(install.target_controller(home, 42), "current")
        state.unlink()
        self.assertEqual(install.target_controller(home, 42), "unknown")
        self._worker_setup()
        state.write_text(json.dumps({"policy": policy, "pid": 100}))
        with mock.patch("synthetics.install._control", return_value={}), \
                mock.patch("synthetics.install.READY_WAIT_SECONDS", 0), \
                mock.patch("synthetics.install.service_pid", side_effect=pids(100, 300)):
            out = self.ok("rowan", "worker", "restart", session="codex")
        self.assertEqual(out["controller"], "old_controller")
        req = [r["data"] for r in self.audit("rowan", "authority.worker") if r["data"]["stage"] == "requested"]
        self.assertEqual(req[-1]["controller"], "old_controller")

    # ---- D8c bootstrap against a synthetic fixture (no real records) ----
    BOOT_ACTIONS = ["project.grant", "project.redact", "company.grant", "runtime.request", "worker.manage",
                    "limit.resume", "limit.raise", "adapter.manage", "kit.run", "delegation.grant"]
    BOOT_SCOPE = {"project.grant": {"projects": ["synthetics"], "max_mode": "write"},
                  "project.redact": {"projects": ["synthetics"]},
                  "company.grant": {"max_mode": "read"},
                  "runtime.request": {"homes": ["employees"]},
                  "worker.manage": {"homes": ["employees"],
                                    "ops": ["install", "start", "stop", "restart", "remove", "kickstart"]},
                  "limit.resume": {"homes": ["employees"]},
                  "limit.raise": {"homes": ["employees"], "max_usd_per_day": 150,
                                  "durations": ["today", "persistent"]},
                  "adapter.manage": {"labels": ["com.synthetics.*"]},
                  "kit.run": {"kits": ["/Users/david/projects/synthetics/.synthetics/management"]}}
    BOOT_EXCERPT = ('the manager already has my authorization for this internal work, so it should handle it '
                    'directly instead of asking for a separate approval')

    def test_d8c_bootstrap_fixture_and_dry_run_writes_nothing(self):
        home = self.homes["codex"]
        capture.write_humans(home, {"default_human": "david", "senders": ["david"],
                                    "channel_prefixes": ["fieldpost", "codex-desktop"],
                                    "operators": {"senders": ["codex-desktop"], "source_prefixes": []}})
        desk = b"d" * 64
        body = ("as my tech secretary, " + self.BOOT_EXCERPT + ".")
        with Store(home) as s:
            s.channel_register("codex-desktop", Store.credential_hash(desk), ["codex-desktop"], "test",
                               routes=["david"])
            like_a114 = s.feed("david", body, source="codex-desktop", external_id="a114", credential=desk)["id"]
            like_6751 = s.feed("david", body, source="telegram", external_id="6751")["id"]  # auth null
            self.assertIsNone(s.event_get(like_6751)["auth"])
        argv = ["codex", "authority", "delegate", "--grantee", self.ids["codex"],
                *sum((["--action", a] for a in self.BOOT_ACTIONS), []), "--scope", json.dumps(self.BOOT_SCOPE),
                "--excerpt", self.BOOT_EXCERPT, "--by", "operator", "--key", "m28-bootstrap-root"]
        self.refused("not_human_source", *argv, "--source", like_6751, "--context-ref", like_a114)

        def fingerprint():
            import hashlib
            files = sorted(p for p in (self.root / "company").rglob("*") if p.is_file())
            with Store(home) as s:
                audit_n = len(s.audit_list(1000))
            return [(p.name, hashlib.sha256(p.read_bytes()).hexdigest()) for p in files], audit_n

        before = fingerprint()
        code, out, err = self.cli(*argv, "--source", like_a114, "--context-ref", like_6751, "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertTrue(out["dry_run"])
        self.assertEqual(fingerprint(), before)  # --dry-run writes nothing (company log or home audit)
        self.assertEqual(self.company()["delegations"], {})
        rid = self.ok(*argv, "--source", like_a114, "--context-ref", like_6751)["ids"][0]
        d = self.company()["delegations"][rid]
        self.assertEqual([x["event"] for x in d["sources"]], [like_a114])
        self.assertEqual(d["sources"][0]["auth"]["channel"], "codex-desktop")
        self.assertEqual(d["context_refs"], [like_6751])


    # ---- M28 P1b: limit.raise / limit.resume (D7) ----
    LIMIT_SCOPE = {"limit.raise": {"homes": ["employees"], "max_usd_per_day": 150, "durations": ["today", "persistent"]},
                   "limit.resume": {"homes": ["employees"]}}

    def limit_root(self, usd=150):
        scope = json.loads(json.dumps(self.LIMIT_SCOPE))
        scope["limit.raise"]["max_usd_per_day"] = usd
        return self.root_delegation(actions=["limit.raise", "limit.resume", "delegation.grant"], scope=scope)

    def override(self, name):
        from synthetics.engine import read_limit_override
        return read_limit_override(self.homes[name])

    def test_d7_self_raise_ceiling(self):
        self.refused("not_delegated", "vesper", "limit", "raise", "120", session="vesper")
        self.limit_root()
        out = self.ok("codex", "limit", "raise", "150", "--by", self.ids["codex"], session="codex")
        self.assertEqual((out["limit_usd"], out["self_raise"], out["set_run"]), (150.0, True, self.runs["codex"]))
        for usd in ("151", "0"):
            self.refused("exceeds_ceiling", "codex", "limit", "raise", usd, session="codex")
        self.refused("owner_only", "codex", "limit", "raise", "100", "--date", "2099-01-01", session="codex")
        self.refused("by_mismatch", "codex", "limit", "raise", "100", "--by", "david", session="codex")
        self.assertEqual(self.override("codex")["limit_usd"], 150.0)
        raises = authority.self_raises(self.homes["codex"])
        self.assertEqual([(r["usd"], r["duration"], r["run"]) for r in raises], [(150.0, "today", self.runs["codex"])])
        from synthetics import doctor
        report = doctor.diagnose(self.homes["codex"], launchd=lambda label: None)
        self.assertEqual([r["usd"] for r in report["self_raises"]], [150.0])
        self.assertIn("limit_self_raise", [f["code"] for f in report["findings"]])
        # The owner path is unchanged: any value, 0 disables the guard for the day.
        self.assertEqual(self.ok("codex", "limit", "raise", "0")["limit_usd"], 0.0)

    def test_d7_child_ceiling_and_named_members(self):
        root = self.limit_root(100)
        base = ["codex", "authority", "delegate", "--parent", root, "--action", "limit.raise",
                "--grantee", self.ids["vesper"]]
        sc = lambda usd, homes: json.dumps({"limit.raise": {"homes": homes, "max_usd_per_day": usd,
                                                            "durations": ["today"]}})
        self.refused("exceeds_parent", *base, "--scope", sc(120, [self.ids["rowan"]]), session="codex")
        self.refused("bad_scope", *base, "--scope", sc(151, [self.ids["rowan"]]), session="codex")
        self.ok(*base, "--scope", sc(80, [self.ids["rowan"]]), session="codex")
        self.assertEqual(self.ok("rowan", "limit", "raise", "80", session="vesper")["limit_usd"], 80.0)
        self.assertFalse(self.ok("rowan", "limit", "raise", "70", session="vesper")["self_raise"])
        self.refused("not_delegated", "rowan", "limit", "raise", "90", session="vesper")
        self.refused("not_delegated", "codex", "limit", "raise", "50", session="vesper")
        self.refused("not_delegated", "rowan", "limit", "raise", "50", "--persistent", session="vesper")
        self.assertEqual(authority.self_raises(self.homes["rowan"]), [])

    def test_d10a_self_raise_needs_homes_cover(self):
        root = self.limit_root(100)
        base = ["codex", "authority", "delegate", "--parent", root, "--action", "limit.raise",
                "--grantee", self.ids["vesper"], "--scope"]
        sc = lambda homes: json.dumps({"limit.raise": {"homes": homes, "max_usd_per_day": 80, "durations": ["today"]}})
        self.ok(*base, sc([self.ids["rowan"]]), session="codex")
        self.refused("not_delegated", "vesper", "limit", "raise", "80", session="vesper")
        self.assertEqual(self.override("vesper"), {})
        self.ok(*base, sc([self.ids["rowan"], self.ids["vesper"]]), "--key", "d10a-2", session="codex")
        out = self.ok("vesper", "limit", "raise", "80", session="vesper")
        self.assertEqual((out["limit_usd"], out["self_raise"]), (80.0, True))
        self.refused("not_delegated", "vesper", "limit", "raise", "90", session="vesper")

    def test_d7_raise_after_run_ended_refused(self):
        self.limit_root()
        w = self._warrant("limit.raise")
        from synthetics.engine import write_limit_override
        with Store(self.homes["codex"]) as s:
            s.run_finish(self.runs["codex"], "done")
        with self.assertRaises(authority.AuthorityError) as cm:
            authority.fenced_file(w, lambda: write_limit_override(self.homes["codex"], date="x", limit_usd=1))
        self.assertEqual(cm.exception.code, "run_inactive")
        self.assertEqual(self.override("codex"), {})
        self.refused("run_inactive", "codex", "limit", "raise", "100", session="codex")
        self.assertTrue([a for a in self.audit("codex", "authority.refused") if a["data"].get("stage") == "write"])

    def test_d7_persistent_self_raise_is_a_runtime_request(self):
        self.limit_root()
        state = {"policy": {"allowed_models": None, "allowed_claude": None, "baseline": {"budget": 1}},
                 "effective": {"daily_limit": 100.0}}
        with mock.patch("synthetics.runtime.read_state", return_value=state), \
                mock.patch("synthetics.runtime.validate", side_effect=lambda c, *a: c), \
                mock.patch("synthetics.runtime.merge", side_effect=lambda e, s: dict(e, **s)), \
                mock.patch("synthetics.runtime.digest", return_value="d"):
            rec = self.ok("codex", "limit", "raise", "150", "--persistent", "--key", "p1", session="codex")
            self.assertEqual((rec["origin"], rec["settings"]), ("delegated", {"daily_limit": 150.0}))
            self.refused("exceeds_ceiling", "codex", "limit", "raise", "0", "--persistent", session="codex")
        self.assertEqual(self.audit("codex", "authority.receipt")[0]["data"]["key"], "p1")
        self.assertEqual([r["duration"] for r in authority.self_raises(self.homes["codex"])], ["persistent"])
        self.assertEqual(self.override("codex"), {})

    def test_p1b_limit_resume_needs_scope(self):
        self.refused("not_delegated", "rowan", "limit", "resume", session="codex")
        root = self.limit_root()
        self.ok("codex", "authority", "delegate", "--parent", root, "--action", "limit.resume",
                "--grantee", self.ids["vesper"], "--scope",
                json.dumps({"limit.resume": {"homes": [self.ids["rowan"]]}}), session="codex")
        out = self.ok("rowan", "limit", "resume", session="vesper")
        self.assertEqual((out["resumed_by"], out["resumed_run"]), (self.ids["vesper"], self.runs["vesper"]))
        self.refused("not_delegated", "codex", "limit", "resume", session="vesper")

    # ---- M28 P1c-a: company-log policy entries (limit.ceiling, worker.defaults) ----
    def policy(self, name, body, actor=None, key=None, **kw):
        argv = ["codex", "authority", "policy", name, "--body", json.dumps(body), "--reason", "test"]
        if key:
            argv += ["--key", key]
        if actor:
            return self.cli(*argv, session=actor, **kw)
        return self.cli(*argv, "--by", "david-desktop", **kw)

    def ceil(self, usd, allow_disable=False, actor=None, key=None):
        code, out, err = self.policy("limit.ceiling", {"max_usd_per_day": usd, "allow_disable": allow_disable},
                                     actor=actor, key=key)
        self.assertEqual(code, 0, err)
        return out["ids"][0]

    def company(self):
        from synthetics import roster
        return roster.company_state_for_home(self.homes["codex"])

    def test_p1c_no_policy_initial_150_binds(self):
        self.assertEqual(authority.ceiling(self.company())["usd"], 150.0)
        self.limit_root()
        self.refused("exceeds_ceiling", "codex", "limit", "raise", "151", session="codex")
        self.refused("bad_scope", "codex", "authority", "delegate", "--grantee", self.ids["codex"], "--action",
                     "limit.raise", "--scope", json.dumps({"limit.raise": {"homes": ["employees"],
                     "max_usd_per_day": 200, "durations": ["today"]}}), "--source", self.ev_ok,
                     "--excerpt", WORDS, "--by", "david-desktop")

    def test_p1c_raised_ceiling_200_and_none(self):
        self.ceil(200)
        self.limit_root(200)
        self.assertEqual(self.ok("codex", "limit", "raise", "200", session="codex")["limit_usd"], 200.0)
        self.refused("exceeds_ceiling", "codex", "limit", "raise", "201", session="codex")
        self.refused("exceeds_ceiling", "codex", "limit", "raise", "0", session="codex")
        self.ceil("none", key="none")
        self.assertIsNone(authority.ceiling(self.company())["usd"])
        # The delegation's own max still binds; with no ceiling a new root may carry any amount.
        self.refused("not_delegated", "codex", "limit", "raise", "500", session="codex")
        self.root_delegation(actions=["limit.raise"], scope={"limit.raise": {"homes": ["employees"],
                             "max_usd_per_day": 500, "durations": ["today"]}})
        self.assertEqual(self.ok("codex", "limit", "raise", "500", session="codex")["limit_usd"], 500.0)

    def test_p1c_lowering_clamps_and_doctor_lists(self):
        from synthetics import doctor
        root = self.limit_root(150)
        self.ceil(100)
        state = self.company()
        self.assertIn(root, state["delegations"])  # clamped, not invalidated
        self.assertEqual([c["delegation"] for c in authority.clamped(state)], [root])
        self.assertEqual(self.ok("codex", "limit", "raise", "100", session="codex")["limit_usd"], 100.0)
        self.refused("exceeds_ceiling", "codex", "limit", "raise", "120", session="codex")
        report = doctor.diagnose(self.homes["codex"], launchd=lambda label: None)
        self.assertEqual(report["limit_ceiling"]["usd"], 100.0)
        self.assertEqual([c["delegation"] for c in report["limit_clamped"]], [root])
        self.assertIn("limit_clamped", [f["code"] for f in report["findings"]])

    def test_p1c_allow_disable(self):
        self.limit_root()
        self.ceil(150, allow_disable=False)
        self.refused("exceeds_ceiling", "codex", "limit", "raise", "0", session="codex")
        self.ceil(150, allow_disable=True, key="dis")
        self.assertEqual(self.ok("codex", "limit", "raise", "0", session="codex")["limit_usd"], 0.0)

    def test_p1c_session_writer_needs_action_company_write_and_live_run(self):
        body = {"max_usd_per_day": 200, "allow_disable": False}
        c, out, err = self.policy("limit.ceiling", body, actor="codex")
        self.assertNotEqual(c, 0, out)
        self.assertIn("not_delegated", err)
        root = self.limit_root()
        # Vesper holds limit.raise through a child but has no company write grant.
        self.ok("codex", "authority", "delegate", "--parent", root, "--action", "limit.raise", "--grantee",
                self.ids["vesper"], "--scope", json.dumps({"limit.raise": {"homes": [self.ids["vesper"]],
                "max_usd_per_day": 100, "durations": ["today"]}}), session="codex")
        c, out, err = self.policy("limit.ceiling", body, actor="vesper", env_home="vesper")
        self.assertNotEqual(c, 0, out)
        self.assertEqual(authority.ceiling(self.company())["usd"], 150.0)
        # worker.defaults needs worker.manage, not limit.raise.
        c, out, err = self.policy("worker.defaults", {"allowed_models": ["m"], "allowed_claude": ["/x/claude"]},
                                  actor="codex")
        self.assertIn("not_delegated", err)
        out = self.policy("limit.ceiling", body, actor="codex")[1]
        self.assertEqual(authority.ceiling(self.company())["usd"], 200.0)
        rows = self.audit("codex", "authority.policy")
        self.assertEqual([(r["data"]["name"], r["data"]["entry"]) for r in rows], [("limit.ceiling", out["ids"][0])])
        with Store(self.homes["codex"]) as s:
            s.run_finish(self.runs["codex"], "done")
        c, out, err = self.policy("limit.ceiling", dict(body, max_usd_per_day=300), actor="codex", key="late")
        self.assertNotEqual(c, 0, out)
        self.assertEqual(authority.ceiling(self.company())["usd"], 200.0)

    # ---- M28 D18: max_usd_per_day "policy" (bounded only by the effective limit.ceiling) ----
    def d18_child(self, root, usd, key):
        return self.cli("codex", "authority", "delegate", "--parent", root, "--action", "limit.raise",
                        "--grantee", self.ids["vesper"], "--key", key, "--scope",
                        json.dumps({"limit.raise": {"homes": [self.ids["rowan"]], "max_usd_per_day": usd,
                                                    "durations": ["today"]}}), session="codex")

    def test_d18_policy_root_follows_raised_ceiling(self):
        self.ceil(200)
        self.limit_root("policy")
        self.assertEqual(self.ok("codex", "limit", "raise", "180", session="codex")["limit_usd"], 180.0)
        self.refused("exceeds_ceiling", "codex", "limit", "raise", "201", session="codex")
        self.ceil(120, key="lower")  # lowering binds a "policy" root at once
        self.refused("exceeds_ceiling", "codex", "limit", "raise", "130", session="codex")
        self.ceil("none", key="none")  # "policy" + no ceiling = no daily bound
        self.assertEqual(self.ok("codex", "limit", "raise", "900", session="codex")["limit_usd"], 900.0)

    def test_d18_numeric_root_alone_keeps_its_number_and_any_root_passes(self):
        self.ceil(200)
        self.limit_root(150)
        self.refused("not_delegated", "codex", "limit", "raise", "180", session="codex")
        self.root_delegation(actions=["limit.raise"], scope={"limit.raise": {"homes": ["employees"],
                             "max_usd_per_day": "policy", "durations": ["today"]}})
        self.assertEqual(self.ok("codex", "limit", "raise", "180", session="codex")["limit_usd"], 180.0)

    def test_d18_allow_disable_unchanged(self):
        self.limit_root("policy")
        self.refused("exceeds_ceiling", "codex", "limit", "raise", "0", session="codex")
        self.ceil(150, allow_disable=True)
        self.assertEqual(self.ok("codex", "limit", "raise", "0", session="codex")["limit_usd"], 0.0)

    def test_d18_children(self):
        num = self.limit_root(100)
        c, out, err = self.d18_child(num, "policy", "c1")
        self.assertNotEqual(c, 0, out)
        self.assertIn("exceeds_parent", err)
        pol = self.limit_root("policy")
        self.assertEqual(self.d18_child(pol, "policy", "c2")[0], 0)
        self.ceil(200)
        self.assertEqual(self.ok("rowan", "limit", "raise", "190", session="vesper")["limit_usd"], 190.0)
        self.refused("exceeds_ceiling", "rowan", "limit", "raise", "210", session="vesper")
        self.ok("codex", "authority", "retract", pol, "--reason", "t", "--by", "david-desktop")
        pol2 = self.limit_root("policy")
        self.assertEqual(self.d18_child(pol2, 120, "c3")[0], 0)
        self.refused("not_delegated", "rowan", "limit", "raise", "130", session="vesper")
        self.assertEqual(self.ok("rowan", "limit", "raise", "120", session="vesper")["limit_usd"], 120.0)

    def test_d18_parent_bound_applies_at_use(self):
        # Defence in depth: even a child stored above its parent (bypassing the write check) is capped.
        root = self.limit_root(100)
        state = self.company()
        child = {"id": "co-x", "seq": 10 ** 6, "grantee": self.ids["vesper"], "parent": root,
                 "actions": ["limit.raise"], "scope": {"limit.raise": {"homes": ["employees"],
                 "max_usd_per_day": "policy", "durations": ["today"]}}}
        state["delegations"]["co-x"] = child
        self.assertEqual(authority.limit_bound(state, child), 100.0)
        self.assertEqual(authority.scope_usd("policy", state), 150.0)
        ok = lambda usd: authority.covering(state, self.ids["vesper"], "limit.raise",
                                            lambda sc: usd <= sc["max_usd_per_day"])
        self.assertIsNotNone(ok(100))
        self.assertIsNone(ok(120))

    def test_d18_clamped_doctor_and_root_owner_only(self):
        from synthetics import doctor
        pol = self.limit_root("policy")
        self.ceil(100)
        self.assertEqual(authority.clamped(self.company()), [])
        report = doctor.diagnose(self.homes["codex"], launchd=lambda label: None)
        self.assertEqual(report["limit_clamped"], [])
        self.assertEqual(self.company()["delegations"][pol]["scope"]["limit.raise"]["max_usd_per_day"], "policy")
        self.refused("root_requires_owner", "codex", "authority", "delegate", "--grantee", self.ids["codex"],
                     "--action", "limit.raise", "--scope", json.dumps({"limit.raise": {"homes": ["employees"],
                     "max_usd_per_day": "policy", "durations": ["today"]}}), "--source", self.ev_ok,
                     "--excerpt", WORDS, session="codex")
        self.refused("bad_scope", "codex", "authority", "delegate", "--grantee", self.ids["codex"], "--action",
                     "limit.raise", "--scope", json.dumps({"limit.raise": {"homes": ["employees"],
                     "max_usd_per_day": "Policy", "durations": ["today"]}}), "--source", self.ev_ok,
                     "--excerpt", WORDS, "--by", "david-desktop")

    # ---- M28 D18-r2: non-finite USD refused; ceiling refusals are team policy (exceeds_ceiling) ----
    NON_FINITE = ("nan", "inf", "-inf", "NaN", "Infinity", "-Infinity")

    def _limit_state(self):
        with Store(self.homes["codex"]) as s:
            reqs = s.runtime_requests(limit=100)
        with Store(self.homes["rowan"]) as s:
            reqs_r = s.runtime_requests(limit=100)
        company = self.company()
        return (self.override("codex"), self.override("rowan"), reqs, reqs_r,
                sorted(company["delegations"]), sorted(company.get("policies", {})),
                authority.ceiling(company))

    def parse_refused(self, home, *argv, **kw):
        """argparse refuses at parse time: SystemExit(2) before any authorization or state change."""
        with self.assertRaises(SystemExit) as cm:
            self.cli(home, *argv, **kw)
        self.assertEqual(cm.exception.code, 2)

    def _no_ceiling_policy_root(self):
        self.ceil("none")
        self.limit_root("policy")
        self.assertIsNone(authority.ceiling(self.company())["usd"])

    def test_d18r2_non_finite_limit_raise_refused_session(self):
        self._no_ceiling_policy_root()
        before = self._limit_state()
        for usd in self.NON_FINITE:
            for target, sess in (("codex", "codex"), ("rowan", "codex")):
                for extra in ((), ("--persistent",)):
                    with self.subTest(usd=usd, target=target, extra=extra):
                        self.parse_refused(target, "limit", "raise", usd, *extra, session=sess)
        self.assertEqual(self._limit_state(), before)
        self.assertEqual(self.audit("codex", "authority.used"), [])
        # A finite amount still passes under "policy" + ceiling "none" (no daily bound).
        self.assertEqual(self.ok("codex", "limit", "raise", "900", session="codex")["limit_usd"], 900.0)

    def test_d18r2_non_finite_limit_raise_refused_owner(self):
        before = self._limit_state()
        for usd in self.NON_FINITE:
            for extra in ((), ("--persistent",), ("--date", "2099-01-01")):
                with self.subTest(usd=usd, extra=extra):
                    self.parse_refused("rowan", "limit", "raise", usd, *extra)
        self.assertEqual(self._limit_state(), before)
        self.assertRaises(ValueError, authority.finite_usd, "nan")
        self.assertEqual(authority.finite_usd("12.5"), 12.5)

    def test_d18r2_non_finite_policy_ceiling_refused(self):
        self.limit_root("policy")
        before = self._limit_state()
        for v in (float("nan"), float("inf"), float("-inf")):
            for actor in (None, "codex"):  # owner and session (holds limit.raise + company write)
                with self.subTest(v=v, actor=actor):
                    c, out, err = self.policy("limit.ceiling", {"max_usd_per_day": v, "allow_disable": False},
                                              actor=actor)  # json.dumps writes the NaN / Infinity tokens
                    self.assertNotEqual(c, 0, out)
                    self.assertIn("bad_json", err)
        for v in (float("nan"), float("inf"), float("-inf")):  # direct route, past the CLI's JSON parser
            with self.subTest(direct=v):
                with self.assertRaises(authority.AuthorityError) as cm:
                    authority.parse_policy({"name": "limit.ceiling", "reason": "r", "max_usd_per_day": v,
                                            "allow_disable": False})
                self.assertEqual(cm.exception.code, "bad_policy")
        self.assertEqual(self._limit_state(), before)

    def test_d18r2_non_finite_delegation_scope_refused(self):
        self.ceil("none")
        root = self.limit_root("policy")
        before = self._limit_state()
        for v in (float("nan"), float("inf"), float("-inf")):
            scope = {"limit.raise": {"homes": ["employees"], "max_usd_per_day": v, "durations": ["today"]}}
            with self.subTest(root=v):
                self.refused("bad_json", "codex", "authority", "delegate", "--grantee", self.ids["codex"],
                             "--action", "limit.raise", "--scope", json.dumps(scope), "--source", self.ev_ok,
                             "--excerpt", WORDS, "--by", "david-desktop")
            with self.subTest(child=v):
                c, out, err = self.d18_child(root, v, "nf-%s" % v)
                self.assertNotEqual(c, 0, out)
                self.assertIn("bad_json", err)
            with self.subTest(direct=v):  # parse_scope itself, with no ceiling (max_usd None)
                with self.assertRaises(authority.AuthorityError) as cm:
                    authority.parse_scope(["limit.raise"], scope, max_usd=None)
                self.assertEqual(cm.exception.code, "bad_scope")
        self.assertEqual(self._limit_state(), before)

    def test_d18r2_ceiling_refusal_classes(self):
        self.limit_root("policy")
        before = self._limit_state()
        # Above the effective ceiling (initial $150 team policy, no entry): team policy, names the ceiling.
        err = self.refused("exceeds_ceiling", "codex", "limit", "raise", "151", session="codex")
        self.assertIn("150", err)
        self.assertIn("authority policy limit.ceiling", err)
        self.assertNotIn("owner_only", err)
        self.assertNotIn("not yet implemented", err)
        entry = self.ceil(200)
        err = self.refused("exceeds_ceiling", "codex", "limit", "raise", "201", "--persistent", session="codex")
        self.assertIn(entry, err)
        # 0 without allow_disable: team policy too.
        err = self.refused("exceeds_ceiling", "codex", "limit", "raise", "0", session="codex")
        self.assertIn("allow_disable", err)
        self.assertNotIn("owner_only", err)
        # A day other than today stays owner-only (not yet implemented under delegation).
        err = self.refused("owner_only", "codex", "limit", "raise", "100", "--date", "2099-01-01", session="codex")
        self.assertIn("not yet implemented under delegation", err)
        # Integrity refusals are unchanged.
        self.refused("by_mismatch", "codex", "limit", "raise", "100", "--by", "david", session="codex")
        self.refused("negative", "codex", "limit", "raise", "-1", session="codex")
        rows = [r["data"] for r in self.audit("codex", "authority.refused") if r["data"].get("code") == "exceeds_ceiling"]
        self.assertEqual(sorted((r["usd"], r["ceiling"]) for r in rows),
                         [(0.0, 200.0), (151.0, 150.0), (201.0, 200.0)])
        after = self._limit_state()
        self.assertEqual(after[:5], before[:5])

    def test_p1c_bad_policy_and_retract_restores_previous(self):
        for body in ({"max_usd_per_day": 0, "allow_disable": False}, {"max_usd_per_day": 200},
                     {"max_usd_per_day": "lots", "allow_disable": False},
                     {"max_usd_per_day": 200, "allow_disable": "no"}):
            c, out, err = self.policy("limit.ceiling", body)
            self.assertNotEqual(c, 0, (body, out))
            self.assertIn("bad_policy", err)
        first = self.ceil(200)
        second = self.ceil(120, key="second")
        self.assertEqual(authority.ceiling(self.company())["entry"], second)
        self.ok("codex", "authority", "retract", second, "--reason", "undo", "--by", "david-desktop")
        cap = authority.ceiling(self.company())
        self.assertEqual((cap["usd"], cap["entry"]), (200.0, first))
        self.ok("codex", "authority", "retract", first, "--reason", "undo", "--by", "david-desktop")
        self.assertEqual(authority.ceiling(self.company())["usd"], 150.0)

    def test_p1c_old_log_without_policies_loads(self):
        from synthetics import roster
        state = roster.company_fold([])
        state.pop("policies")
        self.assertEqual(authority.ceiling(state)["usd"], 150.0)
        self.assertIsNone(authority.worker_defaults(state))
        self.assertEqual(authority.clamped(state), [])

    def _defaults_setup(self):
        self._worker_setup()
        (self.homes["rowan"] / "runtime.state.json").unlink()
        self._publish("vesper", ["claude-opus-5-5"], [str(self.claude)])  # D15: an owner-installed member's lists

    def _publish(self, name, models, claudes):
        (self.homes[name] / "runtime.state.json").write_text(json.dumps(
            {"policy": {"allowed_models": models, "allowed_claude": claudes}}))

    def test_d15_arbitrary_executable_refused_at_write(self):
        self._defaults_setup()
        other = self.root / "bin" / "evil"
        other.write_text("#!/bin/sh\n")
        other.chmod(0o755)
        code, out, err = self.policy("worker.defaults", {"allowed_models": ["claude-opus-5-5"],
                                                         "allowed_claude": [str(other)]}, actor="codex")
        self.assertNotEqual(code, 0, out)
        self.assertIn("bad_policy", err)
        code, out, err = self.policy("worker.defaults", {"allowed_models": ["any-model"],
                                                         "allowed_claude": [str(self.claude)]}, actor="codex")
        self.assertNotEqual(code, 0, out)
        self.assertIn("bad_policy", err)
        with CompanyStore.at_root(self.root) as company:
            self.assertNotIn("worker.defaults", company.state().get("policies", {}))

    def test_d15_dropped_entry_refused_at_use(self):
        self._defaults_setup()
        code, out, err = self.policy("worker.defaults", {"allowed_models": ["claude-opus-5-5"],
                                                         "allowed_claude": [str(self.claude)]}, actor="codex")
        self.assertEqual(code, 0, err)
        self._publish("vesper", ["claude-opus-5-5"], ["/usr/bin/true"])  # the owner narrowed its worker
        c, out, err = self.winstall()
        self.assertNotEqual(c, 0, out)
        self.assertIn("bad_policy", err)
        self.assertFalse(list(self.agents.glob("*.plist")) if self.agents.exists() else [])

    def test_d15_delegated_install_does_not_vouch_for_itself(self):
        self._defaults_setup()
        self.assertEqual(self.policy("worker.defaults", {"allowed_models": ["claude-opus-5-5"],
                                                         "allowed_claude": [str(self.claude)]}, actor="codex")[0], 0)
        self.assertEqual(self.winstall()[0], 0)  # delegated install on rowan: rowan's ledger records it
        self._publish("rowan", ["claude-opus-5-5"], [str(self.claude)])
        (self.homes["vesper"] / "runtime.state.json").unlink()
        from synthetics import authority
        with CompanyStore.at_root(self.root) as company:
            state = company.state()
        self.assertEqual(authority.owner_allow_lists(self.root, state), [])
        with self.assertRaises(authority.AuthorityError):
            authority.check_worker_defaults(self.root, state, ["claude-opus-5-5"], [str(self.claude)])

    def test_p1c_worker_defaults(self):
        self._defaults_setup()
        c, out, err = self.winstall()
        self.assertNotEqual(c, 0, out)
        self.assertIn("no_policy", err)
        self.assertIn("not yet implemented under delegation", err)
        self.assertNotIn("owner installs", err)
        code, out, err = self.policy("worker.defaults", {"allowed_models": ["claude-opus-5-5"],
                                                         "allowed_claude": [str(self.claude)]}, actor="codex")
        self.assertEqual(code, 0, err)
        entry = out["ids"][0]
        c, out, err = self.winstall("--model", "claude-other")
        self.assertIn("model_not_allowed", err)
        c, out, err = self.winstall("--daily-limit", "151")
        self.assertIn("limit_exceeds", err)
        out = self.ok("rowan", "worker", "install", "--claude", str(self.claude), "--launch-agents-dir",
                      str(self.agents), "--no-start", session="codex")
        self.assertEqual(out["outcome"], "confirmed")
        self.assertIn(entry, out["policy_source"])

    def test_p1c_worker_defaults_daily_limit_follows_ceiling(self):
        self._defaults_setup()
        self.ceil(200)
        self.policy("worker.defaults", {"allowed_models": ["claude-opus-5-5"], "allowed_claude": [str(self.claude)]})
        self.assertEqual(self.winstall("--daily-limit", "200")[0], 0)

    def test_p1c_install_over_loaded_label_is_idle_gated(self):
        from synthetics import install
        self._worker_setup()
        with Store(self.homes["rowan"]) as s:
            s.run_begin()
        with mock.patch.object(install, "loaded", return_value=True):
            c, out, err = self.winstall()
        self.assertNotEqual(c, 0, out)
        self.assertIn("not_idle", err)
        self.assertFalse(self.agents.exists() and any(self.agents.iterdir()))
        with Store(self.homes["rowan"]) as s:
            self.assertIsNone(s.worker_hold())


    # ---- M28 P1c-b: adapter.install under delegation (Q1-Q3 rulings) ----
    ALABEL = "com.synthetics.adapter.fieldx"

    def _adapter_setup(self, scope_roots=None, labels=None):
        import hashlib
        self.internal = self.root / "internal"
        pkg = self.internal / "fieldx"
        pkg.mkdir(parents=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "__main__.py").write_text("print('relay')\n")
        self.review = self.root / "management" / "M99" / "SHA256SUMS.txt"
        self.review.parent.mkdir(parents=True)
        lines = ["%s  tree/fieldx/%s" % (hashlib.sha256((pkg / n).read_bytes()).hexdigest(), n)
                 for n in ("__init__.py", "__main__.py")]
        self.review.write_text("\n".join(lines) + "\n")
        self.review_sha = hashlib.sha256(self.review.read_bytes()).hexdigest()
        self.keys = self.root / "keys"
        self.keys.mkdir(mode=0o700)
        self.key = self.keys / "fieldx.key"
        self.key.write_text("k\n")
        self.key.chmod(0o600)
        self.agents = self.root / "agents"
        self.root_delegation(actions=["adapter.install", "delegation.grant"], scope={"adapter.install": {
            "labels": labels or [self.ALABEL], "roots": scope_roots or [str(self.internal)]}})

    def accept_review(self, path=None, sha=None):
        code, out, err = self.policy("adapter.reviews", {"sums_path": str(path or self.review),
                                                         "sums_sha256": sha or self.review_sha,
                                                         "review_ref": "management/M99/review.md"})
        self.assertEqual(code, 0, err)
        return out["ids"][0]

    def ainstall(self, *argv, label=None, cwd=None, review=None, start=False):
        from synthetics import install
        extra = [] if start else ["--no-start"]
        with mock.patch.object(install, "_key_dir", return_value=self.keys):
            return self.cli("codex", "adapter", "install", "--label", label or self.ALABEL, "--launch-agents-dir",
                            str(self.agents), "--cwd", str(cwd or self.internal), "--review",
                            str(review or self.review), *extra, "--",
                            *(argv or ("/usr/bin/python3", "-u", "-m", "fieldx", "--credential-file", str(self.key))),
                            session="codex")

    def test_p1cb_reviewed_module_installs(self):
        import plistlib
        from synthetics import install
        self._adapter_setup()
        c, out, err = self.ainstall()
        self.assertIn("review_not_accepted", err)  # no policy adapter.reviews entry yet
        entry = self.accept_review()
        c, out, err = self.ainstall()
        self.assertEqual(c, 0, err)
        self.assertEqual((out["outcome"], out["review"]["entry"]), ("confirmed", entry))
        with open(self.agents / (self.ALABEL + ".plist"), "rb") as handle:
            plist = plistlib.load(handle)
        self.assertEqual(plist["WorkingDirectory"], str(self.internal.resolve()))
        self.assertEqual(plist["ProgramArguments"][:4], ["/usr/bin/python3", "-u", "-m", "fieldx"])
        with Store(self.homes["codex"]) as s:
            op = s.launch_op(out["op_id"])
        self.assertEqual((op["status"], op["outcome"], op["kind"]), ("settled", "confirmed", "adapter"))
        rows = [r["data"] for r in self.audit("codex", "authority.adapter")]
        self.assertEqual(sorted(r["stage"] for r in rows), ["outcome", "requested"])
        self.assertTrue(all(r["review"]["sha256"] == self.review_sha for r in rows))
        # start: confirmed only by a new live PID (D10b); a loaded label is reloaded (G1)
        with mock.patch.object(install, "reload_if_loaded", return_value={"reloaded": True}) as rl, \
                mock.patch.object(install, "READY_WAIT_SECONDS", 0), \
                mock.patch.object(install, "service_pid", side_effect=pids(100, 200)):
            c, out, err = self.ainstall(start=True)
        self.assertEqual((c, out["outcome"], out["pid"], rl.call_count), (0, "confirmed", 200, 1), err)
        with mock.patch.object(install, "reload_if_loaded", return_value={}), \
                mock.patch.object(install, "READY_WAIT_SECONDS", 0), \
                mock.patch.object(install, "service_pid", side_effect=pids(100, 100)):
            c, out, err = self.ainstall(start=True)
        self.assertEqual((c, out["outcome"]), (0, "uncertain"), err)

    def test_p1cb_refusals(self):
        import os as _os
        self._adapter_setup()
        self.accept_review()
        outside = self.root / "outside"
        outside.mkdir()
        (outside / "fieldx").mkdir()
        for n in ("__init__.py", "__main__.py"):
            (outside / "fieldx" / n).write_text((self.internal / "fieldx" / n).read_text())
        link = self.internal / "escape"
        _os.symlink(str(outside), str(link))
        stray = self.root / "stray.key"
        stray.write_text("k")
        stray.chmod(0o600)
        py = ("/usr/bin/python3", "-u", "-m", "fieldx")
        cases = [
            ("foreign_executable", dict(argv=("/bin/sh", "-c", "true"))),
            ("bad_argv", dict(argv=("/usr/bin/python3", "-c", "print(1)"))),
            ("bad_argv", dict(argv=("/usr/bin/python3", "-X", "importtime", "-m", "fieldx"))),
            ("not_delegated", dict(cwd=outside)),            # cwd outside the scope's roots
            ("not_delegated", dict(cwd=link)),               # a symlink inside the roots escaping them
            ("not_delegated", dict(label="com.synthetics.adapter.other")),
            ("bad_credential_file", dict(argv=py + ("--credential-file", str(self.keys / "missing.key")))),
            ("bad_credential_file", dict(argv=py + ("--key-file=%s" % stray,))),
        ]
        for code, kw in cases:
            c, out, err = self.ainstall(*kw.pop("argv", ()), **kw)
            self.assertNotEqual(c, 0, (code, out))
            self.assertIn(code, err, code)
        other = self.root / "management" / "other.txt"
        other.write_text(self.review.read_text())
        c, out, err = self.ainstall(review=other)  # same lines, but not the accepted file
        self.assertIn("review_not_accepted", err)
        (self.internal / "fieldx" / "extra.py").write_text("x = 1\n")
        c, out, err = self.ainstall()
        self.assertIn("unreviewed_file", err)
        (self.internal / "fieldx" / "extra.py").unlink()
        (self.internal / "fieldx" / "__main__.py").write_text("print('changed')\n")
        c, out, err = self.ainstall()
        self.assertIn("review_mismatch", err)
        self.assertFalse(self.agents.exists() and any(self.agents.iterdir()))
        refused = {a["data"]["code"] for a in self.audit("codex", "authority.refused")}
        self.assertTrue({"foreign_executable", "bad_argv", "bad_credential_file", "unreviewed_file",
                         "review_mismatch", "review_not_accepted"} <= refused)

    # ---- D17 (P1c-b-r2) ----
    def _review_for(self, names, name="R.txt"):
        import hashlib
        pkg = self.internal / "fieldx"
        review = self.review.parent / name
        review.write_text("".join("%s  tree/fieldx/%s\n" % (hashlib.sha256((pkg / n).read_bytes()).hexdigest(), n)
                                  for n in names))
        self.accept_review(review, hashlib.sha256(review.read_bytes()).hexdigest())
        return review

    def test_d17a_install_only_grantee_settles_own_pending_install(self):
        import plistlib
        self._adapter_setup()
        self.accept_review()
        with mock.patch("synthetics.store.Store.launch_op_settle", side_effect=OSError("disk full")):
            c, out, err = self.ainstall()
        self.assertEqual((c, out["outcome"]), (0, "uncertain"), err)
        with Store(self.homes["codex"]) as s:
            row = s.launch_op(out["pending_op"])
        self.assertEqual((row["action"], row["status"]), ("adapter.install", "pending"))
        self.assertTrue(row["files"])
        settled = self.ok("codex", "authority", "settle", out["pending_op"], session="codex")
        self.assertEqual((settled["status"], settled["outcome"]), ("settled", "confirmed"))
        # a plist whose WorkingDirectory differs from the recorded cwd proves nothing: uncertain
        with mock.patch("synthetics.store.Store.launch_op_settle", side_effect=OSError("disk full")):
            c, out, err = self.ainstall()
        path = self.agents / (self.ALABEL + ".plist")
        with open(path, "rb") as handle:
            plist = plistlib.load(handle)
        plist["WorkingDirectory"] = str(self.root)
        with open(path, "wb") as handle:
            plistlib.dump(plist, handle)
        settled = self.ok("codex", "authority", "settle", out["pending_op"], session="codex")
        self.assertEqual(settled["outcome"], "uncertain")

    def test_d17a_settle_needs_install_scope_covering_row(self):
        self._adapter_setup()
        self.accept_review()
        with mock.patch("synthetics.store.Store.launch_op_settle", side_effect=OSError("disk full")):
            c, out, err = self.ainstall()
        op_id = out["pending_op"]
        self.refused("not_own_ledger", "codex", "authority", "settle", op_id, session="vesper")
        with Store(self.homes["codex"]) as s:  # a row whose ledgered files leave the scope's roots is not covered
            row = s.launch_op(op_id)
            with s._tx():
                s._save("launch_ops", dict(row, files=[str(self.root / "elsewhere.py")]), "test.edit")
        self.refused("not_delegated", "codex", "authority", "settle", op_id, session="codex")
        with Store(self.homes["codex"]) as s:
            self.assertEqual(s.launch_op(op_id)["status"], "pending")

    def test_d17b_nested_module_and_package_script_need_whole_package(self):
        self._adapter_setup()
        pkg = self.internal / "fieldx"
        (pkg / "relay.py").write_text("print('relay')\n")
        only = self._review_for(["relay.py"], "only-relay.txt")
        py = ("/usr/bin/python3", "-u", "-m", "fieldx.relay")
        c, out, err = self.ainstall(*py, review=only)
        self.assertNotEqual(c, 0, out)
        self.assertIn("unreviewed_file", err)       # fieldx/__init__.py runs on import of fieldx.relay
        c, out, err = self.ainstall("/usr/bin/python3", "fieldx/relay.py", review=only)
        self.assertIn("unreviewed_file", err)       # a script inside a package: same rule
        full = self._review_for(["__init__.py", "__main__.py", "relay.py"], "full.txt")
        c, out, err = self.ainstall(*py, review=full)
        self.assertEqual(c, 0, err)
        self.assertEqual(out["outcome"], "confirmed")
        (pkg / "sub").mkdir()
        (pkg / "sub" / "__init__.py").write_text("")
        (pkg / "sub" / "deep.py").write_text("x = 1\n")
        c, out, err = self.ainstall("/usr/bin/python3", "-m", "fieldx.sub.deep", review=full)
        self.assertIn("unreviewed_file", err)       # nested module: every *.py of the top package

    def test_d17c_recheck_between_plan_and_effect(self):
        from synthetics import install, authority as auth
        self._adapter_setup()
        self.accept_review()
        real = install.adapter_plan_within

        def change_byte(plan, scope):
            (self.internal / "fieldx" / "__main__.py").write_text("print('evil')\n")
            return real(plan, scope)
        with mock.patch.object(install, "adapter_plan_within", side_effect=change_byte):
            c, out, err = self.ainstall()
        self.assertNotEqual(c, 0, out)
        self.assertIn("review_mismatch", err)
        self.assertFalse(self.agents.exists() and any(self.agents.iterdir()))
        (self.internal / "fieldx" / "__main__.py").write_text("print('relay')\n")

        def retract(plan, scope):
            p = mock.patch.object(auth, "adapter_reviews", return_value=[])
            p.start()
            self.addCleanup(p.stop)
            return real(plan, scope)
        with mock.patch.object(install, "adapter_plan_within", side_effect=retract):
            c, out, err = self.ainstall()
        self.assertNotEqual(c, 0, out)
        self.assertIn("review_not_accepted", err)
        self.assertFalse(self.agents.exists() and any(self.agents.iterdir()))
        with Store(self.homes["codex"]) as s:
            self.assertEqual([o["outcome"] for o in s.launch_ops(pending_only=False)], ["failed", "failed"])

    def test_d17d_file_added_to_package_between_plan_and_effect(self):
        from synthetics import install
        self._adapter_setup()
        self.accept_review()
        real = install.adapter_plan_within
        extra = self.internal / "fieldx" / "sneak.py"

        def add_sibling(plan, scope):
            extra.write_text("print('unreviewed')\n")   # every planned sha still matches
            return real(plan, scope)
        with mock.patch.object(install, "adapter_plan_within", side_effect=add_sibling):
            c, out, err = self.ainstall()
        self.assertNotEqual(c, 0, out)
        self.assertIn("review_mismatch", err)
        self.assertIn("sneak.py", err)
        self.assertFalse(self.agents.exists() and any(self.agents.iterdir()))
        extra.unlink()
        c, out, err = self.ainstall()
        self.assertEqual(c, 0, err)

    def test_p1cb_reviews_policy_list_writer_and_retract(self):
        self._adapter_setup()
        c, out, err = self.policy("adapter.reviews", {"sums_path": "rel/SUMS", "sums_sha256": self.review_sha,
                                                      "review_ref": "r"})
        self.assertIn("bad_policy", err)
        c, out, err = self.policy("adapter.reviews", {"sums_path": str(self.review), "sums_sha256": "ABC",
                                                      "review_ref": "r"})
        self.assertIn("bad_policy", err)
        first = self.accept_review()
        second = self.accept_review(path=self.root / "management" / "M98" / "SHA256SUMS.txt", sha="0" * 64)
        self.assertEqual([r["id"] for r in authority.adapter_reviews(self.company())], [first, second])
        # list semantics: a newer entry does not displace an older accepted review
        c, out, err = self.ainstall()
        self.assertEqual(c, 0, err)
        # a session writes/retracts the policy only while holding adapter.install (and the company write grant)
        c, out, err = self.policy("adapter.reviews", {"sums_path": str(self.review), "sums_sha256": self.review_sha,
                                                      "review_ref": "r"}, actor="vesper")
        self.assertNotEqual(c, 0, out)
        self.ok("codex", "authority", "retract", first, "--reason", "superseded", "--by", "david-desktop")
        self.assertEqual([r["id"] for r in authority.adapter_reviews(self.company())], [second])
        c, out, err = self.ainstall()
        self.assertIn("review_not_accepted", err)

    def test_p1cb_owner_cwd_and_default_root(self):
        import plistlib
        from synthetics import install
        agents = self.root / "agents"
        self.ok("codex", "adapter", "install", "--label", "com.synthetics.adapter.a", "--launch-agents-dir",
                str(agents), "--no-start", "--", "/usr/bin/true")
        self.ok("codex", "adapter", "install", "--label", "com.synthetics.adapter.b", "--launch-agents-dir",
                str(agents), "--no-start", "--cwd", str(self.root), "--", "/usr/bin/true")
        with open(agents / "com.synthetics.adapter.a.plist", "rb") as h:
            self.assertEqual(plistlib.load(h)["WorkingDirectory"], str(install.ROOT))
        with open(agents / "com.synthetics.adapter.b.plist", "rb") as h:
            self.assertEqual(plistlib.load(h)["WorkingDirectory"], str(self.root.resolve()))

    def test_p1cb_g1_reload_g2_interpreter_g3_unload_wait(self):
        import plistlib
        from synthetics import install
        # G1: a loaded label is booted out, waited for and bootstrapped with the new plist
        path = self.root / "com.synthetics.worker.x.plist"
        with mock.patch.object(install, "loaded", side_effect=[True, True, False]), \
                mock.patch("synthetics.service.stop_launch_agent") as stop, \
                mock.patch("synthetics.service.bootstrap_launch_agent", return_value={"label": "x"}) as boot, \
                mock.patch("time.sleep"):
            out = install.reload_if_loaded(path)
        self.assertEqual((stop.call_count, boot.call_count, out["reloaded"]), (1, 1, True))
        with mock.patch.object(install, "loaded", return_value=True), \
                mock.patch("synthetics.service.stop_launch_agent"), \
                mock.patch("synthetics.service.bootstrap_launch_agent") as boot, \
                mock.patch.object(install, "UNLOAD_WAIT_SECONDS", 0):
            with self.assertRaises(RuntimeError):
                install.reload_if_loaded(path)
        self.assertEqual(boot.call_count, 0)
        with mock.patch.object(install, "loaded", return_value=False), \
                mock.patch.object(install, "start_if_needed", return_value={"started": 1}) as sin:
            self.assertEqual(install.reload_if_loaded(path), {"started": 1})
        # G3: stop is judged after a bounded wait for the unload
        args = mock.Mock(no_start=False, never_loaded=False)
        with mock.patch.object(install, "loaded", side_effect=[True, True, False]), mock.patch("time.sleep"):
            self.assertEqual(install._outcome("stop", "l", path, args, {}), "confirmed")
        with mock.patch.object(install, "loaded", return_value=True), \
                mock.patch.object(install, "UNLOAD_WAIT_SECONDS", 0):
            self.assertEqual(install._outcome("stop", "l", path, args, {}), "uncertain")
        # G2: a delegated worker install keeps the installed plist's interpreter
        self._worker_setup()
        label = install.worker_label({"id": self.ids["rowan"]})
        install.write_worker(self.homes["rowan"], label, ["/usr/bin/python3", "-u", "-m", "synthetics", "--home",
                                                          str(self.homes["rowan"]), "serve"], self.agents)
        c, out, err = self.winstall()
        self.assertEqual(c, 0, err)
        self.assertEqual(out.get("argv", [out])[0], "/usr/bin/python3", out)

    # ---- M28 D19: label prefix patterns ("a.b.*" on a dot boundary) ----
    def test_d19_label_matches_and_attenuation_unit(self):
        from synthetics import authority, install
        m = authority.label_matches
        for label in ("com.synthetics.adapter.fieldpost", "com.synthetics.adapter.x.y"):
            self.assertTrue(m(label, ["com.synthetics.adapter.*"]), label)
            self.assertTrue(m(label, ["com.synthetics.*"]), label)
            self.assertTrue(install.label_covered(label, ["com.synthetics.adapter.*"]), label)
        for label in ("com.synthetics.adapterX", "com.synthetics.adapter.", "com.synthetics.adapter", "", None):
            self.assertFalse(m(label, ["com.synthetics.adapter.*"]), label)
        self.assertFalse(m("com.syntheticsX.adapter.a", ["com.synthetics.*"]))
        self.assertTrue(m("anything", ["*"]))
        self.assertTrue(m("com.synthetics.adapter.a", ["com.synthetics.adapter.a"]))
        self.assertFalse(m("com.synthetics.adapter.ab", ["com.synthetics.adapter.a"]))
        w = lambda child, parent: authority._within("labels", child, parent)
        self.assertTrue(w(["com.synthetics.adapter.*"], ["com.synthetics.*"]))
        self.assertTrue(w(["com.synthetics.adapter.fieldx"], ["com.synthetics.adapter.*"]))
        self.assertTrue(w(["com.synthetics.adapter.*"], ["com.synthetics.adapter.*"]))
        self.assertTrue(w(["com.synthetics.adapter.*"], ["*"]))
        self.assertFalse(w(["com.synthetics.*"], ["com.synthetics.adapter.*"]))
        self.assertFalse(w(["com.synthetics.adapterX.*"], ["com.synthetics.adapter.*"]))
        self.assertFalse(w(["com.synthetics.adapter.*"], ["com.synthetics.adapter.fieldx"]))
        self.assertFalse(w(["*"], ["*"]))
        for bad in (["com.synthetics.adapter*"], ["com.*.adapter.x"], [".*"], ["a.*.*"]):
            with self.assertRaises(authority.AuthorityError, msg=bad) as cm:
                authority.parse_scope(["adapter.manage"], {"adapter.manage": {"labels": bad}})
            self.assertEqual(cm.exception.code, "bad_scope")
        self.assertEqual(authority.parse_scope(["adapter.manage"], {"adapter.manage": {
            "labels": ["com.synthetics.*"]}})["adapter.manage"]["labels"], ["com.synthetics.*"])

    def test_d19_live_install_root_shape(self):
        """co-5857f0aa shape: adapter.install labels ["com.synthetics.adapter.*"] covers a real label; the
        D17a settle of its own ledgered install uses the same pattern."""
        self._adapter_setup(labels=["com.synthetics.adapter.*"])
        self.accept_review()
        c, out, err = self.ainstall()
        self.assertEqual((c, out.get("outcome")), (0, "confirmed"), err)
        c, out, err = self.cli("codex", "authority", "check", "adapter.install", "--item",
                               "com.synthetics.adapterX", session="codex")
        self.assertNotEqual(c, 0, out)
        self.assertIn("not_delegated", err)
        self.ok("codex", "authority", "check", "adapter.install", "--item", self.ALABEL, session="codex")
        from synthetics import install
        with mock.patch.object(install, "ledger_settle", return_value=False), \
                mock.patch.object(install, "_key_dir", return_value=self.keys):
            c, out, err = self.ainstall()
        self.assertEqual(c, 0, err)
        op = out["pending_op"]
        settled = self.ok("codex", "authority", "settle", op, session="codex")
        self.assertEqual(settled["status"], "settled")

    def test_d19_live_manage_root_shape(self):
        """co-5d4d5fac shape: adapter.manage labels ["com.synthetics.*"] restarts an existing adapter label."""
        label = "com.synthetics.adapter.postern"
        agents = self.root / "agents"
        A = ("--launch-agents-dir", str(agents))
        self.ok("codex", "adapter", "install", "--label", label, *A, "--no-start", "--", "/usr/bin/true")
        self.root_delegation(actions=["adapter.manage"], scope={"adapter.manage": {"labels": ["com.synthetics.*"]}})
        with mock.patch("synthetics.install._control", return_value={}) as ctl, \
                mock.patch("synthetics.install.READY_WAIT_SECONDS", 0), \
                mock.patch("synthetics.install.service_pid", side_effect=pids(100, 200)):
            out = self.ok("codex", "adapter", "restart", "--label", label, *A, session="codex")
        self.assertEqual((out["outcome"], ctl.call_count), ("confirmed", 1))
        self.ok("codex", "authority", "check", "adapter.manage", "--item", label, session="codex")
        c, out, err = self.cli("codex", "authority", "check", "adapter.manage", "--item", "com.syntheticsX.a",
                               session="codex")
        self.assertIn("not_delegated", err)

    def test_d19_child_narrowing_allowed_widening_refused(self):
        root = self.root_delegation(actions=["adapter.install", "delegation.grant"], scope={"adapter.install": {
            "labels": ["com.synthetics.adapter.*"], "roots": [str(self.root)]}})
        base = ["codex", "authority", "delegate", "--parent", root, "--action", "adapter.install",
                "--grantee", self.ids["vesper"]]
        sc = lambda labels: json.dumps({"adapter.install": {"labels": labels, "roots": [str(self.root)]}})
        for labels in (["com.synthetics.adapter.fieldx.*"], ["com.synthetics.adapter.fieldx"],
                       ["com.synthetics.adapter.*"]):
            self.ok(*base, "--scope", sc(labels), session="codex")
        for labels in (["com.synthetics.*"], ["*"], ["com.synthetics.adapterX.*"], ["com.other.adapter.x"]):
            self.refused("exceeds_parent", *base, "--scope", sc(labels), session="codex")
        self.refused("bad_scope", *base, "--scope", sc(["com.synthetics.adapter*"]), session="codex")

    # ---- PUB-1 G3: company host rules reach every member home's system prompt ----
    HOST_RULE = "- Do not modify /srv/example-team/protected-checkout."

    def test_g3_host_rules_policy_reaches_member_homes(self):
        from synthetics import brain
        shipped = (Path(brain.__file__).parent / "prompts" / "employee.md").read_text()
        for name in self.homes:
            self.assertEqual(brain.system_prompt(self.homes[name]), shipped)
        code, out, err = self.policy("host.rules", {"text": self.HOST_RULE}, actor="codex")
        self.assertNotEqual(code, 0, out)
        self.assertIn("not_delegated", err)
        self.root_delegation()  # runtime.request over "employees"
        code, out, err = self.policy("host.rules", {"text": self.HOST_RULE}, actor="codex")
        self.assertEqual(code, 0, err)
        entry = out["ids"][0]
        self.assertTrue(self.audit("codex", "authority.policy"))
        for name in self.homes:
            prompt = brain.system_prompt(self.homes[name])
            self.assertTrue(prompt.startswith(shipped))
            self.assertIn("Host rules (host-assigned; company policy host.rules %s):\n%s" % (entry, self.HOST_RULE),
                          prompt)
            self.assertNotIn("operator", prompt[len(shipped):])
        command = brain.ClaudeBrain(self.homes["vesper"]).command()
        self.assertEqual(command[command.index("--system-prompt") + 1], brain.system_prompt(self.homes["vesper"]))
        # A home's own prompt.local.md adds to the company rules; it never replaces them.
        (self.homes["rowan"] / brain.HOST_RULES).write_text("- Also keep out of /srv/example-team/stand.\n")
        prompt = brain.system_prompt(self.homes["rowan"])
        self.assertLess(prompt.index(self.HOST_RULE), prompt.index("- Also keep out of /srv/example-team/stand."))
        self.assertIn("Host rules (host-assigned; prompt.local.md in your home):", prompt)
        self.assertNotIn("/srv/example-team/stand", brain.system_prompt(self.homes["vesper"]))
        # Newest active entry wins; retracting it restores the previous one.
        code, out, err = self.policy("host.rules", {"text": "- Newer rule."}, actor="codex", key="second")
        self.assertEqual(code, 0, err)
        second = out["ids"][0]
        self.assertIn("- Newer rule.", brain.system_prompt(self.homes["vesper"]))
        self.assertNotIn(self.HOST_RULE, brain.system_prompt(self.homes["vesper"]))
        self.ok("codex", "authority", "retract", second, "--reason", "undo", "--by", "david-desktop")
        self.assertIn(self.HOST_RULE, brain.system_prompt(self.homes["vesper"]))

    def test_g3_system_prompt_cli_is_read_only(self):
        from synthetics import brain
        self.assertEqual(self.policy("host.rules", {"text": self.HOST_RULE})[0], 0)  # owner write
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["--home", str(self.homes["rowan"]), "system-prompt"]), 0)
        self.assertEqual(out.getvalue(), brain.system_prompt(self.homes["rowan"]))
        fresh = self.root / "not-a-home-yet"
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(main(["--home", str(fresh), "system-prompt"]), 0)
        self.assertIn(self.HOST_RULE, out.getvalue())
        self.assertFalse(fresh.exists())  # never opened a Store

    def test_g3_host_rules_writer_scope_and_bad_policy(self):
        self.root_delegation(scope=dict(SCOPE_ALL, **{"runtime.request": {"homes": [self.ids["rowan"]]}}))
        code, out, err = self.policy("host.rules", {"text": self.HOST_RULE}, actor="codex")
        self.assertNotEqual(code, 0, out)
        self.assertIn("not_delegated", err)  # rules reach every home: a named-homes runtime.request is not enough
        for body in ({"text": "  "}, {"text": "x" * 8193}, {"text": "ok", "homes": ["rowan"]}, {}):
            code, out, err = self.policy("host.rules", body)
            self.assertNotEqual(code, 0, (body, out))
            self.assertIn("bad_policy", err)

    def test_g3_unreadable_company_log_fails_closed(self):
        from synthetics import brain, roster
        path = roster.company_path(self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        for suffix in ("-wal", "-shm"):
            Path(str(path) + suffix).unlink(missing_ok=True)
        path.write_bytes(b"not a sqlite database" * 100)
        with self.assertRaises(brain.BrainError) as caught:
            brain.system_prompt(self.homes["vesper"])
        self.assertIn("host rules unreadable", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
