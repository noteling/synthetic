"""Satchel (P1): extract one employee home into a single archive with secrets masked, and verify an archive.

docs/satchel.md. Owner rulings: archives are not encrypted; secrets and passwords are masked, fail-closed (David,
event-2f2c8899 via Codex); rare migration, no schedule or built-in destination (event-40c88c60). Design:
reviews/extract/P0.md r1 (Codex approved with C1-C3, event-25af8232).

Extract never writes to the home. The snapshot is taken with VACUUM INTO from a read-only connection; the snapshot,
the masking pass and the second VACUUM INTO happen in a private 0700 temp directory on local disk, never under
--out (C1): only the finished, masked tar is moved into --out (temp name + rename). Channel key files, the Telegram
token file and CLI login state are never opened: a pasted channel key is found by hashing candidate tokens and
comparing them with the key_sha256 values already stored in the home's channels table (F7).
"""
from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import re
import shutil
import socket
import sqlite3
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import ledgerline, sqlro

FORMAT = 1
TEMP_PREFIX = "satchel-tmp-"
PARTIAL_PREFIX = ".satchel-partial-"
OWNER_FILE = "owner.pid"
DEFAULT_TIMEOUT = 600.0

# Tables Satchel knows. A table not listed here makes the extract refuse (M4/F9): a table added later is scanned
# only once someone has decided it belongs in an archive.
DATA_TABLES = ("identity", "entities", "memories", "works", "questions", "messages", "reminders", "runs",
               "access_grants", "thread_records", "runtime_requests", "channels", "launch_ops", "glances", "clicks")
OTHER_TABLES = ("events", "idempotency", "audit", "roster_log", "project_bindings", "capture_outbox",
                "coverage_marks", "capture_meta", "sqlite_sequence")
KNOWN_TABLES = frozenset(DATA_TABLES + OTHER_TABLES)

CONFIG_JSON = ("profile.json", "attribution.json", "humans.json", "floor.json", "click.json", "glance.json",
               "runtime.json")
CONFIG_TEXT = ("prompt.local.md",)
EXCLUDED_FILES = {
    "state.sqlite3": "archived as a masked snapshot",
    "state.sqlite3-wal": "SQLite side file (the snapshot includes committed WAL content)",
    "state.sqlite3-shm": "SQLite side file (WAL index, no persistent state)",
    "state.sqlite3-journal": "SQLite side file",
    "runtime.state.json": "per-process state", "runtime.last-good.json": "per-process state",
    "worker.lock": "per-process lock", "telegram.lock": "per-process lock", "usage_limit.json": "per-process state",
    "telegram.json": "adapter config naming a token file; re-installed by the operator",
    "telegram_state.json": "adapter state; re-installed by the operator",
    "SEALED.json": "seal of a moved home", ".DS_Store": "Finder metadata",
}
EXCLUDED_PATTERNS = ((re.compile(r"source_manifest_[0-9a-f]+\.json\Z"), "regenerated source manifest"),)
EXCLUDED_DIRS = {
    "runs": "exported through Ledgerline as runs.jsonl (raw side files never archived)",
    "workspace": "scratch and clones; repos live in their remotes (F2)",
    "shared": "regenerated status", "services": "logs", "roster": "regenerated renders",
    "meetings": "journals archived separately",
}


class SatchelError(ValueError):
    """A named refusal. The extract writes nothing to --out when one is raised."""

    def __init__(self, code, message):
        super().__init__("%s: %s" % (code, message))
        self.code = code


# ---------------------------------------------------------------------------------------------------------
# M2 detectors. Each pattern has a kind; a group named v (when present) is the part replaced, else the whole match.

