"""#15 Byline (slice A, Codex event-29e08672): owner-set attribution, opt-in per scope.

Guards (pass before and after): with no attribution.json the system prompt and the Telegram send
parameters and receipts are byte-identical to before Byline. Behaviour tests fail on the base tree.
"""
import contextlib
import hashlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from synthetics import authority, brain
from synthetics.cli import main
from synthetics.store import Store
from synthetics.telegram import MAX_MESSAGE_LENGTH, OutboxDelivery, TelegramChannelState

SCOPED = {"v": 1, "scopes": {"repos": ["github.com/noteling/synthetic"], "channels": ["telegram"]}}


def _clean_env(**extra):
    env = {k: v for k, v in os.environ.items() if k not in (authority.SESSION_ENV, authority.HOME_ENV)}
    env.update(extra)
    return env


class _Client:
    def __init__(self):
        self.calls = []

    def send_message(self, chat_id, text, reply_to_message_id=None):
        self.calls.append({"chat_id": chat_id, "text": text})
        return {"message_id": 10 * len(self.calls)}


class _ChannelStore:
    """Outbox + channel contract double with a real home path (so the adapter can find attribution.json)."""

    def __init__(self, home, outbox):
        self.home, self._outbox, self.acks = home, outbox, []

    def outbox_list(self):
        return list(self._outbox)

    def channel_claim(self, credential, message_id, instance=None):
        return {"result": "claimed"}

    def channel_ack(self, credential, message_id, outcome, receipt=None, retryable=False, detail=None, instance=None):
        self.acks.append({"message_id": message_id, "outcome": outcome, "receipt": receipt})


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.home = (self.root / "vesper").resolve()
        with Store(self.home) as s:
            self.eid = s.initialize("Vesper", "maintainer")["id"]
            self.run = s.run_begin()["id"]
        self.env_patch = mock.patch.dict(os.environ, _clean_env(), clear=True)
        self.env_patch.start()

    def tearDown(self):
        self.env_patch.stop()
        self.tmp.cleanup()

    def configure(self, data=SCOPED):
        (self.home / "attribution.json").write_text(json.dumps(data))

    def cli(self, *argv, session=False):
        env = _clean_env(**({authority.SESSION_ENV: self.run, authority.HOME_ENV: str(self.home)} if session else {}))
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.dict(os.environ, env, clear=True), contextlib.redirect_stdout(out), \
                contextlib.redirect_stderr(err):
            code = main(["--home", str(self.home), *argv])
        return code, out.getvalue(), err.getvalue()

    def deliver(self, body, credential=b"k" * 32):
        state = TelegramChannelState(self.root / "tg.json")
        state.bot_id, state.bot_username, state.paired = 99, "bot", True
        state.bound_user_id = state.bound_chat_id = 42
        state.outbox_baseline_at = ""
        state.save()
        client = _Client()
        store = _ChannelStore(self.home, [{"id": "message-1", "to": "david", "body": body, "kind": "message",
                                           "question_id": None, "created_at": "2026-11-19T00:00:00Z",
                                           "thread": None}])
        od = OutboxDelivery(state, "david", client, credential=credential)
        od.process_outbox(store)
        return client, store, state


class NoConfigGuards(Base):
    """C: no config → byte-identical prompt and delivery params (guards; pass on the base tree too)."""

    def test_prompt_is_shipped_prompt_plus_host_rules(self):
        expected = (Path(brain.__file__).parent / "prompts" / "employee.md").read_text()
        for label, text in brain.host_rules(self.home):
            expected += "\n\nHost rules (" + label + "):\n" + text + "\n"
        self.assertEqual(brain.system_prompt(self.home), expected)

    def test_telegram_send_and_receipt_unchanged(self):
        client, store, state = self.deliver("hello")
        self.assertEqual(client.calls, [{"chat_id": 42, "text": "hello"}])
        self.assertEqual(store.acks[0]["receipt"], "telegram:42:10")
        self.assertNotIn("footer", state.get_delivery("message-1"))

    def test_long_body_split_unchanged(self):
        body = ("x" * 100 + "\n") * 60
        client, _, _ = self.deliver(body)
        from synthetics.telegram import split_text
        self.assertEqual([c["text"] for c in client.calls], split_text(body, MAX_MESSAGE_LENGTH))


