"""Postern: the maintained reference adapter for the channel contract v1 (M25 §5).

A directory bridge. Anything that can write and read files (a mail gateway, a
ticket system, a shell script) can talk to employees through it:

* inbound: drop ``<inbox>/<name>.jsonl`` (write a temporary name, then rename).
  Each line is a contract inbound line (docs/channel-contract.md) plus an
  optional ``"home"`` naming one of the configured homes (its path or its
  directory name; optional when only one home is configured). Results go to
  ``<inbox>/<name>.result.jsonl`` and the input moves to ``<inbox>/done/``.
* outbound: every routed pending message is claimed, written atomically to
  ``<outbox>/<message_id>.json`` and acked ``delivered`` with receipt
  ``file:<sha256>``.
* recovery: claims left by an earlier Postern instance become ``uncertain``.
  Postern reconciles them from evidence: if the outbox file exists and names
  the message, ``delivered``; otherwise ``failed``, which returns the message to
  pending as a recorded decision. Nothing is ever resent blindly.

Postern never imports the Store. It drives ``synthetics --home H channel ...``
exactly as a third-party adapter would.
"""
import hashlib
import json
import os
import stat
import subprocess
import sys
import time
import uuid
from pathlib import Path

VERSION = 1
RESULT_SUFFIX = ".result.jsonl"


class PosternError(RuntimeError):
    pass


def load_config(path):
    path = Path(path).expanduser()
    config = json.loads(path.read_text())
    if not isinstance(config, dict) or config.get("v") != VERSION:
        raise PosternError("Postern config %s must be a JSON object with \"v\": 1." % path)
    for field in ("key_file", "homes", "inbox", "outbox"):
        if not config.get(field):
            raise PosternError("Postern config %s is missing %r." % (path, field))
    if not isinstance(config["homes"], list):
        raise PosternError("Postern config \"homes\" must be a list of employee home paths.")
    base = path.parent
    resolve = lambda value: (base / Path(value).expanduser()).resolve()
    config["key_file"] = str(resolve(config["key_file"]))
    config["homes"] = [str(resolve(home)) for home in config["homes"]]
    config["inbox"] = resolve(config["inbox"])
    config["outbox"] = resolve(config["outbox"])
    config.setdefault("channel", "postern")
    return config


def check_inbox(inbox):
    """C5: anyone who can write the inbox can speak with this channel's authority."""
    inbox = Path(inbox)
    inbox.mkdir(parents=True, exist_ok=True)
    mode = inbox.stat().st_mode
    if mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PosternError("Refusing to start: inbox %s is group- or world-writable (mode %o). Anyone who can "
                           "write it could post as this channel. Run: chmod go-w %s" % (inbox, stat.S_IMODE(mode),
                                                                                       inbox))