_EDGE = r"A-Za-z0-9_-"
PATTERNS = (
    ("pem", re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----[\s\S]*?(?:-----END [A-Z ]*PRIVATE KEY-----|\Z)")),
    ("anthropic", re.compile(r"(?<![%s])sk-ant-[A-Za-z0-9_-]{20,}" % _EDGE)),
    ("openai", re.compile(r"(?<![%s])sk-(?:proj-)?[A-Za-z0-9_-]{20,}" % _EDGE)),
    ("github", re.compile(r"(?<![A-Za-z0-9_])(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36,}|github_pat_[A-Za-z0-9_]{22,}")),
    ("aws", re.compile(r"(?<![A-Z0-9])AKIA[0-9A-Z]{16}(?![A-Z0-9])")),
    ("slack", re.compile(r"(?<![%s])xox[abprs]-[A-Za-z0-9-]{10,}" % _EDGE)),
    # C2: lookarounds instead of \b, so a token ending in '-' or '_' is still matched whole.
    ("telegram", re.compile(r"(?<![%s])\d{6,12}:[A-Za-z0-9_-]{35}(?![%s])" % (_EDGE, _EDGE))),
    ("bearer", re.compile(r"(?i)\bauthorization\s*[:=]\s*(?:(?:bearer|basic|token)\s+)?(?P<v>[^\s\"'«]{8,})"
                          r"|\bbearer\s+(?P<w>[A-Za-z0-9._~+/=-]{16,})")),
    ("jwt", re.compile(r"(?<![%s])eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}" % _EDGE)),
    ("userinfo", re.compile(r"[A-Za-z][A-Za-z0-9+.-]*://[^\s/:@«]+:(?P<v>[^\s/@«]+)@")),
    # Only the value is masked; the word stays. Value: a quoted string or the next run of >= 4 characters. A marker
    # never counts as a value, so a masked text scans clean.
    ("assign", re.compile(r"(?i)(?<![A-Za-z0-9])(?:password|passwd|pwd|secret(?:[_-]?key)?|token|api[_-]?key"
                          r"|client[_-]?secret)[\"']?\s*(?:=|:|\bis\b)\s*"
                          r"(?:\"(?P<v>[^\"«\n]{4,})\"|'(?P<w>[^'«\n]{4,})'|(?P<x>[^\s\"'«,;}\]]{4,}))")),
)
_CANDIDATE = re.compile(r"(?<![%s])[A-Za-z0-9_-]{16,}(?![%s])" % (_EDGE, _EDGE))
_HEX64 = re.compile(r"[0-9a-fA-F]{64}")
MARKER = re.compile(r"«masked:([a-z-]+):(\d+)»")


def _span(match):
    for name in ("v", "w", "x"):
        if name in match.re.groupindex and match.group(name) is not None:
            return match.start(name), match.end(name)
    return match.start(), match.end()


def detect(text, channel_hashes=frozenset()):
    """Non-overlapping (start, end, kind) hits in text, earliest first; the longer hit wins an overlap."""
    hits = []
    for kind, pattern in PATTERNS:
        for match in pattern.finditer(text):
            start, end = _span(match)
            if end > start:
                hits.append((start, end, kind))
    if channel_hashes:
        for match in _HEX64.finditer(text):
            if match.group().lower() in channel_hashes:
                hits.append((match.start(), match.end(), "channel-key-hash"))
        for match in _CANDIDATE.finditer(text):
            if hashlib.sha256(match.group().encode("utf-8")).hexdigest() in channel_hashes:
                hits.append((match.start(), match.end(), "channel-key"))
    hits.sort(key=lambda h: (h[0], -(h[1] - h[0])))
    out, end = [], -1
    for hit in hits:
        if hit[0] >= end:
            out.append(hit)
            end = hit[1]
    return out


class Masker:
    """Replaces detector hits with «masked:<kind>:<n>» and records where (never the value nor a hash of it)."""

    def __init__(self, channel_hashes=frozenset()):
        self.channel_hashes = frozenset(h.lower() for h in channel_hashes if isinstance(h, str))
        self.masks = []

    def text(self, text, location):
        hits = detect(text, self.channel_hashes)
        if not hits:
            return text, False
        parts, last = [], 0
        for start, end, kind in hits:
            n = len(self.masks) + 1
            self.masks.append(dict(location, n=n, kind=kind, offset=start, length=end - start))
            parts.append(text[last:start])
            parts.append("«masked:%s:%d»" % (kind, n))
            last = end
        parts.append(text[last:])
        return "".join(parts), True

    def value(self, value, location, path="$"):
        """Walk a parsed JSON value: every string and every dict key. Returns (value, changed)."""
        if isinstance(value, str):
            return self.text(value, dict(location, json_path=path))
        if isinstance(value, list):
            changed, out = False, []
            for index, item in enumerate(value):
                item, hit = self.value(item, location, "%s[%d]" % (path, index))
                out.append(item)
                changed = changed or hit
            return out, changed
        if isinstance(value, dict):
            changed, out = False, {}
            for key, item in value.items():
                new_key, key_hit = self.text(key, dict(location, json_path="%s.<key %s>" % (path, len(out))))
                item, hit = self.value(item, location, "%s.%s" % (path, new_key))
                out[new_key] = item
                changed = changed or key_hit or hit
            return out, changed
        return value, False

    def field(self, raw, location):
        """A stored TEXT value: JSON object/array walked, anything else scanned as text. Returns (new, changed);
        an unchanged value is returned as is (byte-identical, never re-serialised)."""
        parsed = None
        if raw[:1] in ("{", "[") or raw.strip()[:1] in ("{", "["):
            try:
                parsed = json.loads(raw)
            except ValueError:
                parsed = None
        if isinstance(parsed, (dict, list)):
            value, changed = self.value(parsed, location)
            if not changed:
                return raw, False
            return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False), True
        return self.text(raw, location)