class Configured(Base):
    def test_prompt_block_names_trailer_line_and_scopes(self):
        before = brain.system_prompt(self.home)
        self.configure()
        after = brain.system_prompt(self.home)
        self.assertTrue(after.startswith(before))
        block = after[len(before):]
        self.assertIn("Synthetic-Employee: Vesper (%s)" % self.eid, block)
        self.assertIn("github.com/noteling/synthetic", block)
        self.assertIn("— Vesper (synthetic employee)", block)
        self.assertIn("never change the commit author or committer", block)

    def test_invalid_config_leaves_prompt_and_show_reports_it(self):
        before = brain.system_prompt(self.home)
        self.configure({"v": 1, "scopes": {"channels": ["email"]}})
        self.assertEqual(brain.system_prompt(self.home), before)
        code, out, _ = self.cli("byline", "show")
        self.assertEqual(code, 0)
        self.assertFalse(json.loads(out)["valid"])

    def test_validate_refuses_unknown_keys_and_multiline_line(self):
        from synthetics import byline
        for bad in ({"v": 1, "scope": {}}, {"v": 2}, {"v": 1, "line": "a\nb"}, {"v": 1, "trailer": "has space"},
                    {"v": 1, "line": "x" * 201}):
            with self.assertRaises(byline.BylineError, msg=bad):
                byline.validate(bad)

    def test_trailer_command(self):
        code, out, _ = self.cli("byline", "trailer")
        self.assertEqual((code, out), (0, ""))
        self.configure()
        code, out, _ = self.cli("byline", "trailer")
        self.assertEqual(out, "Synthetic-Employee: Vesper (%s)\n" % self.eid)


class Footer(Base):
    def test_footer_on_in_scope_channel_with_receipt_sha(self):
        self.configure()
        client, store, state = self.deliver("hello")
        footer = "— Vesper (synthetic employee)"
        sha8 = hashlib.sha256(footer.encode()).hexdigest()[:8]
        self.assertEqual(client.calls[0]["text"], "hello\n\n" + footer)
        self.assertEqual(state.get_delivery("message-1")["footer"], sha8)
        self.assertEqual(store.acks[0]["receipt"], "telegram:42:10;footer:" + sha8)
        self.assertEqual(store.outbox_list()[0]["body"], "hello")  # stored body unchanged

    def test_footer_only_on_last_part_and_parts_within_limit(self):
        self.configure()
        body = "y" * (MAX_MESSAGE_LENGTH - 5)  # fits alone, not with the footer
        client, _, state = self.deliver(body)
        texts = [c["text"] for c in client.calls]
        self.assertEqual(len(texts), 2)
        self.assertTrue(all(len(t) <= MAX_MESSAGE_LENGTH for t in texts))
        self.assertNotIn("Vesper", texts[0])
        self.assertTrue(texts[-1].endswith("\n\n— Vesper (synthetic employee)"))
        self.assertEqual("".join(texts).replace("\n\n— Vesper (synthetic employee)", ""), body)
        self.assertEqual(state.get_delivery("message-1")["parts_total"], 2)

    def test_channel_not_in_scope_gets_no_footer(self):
        self.configure({"v": 1, "scopes": {"repos": ["github.com/noteling/synthetic"]}})
        client, store, _ = self.deliver("hello")
        self.assertEqual(client.calls[0]["text"], "hello")
        self.assertEqual(store.acks[0]["receipt"], "telegram:42:10")