class Postern:
    def __init__(self, config, command=None, instance=None):
        self.config = config
        self.command = command or config.get("command") or [sys.executable, "-m", "synthetics"]
        self.instance = instance or "postern-" + uuid.uuid4().hex[:12]
        self.inbox = Path(config["inbox"])
        self.outbox = Path(config["outbox"])
        self.homes = list(config["homes"])
        check_inbox(self.inbox)
        self.outbox.mkdir(parents=True, exist_ok=True)
        (self.inbox / "done").mkdir(exist_ok=True)

    # -- contract plumbing -------------------------------------------------
    def _contract(self, home, name, *args, lines=None):
        argv = list(self.command) + ["--home", home, "channel", name, "--key-file", self.config["key_file"]]
        if name != "reconcile":
            argv += ["--instance", self.instance]
        argv += list(args)
        stdin = "".join(json.dumps(item, ensure_ascii=False) + "\n" for item in lines) if lines is not None else None
        env = {k: v for k, v in os.environ.items() if k != "SYNTHETICS_SESSION_RUN"}
        try:
            out = subprocess.run(argv, input=stdin, capture_output=True, text=True, env=env, timeout=120)
        except subprocess.TimeoutExpired:
            raise PosternError("channel %s on %s timed out after 120 s" % (name, home))
        # Split on "\n" only: str.splitlines() also splits on U+2028/U+2029/U+0085, which json.dumps(ensure_ascii=False)
        # leaves raw inside strings (issue #4).
        try:
            rows = [json.loads(text) for text in out.stdout.split("\n") if text.strip()]
        except ValueError as exc:
            raise PosternError("channel %s on %s printed a line that is not JSON: %s" % (name, home, exc))
        if out.returncode != 0:
            error = rows[0].get("error") if rows and isinstance(rows[0], dict) else None
            raise PosternError("channel %s on %s failed (exit %s): %s" % (
                name, home, out.returncode, error or out.stderr.strip()[-400:]))
        return rows

    def _home_for(self, item):
        wanted = item.get("home") if isinstance(item, dict) else None
        if wanted is None:
            if len(self.homes) == 1:
                return self.homes[0]
            return None
        for home in self.homes:
            if wanted == home or wanted == Path(home).name:
                return home
        return None

    # -- inbound -------------------------------------------------------------
    def inbound_files(self):
        return sorted(p for p in self.inbox.glob("*.jsonl")
                      if p.is_file() and not p.name.endswith(RESULT_SUFFIX))

    def process_inbound(self, path):
        try:
            text = path.read_bytes().decode("utf-8")
        except UnicodeDecodeError as exc:
            # Undecodable input can never be fed: answer it with one file-level rejection and move it to done/,
            # like a file of bad lines, so it does not replay forever (issue #4).
            return self._finish(path, [dict(v=VERSION, external_id=None, result="rejected", event_id=None,
                                            error=dict(code="bad_file", message="not UTF-8: %s" % exc))])
        raw = text.split("\n")
        if raw and raw[-1] == "":
            raw.pop()  # the newline that ends the last line
        results = [None] * len(raw)
        batches = {}
        for index, text in enumerate(raw):
            if not text.strip():
                results[index] = dict(v=VERSION, external_id=None, result="rejected", event_id=None,
                                      error=dict(code="bad_line", message="empty line"))
                continue
            try:
                item = json.loads(text)
            except ValueError as exc:
                item, home = None, None
                results[index] = dict(v=VERSION, external_id=None, result="rejected", event_id=None,
                                      error=dict(code="bad_line", message="not JSON: %s" % exc))
                continue
            if not isinstance(item, dict):
                results[index] = dict(v=VERSION, external_id=None, result="rejected", event_id=None,
                                      error=dict(code="bad_line", message="not a JSON object"))
                continue
            home = self._home_for(item)
            if home is None:
                results[index] = dict(v=VERSION, external_id=item.get("external_id") if isinstance(item, dict)
                                      else None, result="rejected", event_id=None,
                                      error=dict(code="not_routed", message="line names no configured home"))
                continue
            line = {k: v for k, v in item.items() if k != "home"}
            batches.setdefault(home, []).append((index, line))
        for home, entries in batches.items():
            rows = self._contract(home, "inbound", lines=[line for _, line in entries])
            if len(rows) != len(entries):
                raise PosternError("channel inbound on %s returned %d results for %d lines" % (
                    home, len(rows), len(entries)))
            for (index, _), row in zip(entries, rows):
                row["home"] = Path(home).name
                results[index] = row
        return self._finish(path, results)

    def _finish(self, path, results):
        result_path = self.inbox / (path.name[:-len(".jsonl")] + RESULT_SUFFIX)
        _atomic_write(result_path, "".join(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n"
                                           for row in results).encode())
        # Only after the results are durable does the input leave the inbox. A crash before
        # this point replays the file, and the contract's dedup returns "duplicate".
        os.replace(path, self.inbox / "done" / path.name)
        return results

    # -- outbound ------------------------------------------------------------
    def _outbox_path(self, message_id):
        if not message_id.startswith("message-") or "/" in message_id or os.sep in message_id:
            raise PosternError("refusing unsafe message id %r" % message_id)
        return self.outbox / (message_id + ".json")

    def deliver(self, home):
        delivered = []
        for row in self._contract(home, "pending"):
            message_id = row["message_id"]
            claimed = self._contract(home, "claim", "--message", message_id)
            if not claimed or claimed[0].get("result") == "rejected":
                continue  # another instance holds it, or it is no longer pending
            record = {k: v for k, v in claimed[0].items() if k not in ("state", "attempt")}
            record["home"] = Path(home).name
            data = (json.dumps(record, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()
            _atomic_write(self._outbox_path(message_id), data)
            ack = dict(v=VERSION, message_id=message_id, outcome="delivered",
                       receipt="file:" + hashlib.sha256(data).hexdigest())
            self._contract(home, "ack", lines=[ack])
            delivered.append(message_id)
        return delivered

    def reconcile(self, home):
        """Settle uncertain claims from evidence in the outbox directory."""
        settled = []
        for row in self._contract(home, "uncertain"):
            message_id = row["message_id"]
            path = self._outbox_path(message_id)
            outcome, receipt, note = "failed", None, "postern: no outbox file; returning to pending"
            if path.exists():
                data = path.read_bytes()
                try:
                    named = json.loads(data.decode()).get("message_id")
                except (ValueError, AttributeError, UnicodeDecodeError):
                    named = None
                if named == message_id:
                    outcome, receipt = "delivered", "file:" + hashlib.sha256(data).hexdigest()
                    note = "postern: outbox file present"
            self._contract(home, "reconcile", "--message", message_id, "--outcome", outcome,
                           *(["--receipt", receipt] if receipt else []), "--note", note,
                           "--by", self.instance)
            settled.append((message_id, outcome))
        return settled

    def cycle(self):
        """One pass. A failure is contained to its file or its home and listed under ``errors``; the other files
        and homes still run (issue #4: one failing home used to block every home). A failed file stays in the
        inbox and is retried next cycle (the contract's dedup answers duplicate for lines already fed)."""
        report = dict(inbound=[], delivered=[], reconciled=[], errors=[])
        for path in self.inbound_files():
            try:
                report["inbound"].append(dict(file=path.name, results=len(self.process_inbound(path))))
            except Exception as exc:  # noqa: BLE001 - contain it to this file
                report["errors"].append(dict(stage="inbound", file=path.name, error=_describe(exc)))
        for home in self.homes:
            name = Path(home).name
            try:
                report["reconciled"] += [dict(home=name, message_id=m, outcome=o) for m, o in self.reconcile(home)]
            except Exception as exc:  # noqa: BLE001 - contain it to this home
                report["errors"].append(dict(stage="reconcile", home=name, error=_describe(exc)))
                continue  # deliver claims new work; not before this home's uncertain claims are settled
            try:
                report["delivered"] += [dict(home=name, message_id=m) for m in self.deliver(home)]
            except Exception as exc:  # noqa: BLE001
                report["errors"].append(dict(stage="deliver", home=name, error=_describe(exc)))
        return report


def _describe(exc):
    text = str(exc) if isinstance(exc, PosternError) else "%s: %s" % (type(exc).__name__, exc)
    return text[-400:]


def _atomic_write(path, data):
    path = Path(path)
    tmp = path.with_name(".%s.%s.tmp" % (path.name, uuid.uuid4().hex[:8]))
    with open(tmp, "wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


def run(config_path, once=False, interval=5.0, out=None):
    out = out or sys.stdout
    postern = Postern(load_config(config_path))
    while True:
        try:
            report = postern.cycle()
            if once or any(report.values()):
                out.write(json.dumps(dict(report, instance=postern.instance), sort_keys=True) + "\n")
                out.flush()
            for error in report["errors"]:
                sys.stderr.write("postern: %s\n" % json.dumps(error, sort_keys=True))
        except Exception as exc:  # noqa: BLE001 - a long-running bridge logs and keeps cycling (issue #4)
            if once:
                raise
            sys.stderr.write("postern: %s\n" % _describe(exc))
        if once:
            return 1 if report["errors"] else 0  # --once: a contained failure still fails the command
        time.sleep(interval)


def add_parser(sub):
    parser = sub.add_parser("postern", help="Postern: reference directory-bridge adapter for the channel contract")
    psub = parser.add_subparsers(dest="postern_command", required=True)
    run_parser = psub.add_parser("run", help="bridge inbox/outbox directories to the configured homes")
    run_parser.add_argument("--config", required=True)
    run_parser.add_argument("--once", action="store_true", help="one cycle, then exit")
    run_parser.add_argument("--interval", type=float, default=5.0)
    return parser