# ---------------------------------------------------------------------------------------------------------
# Temp handling (C1)

def _alive(pid):
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _sweep_stale(base):
    """Remove our own satchel temp dirs left by a dead process (a SIGKILLed extract). Returns their names."""
    removed = []
    for entry in Path(base).glob(TEMP_PREFIX + "*"):
        try:
            if not entry.is_dir() or entry.is_symlink() or entry.stat().st_uid != os.getuid():
                continue
            pid = int((entry / OWNER_FILE).read_text().strip())
        except (OSError, ValueError):
            continue
        if not _alive(pid):
            shutil.rmtree(str(entry), ignore_errors=True)
            removed.append(entry.name)
    return removed


@contextlib.contextmanager
def _workdir(base=None):
    base = Path(base or tempfile.gettempdir())
    swept = _sweep_stale(base)
    path = Path(tempfile.mkdtemp(prefix=TEMP_PREFIX, dir=str(base)))
    os.chmod(str(path), 0o700)
    (path / OWNER_FILE).write_text(str(os.getpid()))
    try:
        yield path, swept
    finally:
        shutil.rmtree(str(path), ignore_errors=True)


def _now():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(str(path), "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


# ---------------------------------------------------------------------------------------------------------
# Extract

def inventory(home):
    """(included config names, excluded {name: reason}); refuses an unknown file or directory at the home root."""
    included, excluded, unknown = [], {}, []
    for entry in sorted(Path(home).iterdir(), key=lambda p: p.name):
        name = entry.name
        if entry.is_dir() and not entry.is_symlink():
            if name in EXCLUDED_DIRS:
                excluded[name + "/"] = EXCLUDED_DIRS[name]
            else:
                unknown.append(name + "/")
            continue
        if name in CONFIG_JSON or name in CONFIG_TEXT:
            if entry.is_symlink() or not entry.is_file():
                raise SatchelError("unknown_home_file", "%s is not a regular file" % name)
            included.append(name)
        elif name in EXCLUDED_FILES:
            excluded[name] = EXCLUDED_FILES[name]
        elif any(p.match(name) for p, _ in EXCLUDED_PATTERNS):
            excluded[name] = next(r for p, r in EXCLUDED_PATTERNS if p.match(name))
        else:
            unknown.append(name)
    if unknown:
        raise SatchelError("unknown_home_file", "the home holds files Satchel does not know how to archive or "
                           "exclude: %s; move them out of the home (or extend Satchel) and retry" % ", ".join(unknown))
    return included, excluded


def _snapshot(home, target, timeout):
    conn = sqlro.connect_ro(Path(home) / "state.sqlite3", timeout=min(timeout, 30.0))
    deadline = time.monotonic() + timeout
    conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 10000)
    try:
        conn.execute("VACUUM INTO ?", (str(target),))
    except sqlite3.OperationalError as error:
        if time.monotonic() > deadline:
            raise SatchelError("snapshot_timeout", "VACUUM INTO did not finish within %.0f s" % timeout)
        raise SatchelError("snapshot_failed", str(error))
    finally:
        conn.close()