class Hook(Base):
    def git(self, repo, *argv, env=None, check=True):
        return subprocess.run(["git", "-C", str(repo), *argv], capture_output=True, text=True, check=check,
                              env=env or _clean_env())

    def repo(self, name, remote):
        repo = self.home / "workspace" / name
        repo.mkdir(parents=True)
        self.git(repo, "init", "-q")
        if remote:
            self.git(repo, "remote", "add", "origin", remote)
        self.git(repo, "config", "user.name", "david")
        self.git(repo, "config", "user.email", "david@example.invalid")
        self.git(repo, "config", "commit.gpgsign", "false")
        return repo

    def commit(self, repo, msg, session=True, extra=()):
        (repo / ("f%d" % len(list(repo.iterdir())))).write_text(msg)
        self.git(repo, "add", "-A")
        env = _clean_env(**({authority.SESSION_ENV: self.run, authority.HOME_ENV: str(self.home)}
                            if session else {}))
        self.git(repo, "commit", "-q", "-m", msg, *extra, env=env)
        return self.git(repo, "log", "-1", "--format=%an|%ae|%cn|%ce%n%B").stdout

    def install(self, repo, session=False):
        code, out, err = self.cli("byline", "hook", "install", "--repo", str(repo), session=session)
        self.assertEqual(code, 0, err)
        return json.loads(out)

    def test_trailer_once_in_session_for_in_scope_remote_author_unchanged(self):
        self.configure()
        repo = self.repo("syn", "git@github.com:noteling/synthetic.git")
        self.install(repo)
        trailer = "Synthetic-Employee: Vesper (%s)" % self.eid
        log = self.commit(repo, "one\n\nbody")
        self.assertEqual(log.splitlines()[0], "david|david@example.invalid|david|david@example.invalid")
        self.assertEqual(log.count(trailer), 1)
        log = self.commit(repo, "two\n\n" + trailer)  # already present: not added twice
        self.assertEqual(log.count(trailer), 1)
        self.assertEqual(self.git(repo, "config", "user.name").stdout.strip(), "david")
        self.assertEqual(self.git(repo, "config", "user.email").stdout.strip(), "david@example.invalid")

    def test_no_trailer_outside_session_other_remote_or_other_home(self):
        self.configure()
        repo = self.repo("syn", "https://github.com/noteling/synthetic.git")
        self.install(repo)
        self.assertNotIn("Synthetic-Employee", self.commit(repo, "owner commit", session=False))
        other = self.repo("stand", "https://github.com/someone/stand.git")
        self.install(other)
        self.assertNotIn("Synthetic-Employee", self.commit(other, "out of scope"))
        (self.home / "attribution.json").unlink()  # config removed: hook stays, adds nothing
        self.assertNotIn("Synthetic-Employee", self.commit(repo, "after clear"))

    def test_hook_never_changes_author_or_committer_vs_no_hook(self):
        self.configure()
        plain, hooked = self.repo("plain", "git@github.com:noteling/synthetic.git"), \
            self.repo("hooked", "git@github.com:noteling/synthetic.git")
        self.install(hooked)
        a = self.commit(plain, "same").splitlines()[0]
        b = self.commit(hooked, "same").splitlines()[0]
        self.assertEqual(a, b)

    def test_refuses_to_replace_a_foreign_hook(self):
        self.configure()
        repo = self.repo("syn", "git@github.com:noteling/synthetic.git")
        (repo / ".git" / "hooks" / "prepare-commit-msg").write_text("#!/bin/sh\nexit 0\n")
        code, _, err = self.cli("byline", "hook", "install", "--repo", str(repo))
        self.assertNotEqual(code, 0)
        self.assertIn("not written by Byline", err)

    def test_session_installs_only_inside_its_own_home(self):
        self.configure()
        inside = self.repo("syn", "git@github.com:noteling/synthetic.git")
        self.install(inside, session=True)
        outside = self.root / "elsewhere"
        outside.mkdir()
        self.git(outside, "init", "-q")
        code, _, err = self.cli("byline", "hook", "install", "--repo", str(outside), session=True)
        self.assertNotEqual(code, 0)
        self.assertIn("own_home_only", err)
        self.assertFalse((outside / ".git" / "hooks" / "prepare-commit-msg").exists())


class OwnerOnly(Base):
    def test_session_cannot_write_or_clear_attribution(self):
        cfg = self.root / "cfg.json"
        cfg.write_text(json.dumps(SCOPED))
        code, _, err = self.cli("byline", "set", "--file", str(cfg), session=True)
        self.assertNotEqual(code, 0)
        self.assertIn("owner_only", err)
        self.assertFalse((self.home / "attribution.json").exists())
        self.configure()
        code, _, err = self.cli("byline", "clear", session=True)
        self.assertNotEqual(code, 0)
        self.assertIn("owner_only", err)
        self.assertTrue((self.home / "attribution.json").exists())
        with Store(self.home) as s:
            rows = [r for r in s.audit_list() if r.get("action") == "authority.refused"]
        self.assertEqual(sorted(json.loads(json.dumps(r["data"]))["what"] for r in rows),
                         ["byline clear", "byline set"])

    def test_owner_sets_validated_0600_and_clears(self):
        cfg = self.root / "cfg.json"
        cfg.write_text(json.dumps(SCOPED))
        code, out, err = self.cli("byline", "set", "--file", str(cfg))
        self.assertEqual(code, 0, err)
        p = self.home / "attribution.json"
        self.assertEqual(p.stat().st_mode & 0o777, 0o600)
        cfg.write_text(json.dumps({"v": 1, "scopes": {"channels": ["sms"]}}))
        code, _, err = self.cli("byline", "set", "--file", str(cfg))
        self.assertNotEqual(code, 0)
        self.assertEqual(json.loads(p.read_text()), SCOPED)  # invalid set leaves the old file
        code, _, _ = self.cli("byline", "clear")
        self.assertEqual(code, 0)
        self.assertFalse(p.exists())


if __name__ == "__main__":
    unittest.main()