def _tables(conn):
    return [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def _read_channels(conn):
    """Current channels (for the re-provision list) and every key hash this home ever stored: the current rows plus
    the hashes in channel.registered / channel.rotated audit rows, so a rotated-away hash does not travel either."""
    channels, hashes = [], set()
    for (raw,) in conn.execute("SELECT data FROM channels ORDER BY rowid"):
        try:
            record = json.loads(raw)
        except ValueError:
            raise SatchelError("unreadable_row", "a channels row is not JSON")
        if isinstance(record.get("key_sha256"), str):
            hashes.add(record["key_sha256"].lower())
        channels.append({"name": record.get("id"), "source_prefixes": record.get("source_prefixes"),
                         "routes": record.get("routes"), "registered_by": record.get("registered_by")})
    for (raw,) in conn.execute("SELECT data FROM audit WHERE action IN ('channel.registered', 'channel.rotated')"):
        try:
            record = json.loads(raw)
        except ValueError:
            raise SatchelError("unreadable_row", "a channel audit row is not JSON")
        if isinstance(record, dict) and isinstance(record.get("key_sha256"), str):
            hashes.add(record["key_sha256"].lower())
    return channels, hashes


def mask_database(conn, masker):
    """M1: every TEXT/BLOB value of every table. Rows without a hit are not rewritten."""
    counts = {}
    conn.text_factory = bytes  # decode here, so a non-UTF-8 TEXT value is a named refusal (M4)
    try:
        _mask_tables(conn, masker, counts)
    finally:
        conn.text_factory = str
    return counts


def _mask_tables(conn, masker, counts):
    for table in [t.decode("utf-8") if isinstance(t, bytes) else t for t in _tables(conn)]:
        if table not in KNOWN_TABLES:
            raise SatchelError("unknown_table", "table %s is not known to Satchel" % table)
        columns = [r[1].decode("utf-8") if isinstance(r[1], bytes) else r[1]
                   for r in conn.execute("PRAGMA table_info(%s)" % table)]
        rows = conn.execute("SELECT rowid, %s FROM %s ORDER BY rowid" % (", ".join(columns), table)).fetchall()
        counts[table] = len(rows)
        for row in rows:
            rowid, values = row[0], row[1:]
            for column, value in zip(columns, values):
                if isinstance(value, bytes):
                    try:
                        value = value.decode("utf-8")
                    except UnicodeDecodeError:
                        raise SatchelError("undecodable", "%s.%s rowid %d is not UTF-8" % (table, column, rowid))
                if not isinstance(value, str):
                    continue
                new, changed = masker.field(value, {"member": "state.sqlite3", "table": table, "column": column,
                                                    "row": rowid})
                if changed:
                    conn.execute("UPDATE %s SET %s=? WHERE rowid=?" % (table, column), (new, rowid))


def _mask_json_file(source, masker, member):
    try:
        raw = source.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise SatchelError("undecodable", "%s is not UTF-8" % member)
    try:
        parsed = json.loads(raw)
    except ValueError:
        raise SatchelError("unreadable_config", "%s is not JSON" % member)
    value, changed = masker.value(parsed, {"member": member})
    if not changed:
        return raw.encode("utf-8")
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode("utf-8")


def _mask_text_file(source, masker, member):
    try:
        raw = source.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        raise SatchelError("undecodable", "%s is not UTF-8" % member)
    return masker.text(raw, {"member": member})[0].encode("utf-8")


def _mask_journal(source, masker, member):
    """Journals line by line; a line with a hit is re-serialised canonically (its chain head changes, M5)."""
    try:
        raws = source.read_bytes().decode("utf-8").split("\n")
    except UnicodeDecodeError:
        raise SatchelError("undecodable", "%s is not UTF-8" % member)
    out = []
    for number, raw in enumerate(raws, 1):
        location = {"member": member, "line": number}
        try:
            parsed = json.loads(raw) if raw.strip() else None
        except ValueError:
            parsed = None
        if isinstance(parsed, dict):
            value, changed = masker.value(parsed, location)
            out.append(ledgerline.canonical(value) if changed else raw)
        else:
            out.append(masker.text(raw, location)[0])
    return "\n".join(out).encode("utf-8")


def _export_runs(home, masker):
    """Ledgerline export with the raw values scanned (M1): a free-text value with a hit is exported without its
    sha256 (F8), every exported string is masked, and the run line of a run with a mask says so (M5)."""
    lines, hit = [], {"now": False}

    def guard(text):
        kinds = sorted({k for _, _, k in detect(text, masker.channel_hashes)})
        if kinds:
            hit["now"] = True
        return kinds

    run_line, masked_runs = {}, set()
    with ledgerline.text_guard(guard):
        for line in ledgerline.export_runs(home, audit=False):
            # The guard ran while the generator built this line; reset only after it is accounted for.
            new, changed = masker.value(line, {"member": "runs.jsonl", "line": len(lines) + 1})
            run_id = line.get("run_id")
            if line.get("kind") == "run":
                run_line[run_id] = len(lines)
            if changed or hit["now"]:
                masked_runs.add(run_id)
            hit["now"] = False
            lines.append(new)
    for run_id in masked_runs:
        if run_id in run_line:
            lines[run_line[run_id]]["masked"] = True
    return "".join(ledgerline.canonical(line) + "\n" for line in lines).encode("utf-8")


def _meetings(home):
    base = Path(home) / "meetings"
    if not base.is_dir():
        return []
    out = []
    for entry in sorted(base.iterdir(), key=lambda p: p.name):
        journal = entry / "journal.jsonl"
        if entry.is_dir() and journal.is_file():
            out.append((entry.name, journal))
        elif entry.name != ".DS_Store":
            raise SatchelError("unknown_home_file", "meetings/%s is not a meeting journal directory" % entry.name)
    return out


def _git_commit():
    head = Path(__file__).resolve().parent.parent / ".git"
    try:
        ref = (head / "HEAD").read_text().strip()
        if ref.startswith("ref: "):
            return (head / ref[5:]).read_text().strip()
        return ref
    except OSError:
        return None


def extract(home, out, timeout=DEFAULT_TIMEOUT, temp_base=None, now=None):
    """Write satchel-<id>-<stamp>.tar into `out`. Returns a summary (path, sha256, mask counts per kind)."""
    home = Path(home).expanduser().resolve()
    out = Path(out).expanduser().resolve()
    if not (home / "state.sqlite3").is_file():
        raise SatchelError("no_home", "%s has no state.sqlite3" % home)
    if not out.is_dir():
        raise SatchelError("no_out", "--out %s is not a directory" % out)
    created = now or _now()
    with _workdir(temp_base) as (work, swept):
        included, excluded = inventory(home)
        snap, member_dir = work / "snap.sqlite3", work / "archive"
        member_dir.mkdir(mode=0o700)
        _snapshot(home, snap, timeout)
        conn = sqlite3.connect(str(snap), isolation_level=None)
        try:
            version = conn.execute("PRAGMA user_version").fetchone()[0]
            ident = conn.execute("SELECT data FROM identity LIMIT 1").fetchone()
            identity = json.loads(ident[0]) if ident else {}
            channels, hashes = _read_channels(conn)
            masker = Masker(hashes)
            conn.execute("BEGIN")
            conn.execute("DELETE FROM channels")
            counts = mask_database(conn, masker)
            counts["channels"] = 0
            conn.execute("COMMIT")
            grants = [json.loads(r[0]) for r in conn.execute("SELECT data FROM access_grants ORDER BY rowid")]
            # 2c: a second VACUUM INTO so freed pages (deleted channel rows, pre-mask text) are not carried.
            conn.execute("VACUUM INTO ?", (str(member_dir / "state.sqlite3"),))
        finally:
            conn.close()
        snap.unlink()
        employee = identity.get("id")
        if not employee:
            raise SatchelError("no_identity", "the home has no identity row")
        members = ["state.sqlite3"]
        (member_dir / "config").mkdir(mode=0o700)
        mtimes = {}
        for name in included:
            source = home / name
            mtimes[name] = source.stat().st_mtime
            data = (_mask_text_file if name in CONFIG_TEXT else _mask_json_file)(source, masker, "config/" + name)
            (member_dir / "config" / name).write_bytes(data)
            members.append("config/" + name)
        premask = {}
        for meeting, journal in _meetings(home):
            premask[meeting] = ledgerline.chain_head(journal)
            member = "meetings/%s/journal.jsonl" % meeting
            data = _mask_journal(journal, masker, member)
            (member_dir / "meetings" / meeting).mkdir(parents=True, mode=0o700)
            (member_dir / member).write_bytes(data)
            members.append(member)
        (member_dir / "runs.jsonl").write_bytes(_export_runs(home, masker))
        members.append("runs.jsonl")
        if hashes:
            # The detectors must have removed every stored key hash from every member.
            for member in members:
                blob = (member_dir / member).read_bytes()
                if any(h.encode() in blob for h in hashes):
                    raise SatchelError("leak", "a channel key hash survived masking in %s" % member)
        kinds = {}
        for mask in masker.masks:
            kinds[mask["kind"]] = kinds.get(mask["kind"], 0) + 1
        manifest = {
            "format": FORMAT, "kind": "backup", "employee_id": employee, "employee_name": identity.get("name"),
            "user_version": version, "created_at": created,
            "source_host": hashlib.sha256(socket.gethostname().encode("utf-8")).hexdigest()[:16],
            "code_commit": _git_commit(), "row_counts": counts,
            "members": {m: {"sha256": _sha256_file(member_dir / m), "size": (member_dir / m).stat().st_size}
                        for m in members},
            "config_mtimes": mtimes, "excluded": excluded,
            "reprovision": {"channels": channels,
                            "grants": [{k: g.get(k) for k in ("id", "path", "mode", "purpose", "revoked_at")}
                                       for g in grants],
                            "launchd_label": "com.synthetics.worker." + hashlib.sha256(
                                employee.encode("utf-8")).hexdigest()[:16]},
            "masks": masker.masks, "mask_counts": kinds,
            "premask": {"journals": premask},
            "limits": ["Masked values are replaced by markers; nothing un-masks them.",
                       "Detectors are patterns plus exact channel keys: a secret with no recognisable shape is "
                       "not masked.", "Everything else is plaintext (no encryption, owner's choice)."],
        }
        (member_dir / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True,
                                                             ensure_ascii=False) + "\n", encoding="utf-8")
        members.insert(0, "manifest.json")
        sums = "".join("%s  %s\n" % (_sha256_file(member_dir / m), m) for m in members)
        (member_dir / "SHA256SUMS").write_text(sums, encoding="utf-8")
        members.append("SHA256SUMS")
        stamp = created.replace("-", "").replace(":", "").split(".")[0] + "Z"
        name = "satchel-%s-%s.tar" % (employee, stamp)
        local_tar = work / name
        with tarfile.open(str(local_tar), "w", format=tarfile.PAX_FORMAT) as tar:
            for member in members:
                info = tar.gettarinfo(str(member_dir / member), arcname=member)
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                info.mtime = 0
                info.mode = 0o600
                with open(str(member_dir / member), "rb") as handle:
                    tar.addfile(info, handle)
        digest = _sha256_file(local_tar)
        # Only the finished, masked tar reaches --out: temp name in --out, then rename (C1).
        partial = out / (PARTIAL_PREFIX + name)
        final = out / name
        if final.exists():
            raise SatchelError("exists", "%s already exists" % final)
        try:
            fd = os.open(str(partial), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle, open(str(local_tar), "rb") as source:
                shutil.copyfileobj(source, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(str(partial), str(final))
        except BaseException:
            with contextlib.suppress(OSError):
                partial.unlink()
            raise
        return {"archive": str(final), "sha256": digest, "employee_id": employee, "mask_counts": kinds,
                "masks": len(masker.masks), "row_counts": counts, "excluded": sorted(excluded),
                "stale_temps_removed": swept}


# ---------------------------------------------------------------------------------------------------------
# Verify (read-only)

def _safe_member(name):
    return not (name.startswith("/") or ".." in Path(name).parts or "\\" in name)


def _verify_runs(data):
    """Chain check of runs.jsonl: per run and file kind, prev_sha256 links line_sha256s and the last line equals the
    run's anchored receipt head (mcp receipts). Returns {run_id: verdict}."""
    out, runs, chains = {}, {}, {}
    for raw in data.decode("utf-8").splitlines():
        if not raw:
            continue
        line = json.loads(raw)
        kind = line.get("kind")
        if kind == "run":
            runs[line.get("run_id")] = line
        elif kind in ("mcp.call", "stream"):
            chains.setdefault((line.get("run_id"), kind), []).append(line)
    for run_id, record in runs.items():
        verdict = "chain_ok"
        for kind in ("mcp.call", "stream"):
            previous = None
            for line in chains.get((run_id, kind), []):
                if line.get("unparseable") or "prev_sha256" not in line:
                    verdict = "unanchored" if verdict == "chain_ok" else verdict
                    previous = line.get("line_sha256")
                    continue
                if line.get("prev_sha256") != previous:
                    verdict = "chain_broken"
                previous = line.get("line_sha256")
            if kind == "mcp.call":
                anchor = record.get("receipt_chain")
                calls = chains.get((run_id, kind), [])
                if isinstance(anchor, dict) and calls:
                    if anchor.get("head_sha256") != previous or anchor.get("lines") != len(calls):
                        verdict = "chain_broken"
        if verdict == "chain_ok" and record.get("masked"):
            verdict = "chain_ok (masked values)"
        out[run_id] = verdict
    return out


def verify(archive, temp_base=None):
    """Read-only check of an archive. Returns a report with ok True/False and the reasons."""
    archive = Path(archive).expanduser().resolve()
    problems, report = [], {"archive": str(archive)}
    with _workdir(temp_base) as (work, _):
        try:
            tar = tarfile.open(str(archive), "r:")
        except (OSError, tarfile.TarError) as error:
            return dict(report, ok=False, problems=["unreadable archive: %s" % error])
        with tar:
            data = {}
            for info in tar.getmembers():
                if not info.isfile() or not _safe_member(info.name):
                    problems.append("unsafe or non-file member %s" % info.name)
                    continue
                data[info.name] = tar.extractfile(info).read()
        if "SHA256SUMS" not in data or "manifest.json" not in data:
            return dict(report, ok=False, problems=problems + ["missing SHA256SUMS or manifest.json"])
        listed = {}
        for row in data["SHA256SUMS"].decode("utf-8").splitlines():
            digest, _, name = row.partition("  ")
            listed[name] = digest
        for name, blob in data.items():
            if name == "SHA256SUMS":
                continue
            if name not in listed:
                problems.append("member %s not in SHA256SUMS" % name)
            elif hashlib.sha256(blob).hexdigest() != listed[name]:
                problems.append("sha256 mismatch: %s" % name)
        for name in listed:
            if name not in data:
                problems.append("member %s missing" % name)
        manifest = json.loads(data["manifest.json"].decode("utf-8"))
        if manifest.get("format") != FORMAT:
            return dict(report, ok=False, problems=problems + ["unknown format %r" % manifest.get("format")])
        report.update(employee_id=manifest.get("employee_id"), kind=manifest.get("kind"),
                      created_at=manifest.get("created_at"), mask_counts=manifest.get("mask_counts"))
        for name, facts in (manifest.get("members") or {}).items():
            if name in data and facts.get("sha256") != hashlib.sha256(data[name]).hexdigest():
                problems.append("manifest sha256 mismatch: %s" % name)
        db = work / "state.sqlite3"
        db.write_bytes(data.get("state.sqlite3", b""))
        try:
            conn = sqlro.connect_ro(db)
            try:
                check = conn.execute("PRAGMA integrity_check").fetchone()[0]
                if check != "ok":
                    problems.append("integrity_check: %s" % check)
                for table, count in (manifest.get("row_counts") or {}).items():
                    got = conn.execute("SELECT COUNT(*) FROM %s" % table).fetchone()[0] \
                        if table in _tables(conn) else None
                    if got != count:
                        problems.append("row count %s: manifest %s, archive %s" % (table, count, got))
            finally:
                conn.close()
        except sqlite3.Error as error:
            problems.append("database unreadable: %s" % error)
        # Markers and detectors: every marker in a member is listed in masks[] (and vice versa); 0 hits remain.
        listed_masks = {(m.get("n"), m.get("kind")) for m in manifest.get("masks") or []}
        seen = set()
        for name, blob in data.items():
            if name in ("SHA256SUMS", "manifest.json"):
                continue
            text = blob.decode("utf-8", errors="replace") if name != "state.sqlite3" else _db_text(db)
            for match in MARKER.finditer(text):
                key = (int(match.group(2)), match.group(1))
                if key not in listed_masks:
                    problems.append("marker %s in %s is not listed in masks[]" % (match.group(), name))
                seen.add(key)
            hits = detect(text)
            if hits:
                problems.append("%d detector hit(s) left in %s (%s)" % (len(hits), name,
                                                                        ", ".join(sorted({h[2] for h in hits}))))
        missing = listed_masks - seen
        if missing:
            problems.append("%d masks[] entries have no marker in the archive" % len(missing))
        if "runs.jsonl" in data:
            verdicts = _verify_runs(data["runs.jsonl"])
            report["runs"] = {v: sum(1 for x in verdicts.values() if x == v) for v in set(verdicts.values())}
            broken = sorted(r for r, v in verdicts.items() if v == "chain_broken")
            if broken:
                problems.append("run chains broken: %s" % ", ".join(broken[:10]))
        journals = {}
        for meeting, head in ((manifest.get("premask") or {}).get("journals") or {}).items():
            blob = data.get("meetings/%s/journal.jsonl" % meeting)
            if blob is None:
                problems.append("journal of %s missing" % meeting)
                continue
            lines = [l for l in blob.split(b"\n") if l]
            now_head = hashlib.sha256(lines[-1]).hexdigest() if lines else None
            pre = (head or {}).get("head_sha256")
            journals[meeting] = "unchanged" if now_head == pre else {"masked": True, "premask_head": pre,
                                                                     "archived_head": now_head}
        report["journals"] = journals
    report["problems"] = problems
    report["ok"] = not problems
    report["guarantee"] = ("members are the extracted ones, the database is consistent, row counts match, run chains "
                           "hold (masked values noted), every marker is accounted for and no detector pattern hits; "
                           "content is not equal to the source home where values were masked, and the exact "
                           "channel-key check is not repeated (the archive keeps no key hashes)")
    return report


def _db_text(db):
    """All text values of the archived database, read through SQL (the raw bytes are scanned by the leak test)."""
    conn = sqlro.connect_ro(db)
    try:
        parts = []
        for table in _tables(conn):
            columns = [r[1] for r in conn.execute("PRAGMA table_info(%s)" % table)]
            for row in conn.execute("SELECT %s FROM %s" % (", ".join(columns), table)):
                parts.extend(v if isinstance(v, str) else v.decode("utf-8", "replace")
                             for v in row if isinstance(v, (str, bytes)))
        return "\n".join(parts)
    finally:
        conn.close()
