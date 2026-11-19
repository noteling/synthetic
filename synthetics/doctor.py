"""`synthetics doctor`: a read-only health check of one employee home (M25 §6).

doctor never writes: it opens state.sqlite3 read-only through sqlro.connect_ro (Store() would create
tables, migrate and chmod; on older SQLite a cold WAL file is reopened rw with query_only; a WAL reader may still create SQLite's empty -shm/-wal side files), reads runtime.json with runtime._read (runtime.load seeds files), and only asks launchctl to print.
Exit status: 0 with no errors, 1 otherwise. Findings are {level: error|warning|info, code, detail}.
"""
from __future__ import annotations

import json
import os
import sqlite3
import stat
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from . import capture, runtime
from .sqlro import connect_ro
from .engine import FAILURE_WINDOW, failure_streak, read_limit_override, worker_status

# --compare-legacy takes the pre-M25 behaviour as data (C7: product code names no human or provider prefix):
#   {"senders": [...], "channel_prefixes": [...], "secretary_prefixes": [...], "bypass_sources": [...]}
# reproducing base capture.LEGACY_HUMANS and engine.LIMIT_BYPASS_SOURCES. Bypass also needed auth and
# limit_allow since M22. The P2 operator kit stages this host's file.
COMPARE_EVENTS = 200
LEGACY_KEYS = ("senders", "channel_prefixes", "secretary_prefixes", "bypass_sources")


def load_legacy(path):
    data = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("%s must be a JSON object" % path)
    out = {}
    for key in LEGACY_KEYS:
        value = data.get(key, [])
        if not isinstance(value, list) or not all(isinstance(x, str) and x for x in value):
            raise ValueError("%s: %s must be a list of non-empty strings" % (path, key))
        out[key] = value
    return out


def legacy_role(event, legacy):
    if (event.get("source") or "").startswith(tuple(legacy["secretary_prefixes"])):
        return "secretary"
    if event.get("sender") in legacy["senders"]:
        return "human"
    return "other"


def legacy_bypass(event, legacy, limit_allow):
    return bool(str(event.get("source", "")).startswith(tuple(legacy["bypass_sources"]))
                and event.get("sender") in set(limit_allow or ())
                and event.get("auth"))


def probe_events(legacy, cfg, limit_allow):
    """Deterministic probes covering every legacy and configured branch, so an empty home still compares."""
    ops = cfg.get("operators") or {}
    senders = sorted(set(legacy["senders"]) | set(limit_allow or ()) | set(cfg["senders"])
                     | set(ops.get("senders", ()))) + ["probe-other-sender"]
    prefixes = (set(legacy["channel_prefixes"]) | set(legacy["secretary_prefixes"]) | set(legacy["bypass_sources"])
                | set(cfg["channel_prefixes"]) | set(cfg["secretary_prefixes"]) | set(ops.get("source_prefixes", ()))
                | set((cfg.get("limit_bypass") or {}).get("channel_prefixes", ())))
    sources = sorted(p + "probe" for p in prefixes) + ["probe-other-source"]
    out = []
    for sender in senders:
        for source in sources:
            for auth in (None, {"channel": "probe"}):
                out.append({"id": "probe:%s:%s:%s" % (sender, source, "auth" if auth else "noauth"),
                            "sender": sender, "source": source, "auth": auth})
    return out


def compare_legacy(legacy, cfg, limit_allow, events):
    """Return (diffs, unauthenticated, operator) comparing legacy constants with this humans.json.

    Intended M25 changes are not diffs: C1 maps operator sources/senders to secretary (even when the sender
    label claims a human), and C3 keeps today's rule that an unauthenticated forward never bypasses a limit.
    """
    diffs, unauth, operator = [], [], []
    for event in events:
        old_role, new_role = legacy_role(event, legacy), capture.role_of(event, "in", cfg)
        old_bypass = legacy_bypass(event, legacy, limit_allow)
        new_bypass = capture.may_bypass_limit(event, cfg, limit_allow)
        if old_role != new_role:
            if new_role == "secretary" and capture.is_operator(event, cfg):
                operator.append({"event": event["id"], "legacy": old_role})
            else:
                diffs.append({"event": event["id"], "field": "role", "legacy": old_role, "config": new_role})
        if old_bypass != new_bypass:
            diffs.append({"event": event["id"], "field": "limit_bypass", "legacy": old_bypass, "config": new_bypass})
        if (not event.get("auth") and str(event.get("source", "")).startswith(tuple(legacy["bypass_sources"]))
                and event.get("sender") in set(limit_allow or ())):
            unauth.append(event["id"])
    return diffs, unauth, operator


def _connect_ro(path):
    db = connect_ro(path)  # backlog 40: also opens a cold WAL database on older SQLite builds
    db.row_factory = sqlite3.Row
    return db


def _all(db, table):
    try:
        return [json.loads(r["data"]) for r in db.execute("SELECT data FROM %s ORDER BY rowid" % table)]
    except sqlite3.OperationalError:
        return []


def _launchd_pid(label, runner=subprocess.run):
    result = runner(["launchctl", "print", "gui/%d/%s" % (os.getuid(), label)],
                    capture_output=True, text=True, timeout=10)
    if result.returncode != 0:
        return False, None
    for line in result.stdout.splitlines():
        line = line.strip()
        if line.startswith("pid = "):
            value = line.split("=", 1)[1].strip()
            return True, int(value) if value.isdigit() else None
    return True, None


def _parse_time(text):
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None


def relay_cross_check(home, identity_id, channels, relay_file, relay_channel="fieldpost"):
    """M27 (ii): compare one channel record's routes with the relay's route file.

    A channel route the relay cannot carry leaves messages pending with no signal to the sender
    (question-7ca95827, 11-18); a relay route the channel record lacks makes the relay's
    delivery_record fail not_routed. Returns (summary, findings)."""
    data = json.loads(Path(relay_file).expanduser().read_text(encoding="utf-8"))
    aliases = data.get("aliases") or {}
    home_text = str(Path(home).resolve())
    outgoing = {r.get("recipient_id") for r in data.get("routes") or []
                if r.get("sender_id") == identity_id
                or (r.get("sender_home") and str(Path(r["sender_home"]).expanduser().resolve()) == home_text)}
    findings = []
    channel = next((c for c in channels if c.get("id") == relay_channel), None)
    if channel is None:
        findings.append(("info", "relay_channel_unregistered", "No channel record %s in this home." % relay_channel))
        return {"channel": relay_channel, "relay_recipients": sorted(outgoing)}, findings
    routes = list(channel.get("routes") or [])
    resolved = {route: aliases.get(route, route) for route in routes}
    missing = sorted(r for r, target in resolved.items() if target != identity_id and target not in outgoing)
    covered = set(resolved.values())
    unrouted = sorted(t for t in outgoing if t not in covered)
    if missing:
        findings.append(("warning", "route_without_relay",
                         "%s routes %s but the relay has no route from this home to them; messages stay pending."
                         % (relay_channel, ", ".join(missing))))
    if unrouted:
        findings.append(("warning", "relay_without_channel_route",
                         "The relay carries this home to %s but channel %s does not route them (not_routed)."
                         % (", ".join(unrouted), relay_channel)))
    return {"channel": relay_channel, "routes": routes, "relay_recipients": sorted(outgoing),
            "route_without_relay": missing, "relay_without_channel_route": unrouted}, findings


def diagnose(home, label=None, compare=None, key_files=(), launchd=_launchd_pid, now=None, collector_plists=(),
             collector_dirs=(), relay_routes=None, relay_channel="fieldpost"):
    from .store import Store

    home = Path(home).expanduser().resolve()
    now = now or datetime.now(timezone.utc)
    findings = []

    def add(level, code, detail=None):
        findings.append({"level": level, "code": code, "detail": detail})

    report = {"home": str(home), "findings": findings}
    db_path = home / "state.sqlite3"
    if not db_path.is_file():
        add("error", "home_missing", "No state.sqlite3 in %s." % home)
        return report
    try:
        db = _connect_ro(db_path)
        version = db.execute("PRAGMA user_version").fetchone()[0]
    except sqlite3.Error as error:
        add("error", "home_unreadable", str(error))
        return report
    try:
        report["schema_version"] = version
        if version < Store.SCHEMA_VERSION:
            add("error", "schema_old", "user_version %d < %d; the next Store open migrates it."
                % (version, Store.SCHEMA_VERSION))
        elif version > Store.SCHEMA_VERSION:
            add("error", "schema_newer", "user_version %d > %d; this code is older than the home."
                % (version, Store.SCHEMA_VERSION))

        identities = _all(db, "identity")
        if not identities:
            add("error", "identity_missing", "Initialize an employee first.")
        else:
            report["identity"] = {"id": identities[0].get("id"), "name": identities[0].get("name")}

        # runtime.json: valid, and its claude executable (allowlists are the worker's argv; not checked here)
        config, problem = runtime._read(home / runtime.CONFIG_FILE, None, None)
        limit_allow = []
        if config is None:
            add("warning" if problem == "absent" else "error", "runtime_" + problem.split(":")[0],
                None if problem == "absent" else problem)
        else:
            limit_allow = list(config.get("limit_allow") or [])
            report["runtime"] = {k: config.get(k) for k in ("model", "budget", "timeout", "daily_limit",
                                                            "limit_notify", "limit_allow", "claude")}
            notify = config.get("limit_notify")
            channels = _all(db, "channels")
            if notify and channels and not any(notify in (c.get("routes") or []) for c in channels):
                # backlog 25: limit/stop alerts and runtime notices to limit_notify would queue forever
                add("warning", "limit_notify_unroutable", "runtime limit_notify %s is routed by no channel of this "
                    "home (channels: %s)" % (notify, ", ".join(sorted(c.get("id", "?") for c in channels))))
            claude = config.get("claude")
            if claude and not os.access(os.path.realpath(os.path.expanduser(claude)), os.X_OK):
                add("error", "claude_not_executable", claude)

        # humans.json (C1/C2): missing is a warning, malformed an error
        cfg = None
        try:
            cfg = capture.humans(home)
        except (ValueError, OSError) as error:
            add("error", "humans_invalid", str(error))
        else:
            if not cfg.get("present"):
                add("warning", "humans_missing", "No humans.json: nobody is human and nothing bypasses a limit.")
            elif not cfg["senders"]:
                add("warning", "humans_empty", "humans.json lists no human senders.")
            report["humans"] = {"default_human": cfg["default_human"], "senders": cfg["senders"]}
            lb = cfg.get("limit_bypass") or {}
            board_prefixes = [x for x in list(cfg.get("channel_prefixes") or []) + list(cfg.get("secretary_prefixes") or [])
                              + list(lb.get("channel_prefixes") or [])
                              + list((cfg.get("operators") or {}).get("source_prefixes") or [])
                              if str(x).startswith("board") or capture.BOARD_SOURCE.startswith(str(x))]
            if board_prefixes:
                add("warning", "humans_board_prefix", "humans.json prefixes match board events (M27 Q4: board "
                    "posts are never a human's words or a limit bypass): %s" % ", ".join(map(str, board_prefixes)))

        # worker process and launch agent
        status = worker_status(home)
        report["worker"] = status
        if label:
            loaded, pid = launchd(label)
            report["launch_agent"] = {"label": label, "loaded": loaded, "pid": pid}
            if not loaded:
                add("error", "launch_agent_not_loaded", label)
            elif not pid:
                add("warning", "launch_agent_not_running", label)
            elif status["pid"] and pid != status["pid"]:
                add("warning", "worker_pid_mismatch", "launchd %s, worker.lock %s" % (pid, status["pid"]))

        # channels (C2: none registered is reported, not a failure; P1b adds routes)
        channels = _all(db, "channels")
        report["channels"] = [{"id": c.get("id"), "source_prefixes": c.get("source_prefixes")} for c in channels]
        if not channels:
            add("info", "channel_unprovisioned",
                "No channel records; in-process Telegram/Fieldpost keep today's unauthenticated behaviour.")
        if relay_routes:
            identity_id = (report.get("identity") or {}).get("id")
            try:
                summary, extra = relay_cross_check(home, identity_id, channels, relay_routes, relay_channel)
            except (OSError, ValueError) as error:
                add("error", "relay_routes_invalid", str(error))
            else:
                report["relay"] = summary
                for level, code, detail in extra:
                    add(level, code, detail)
        for key_file in key_files:
            path = Path(key_file).expanduser()
            try:
                mode = path.stat().st_mode
            except OSError as error:
                add("error", "key_file_unreadable", "%s: %s" % (path, error))
                continue
            if mode & (stat.S_IRWXG | stat.S_IRWXO):
                add("error", "key_file_exposed", "%s is mode %o; use 0600." % (path, stat.S_IMODE(mode)))

        # outbox: undelivered messages and the oldest age
        pending = [m for m in _all(db, "messages")
                   if not any(d.get("status") == "delivered" for d in m.get("deliveries") or [])]
        oldest = min((t for t in (_parse_time(m.get("created_at")) for m in pending) if t), default=None)
        report["outbox"] = {"undelivered": len(pending),
                            "oldest_age_seconds": int((now - oldest).total_seconds()) if oldest else None}
        # M27 (ii): the same view the employee's snapshot shows (pending/uncertain >10 min, <48 h)
        stuck = Store.outbox_stuck_from(_all(db, "messages"), channels, now)
        report["outbox"]["stuck"] = stuck["count"]
        unroutable = [i for i in stuck["items_all"] if not i["routed_by"]]
        if stuck["count"]:
            add("warning", "outbox_stuck", "%d messages undelivered for over 10 minutes (newest %s to %s)."
                % (stuck["count"], stuck["items_all"][0]["id"], stuck["items_all"][0]["to"]))
        if unroutable:
            add("warning", "outbox_unroutable", "%d stuck messages go to addresses no channel routes: %s"
                % (len(unroutable), ", ".join(sorted({i["to"] for i in unroutable}))))

        # Docket P1b (Q4): answered relay questions whose carried questions still await my reply
        owed = Store.relay_owed_from(_all(db, "messages"), _all(db, "questions"), now)
        report["outbox"]["relay_owed"] = len(owed)
        late = [o for o in owed if (o["age_minutes"] or 0) >= Store.RELAY_OWED_WARN_MINUTES]
        if late:
            add("warning", "relay_owed", "%d relayed questions answered over 4 h ago and not yet passed on: %s"
                % (len(late), ", ".join("%s to %s" % (o["question_id"], o["asker"]) for o in late)))

        # capture (M21)
        try:
            rows = db.execute("SELECT event_id,error FROM capture_outbox WHERE status='pending'").fetchall()
        except sqlite3.OperationalError:
            rows = []
        report["capture"] = {"pending": len(rows), "failing": sum(1 for r in rows if r["error"])}
        if report["capture"]["failing"]:
            add("error", "capture_failing", "%d capture rows have errors." % report["capture"]["failing"])

        # recent failure streak or operator limit override
        runs = list(reversed(_all(db, "runs")))
        streak = failure_streak(runs)
        if streak:
            add("error", "failure_streak", "The newest %d runs failed (latest %s); the worker stops." %
                (FAILURE_WINDOW, streak))
        override = read_limit_override(home)
        if override:
            report["limit_override"] = override

        # RTN P1 routines: discipline flags (David's revisit trigger) and the manager route (E2)
        from . import routines
        works = [w for w in _all(db, "works") if routines.is_routine(w)]
        if works:
            running = any(r.get("status") == "running" for r in runs)
            flagged = routines.flags(works, now, running)
            report["routines"] = {"open": sum(1 for w in works if w.get("status") in ("ready", "waiting")),
                                  "flags": [f["code"] for f in flagged]}
            for flag in flagged:
                detail = flag.get("detail") or ("%(total)d all time, %(last_7_days)d in the last 7 days" % flag)
                add(flag["severity"], flag["code"], "%s %s: %s" % (flag["kind"], flag["work_id"], detail))
            route = routines.route(routines.configured(home), None, (identities[0] if identities else {}).get("id"),
                                   (cfg or {}).get("default_human"))
            report["routines"]["route"] = route
            if not route["resolved"] or route.get("reason"):
                add("warning", "routine_no_manager", route.get("reason"))

        if compare:
            try:
                legacy = load_legacy(compare)
            except (OSError, ValueError) as error:
                legacy = None
                add("error", "legacy_file_invalid", str(error))
            if cfg is None:
                add("error", "compare_legacy_skipped", "humans.json is invalid.")
            elif legacy is not None:
                rows = db.execute("SELECT data FROM events ORDER BY seq DESC LIMIT ?", (COMPARE_EVENTS,)).fetchall()
                events = [json.loads(r["data"]) for r in rows]
                diffs, unauth, operator = compare_legacy(legacy, cfg, limit_allow, events)
                probes = probe_events(legacy, cfg, limit_allow)
                probe_diffs, _, _ = compare_legacy(legacy, cfg, limit_allow, probes)
                diffs += probe_diffs  # probes cover every legacy branch; only real events produce warnings
                report["compare_legacy"] = {"events": len(events), "probes": len(probes), "diffs": diffs,
                                            "unauthenticated_no_bypass": len(unauth),
                                            "operator_as_secretary": len(operator)}
                if diffs:
                    add("error", "legacy_diff", "%d differences from legacy behaviour." % len(diffs))
                if unauth:
                    add("warning", "unauthenticated_no_bypass",
                        "%d events from a bypass channel lack auth and do not bypass a limit (as today, C3)."
                        % len(unauth))
                was_human = [o["event"] for o in operator if o["legacy"] == "human"]
                if was_human:
                    add("warning", "operator_was_human",
                        "%d operator-source events claimed a human sender and are now secretary (C1): %s"
                        % (len(was_human), ", ".join(was_human[:5])))
                if operator:
                    add("info", "operator_as_secretary",
                        "%d operator events now map to secretary (C1)." % len(operator))
        roster_checks(home, db, version, cfg, add, report)
        self_raise_checks(db, add, report)
        clamped_checks(home, add, report)
        launch_op_checks(db, add, report)
        runs_size_check(home, add)
    finally:
        db.close()
    collector_checks(home, collector_plists, collector_dirs, add, report)
    report["ok"] = not any(f["level"] == "error" for f in findings)
    return report


def self_raise_checks(db, add, report):
    """M28 D7: list every limit.raise a session made on its own home (audit authority.used, self_raise)."""
    try:
        rows = db.execute("SELECT at, data FROM audit WHERE action='authority.used' ORDER BY seq DESC LIMIT 500")
        raises = []
        for at, data in rows.fetchall():
            d = json.loads(data)
            if d.get("action") == "limit.raise" and d.get("self_raise"):
                raises.append({"at": at, "run": d.get("run"), "usd": d.get("usd"), "duration": d.get("duration"),
                               "delegation": d.get("delegation")})
    except Exception:  # an old home without the audit shape: nothing to list
        return
    report["self_raises"] = raises
    if raises:
        add("info", "limit_self_raise", "%d self-raise(s) of the daily limit; newest %s $%s %s (run %s)"
            % (len(raises), raises[0]["at"], raises[0]["usd"], raises[0]["duration"], raises[0]["run"]))


def runs_size_check(home, add):
    """E4: info runs_size when runs/ is over 80% of its retention size bound (the worker prunes at idle)."""
    try:
        from . import runtime as runtime_settings
        try:
            config = json.loads((Path(home) / runtime_settings.CONFIG_FILE).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            config = {}
        bound = float(runtime_settings.setting(config, "runs_max_mb")) * 1024 * 1024
        total = sum(e.stat(follow_symlinks=False).st_size for e in os.scandir(str(Path(home) / "runs"))
                    if e.is_file(follow_symlinks=False))
    except Exception:  # noqa: BLE001 - no runs/ yet, or unreadable: nothing to report
        return
    if total > bound:
        # E4-r2 (Rowan L2): still over after the daily prune means pinned, recent or unknown files fill runs/.
        add("warning", "runs_over_bound", "runs/ holds %.1f MB, over its %.0f MB bound; if the daily prune already ran, "
            "what is left is pinned, among the newest runs, running, or not a known run's file "
            "(`synthetics prune-runs --dry-run` shows unremovable_bytes and over_bound_after)"
            % (total / 1048576.0, bound / 1048576.0))
    elif total > 0.8 * bound:
        add("info", "runs_size", "runs/ holds %.1f MB of a %.0f MB bound; the worker prunes once a day at idle "
            "(`synthetics prune-runs --dry-run` lists what would go)" % (total / 1048576.0, bound / 1048576.0))


def launch_op_checks(db, add, report):
    """M28 D14: delegated launchd ops still pending in this home's ledger (outcome never recorded)."""
    try:
        rows = db.execute("SELECT data FROM launch_ops ORDER BY rowid").fetchall()
    except Exception:  # an old home without the ledger table
        return
    pending = []
    for (data,) in rows:
        d = json.loads(data)
        if d.get("status") == "pending":
            pending.append({"id": d["id"], "op": d.get("op"), "label": d.get("label"), "at": d.get("created_at")})
    report["launch_ops_pending"] = pending
    if pending:
        add("warning", "launch_op_pending", "%d delegated launchd op(s) without a recorded outcome (oldest %s %s %s); "
            "reconcile with `authority ops` / `authority settle OP`"
            % (len(pending), pending[0]["id"], pending[0]["op"], pending[0]["label"]))


def clamped_checks(home, add, report):
    """M28 P1c: list limit.raise delegations whose max_usd_per_day exceeds the effective limit.ceiling
    policy (they stay valid; use is clamped to the ceiling)."""
    from . import authority, roster
    path = roster.company_path(roster.root_of_home(home))
    if not path.exists():
        return
    try:
        cdb = _connect_ro(path)
        try:
            rows = cdb.execute("SELECT seq,id,kind,author,run,at,body FROM entries ORDER BY seq").fetchall()
        finally:
            cdb.close()
        state = roster.company_fold([{"seq": r[0], "id": r[1], "kind": r[2], "author": r[3], "run": r[4],
                                      "at": r[5], "body": json.loads(r[6])} for r in rows])
    except Exception:  # unreadable company log is reported by roster_checks
        return
    cap = authority.ceiling(state)
    report["limit_ceiling"] = {"usd": cap["usd"], "allow_disable": cap["allow_disable"], "entry": cap["entry"]}
    over = authority.clamped(state)
    report["limit_clamped"] = over
    if over:
        add("info", "limit_clamped", "%d limit.raise delegation(s) above the current ceiling $%s are clamped at use: %s"
            % (len(over), cap["usd"], ", ".join(o["delegation"] for o in over[:5])))


def roster_checks(home, db, version, cfg, add, report):
    """M26 step 7: company log readable, humans.json senders have profiles, alias collisions, unresolvable
    views, rendered-file drift. Read-only: the private log is read through this doctor's read-only handle."""
    from . import roster
    from .projects import ProjectError
    path = roster.company_path(roster.root_of_home(home))
    company = roster.company_fold([])
    if path.exists():
        try:
            cdb = _connect_ro(path)
            try:
                rows = cdb.execute("SELECT seq,id,kind,author,run,at,body FROM entries ORDER BY seq").fetchall()
            finally:
                cdb.close()
            entries = [{"seq": r["seq"], "id": r["id"], "kind": r["kind"], "author": r["author"],
                        "run": r["run"], "at": r["at"], "body": json.loads(r["body"])} for r in rows]
            company = roster.company_fold(entries)
        except (sqlite3.Error, ValueError, KeyError, ProjectError) as error:
            add("error", "company_log_unreadable", "%s: %s" % (path, error))
            return
    else:
        add("info", "company_log_missing", "No company log at %s (roster not seeded)." % path)
    people = company["people"]
    summary = {"people": len(people), "views": 0}
    if people and cfg is not None:
        missing = [s for s in cfg.get("senders", []) if roster.resolve(company, s)["id"] is None]
        if missing:
            add("warning", "human_without_profile", "humans.json senders with no company profile: %s"
                % ", ".join(sorted(missing)))
    # alias collisions: an alias that also names another person (as ID or name)
    for key, hit in sorted(company.get("aliases", {}).items()):
        for pid, p in people.items():
            if pid != hit["id"] and (roster.fold_alias(pid) == key or
                                     (p.get("name") and roster.fold_alias(p["name"]) == key)):
                add("error", "alias_collision", "alias %r -> %s also names %s" % (hit.get("alias", key),
                                                                                   hit["id"], pid))
    if version >= 5:
        rows = db.execute("SELECT * FROM roster_log ORDER BY seq").fetchall()
        private = roster.private_fold([{"seq": r["seq"], "id": r["id"], "kind": r["kind"], "author": r["author"],
                                        "run": r["run"], "at": r["at"], "body": json.loads(r["body"])}
                                       for r in rows])
        summary["views"] = len(private["views"])
        bad = sorted(pid for pid in private["views"] if pid not in people)
        if bad:
            add("warning", "view_unresolvable", "private views name people with no company profile: %s"
                % ", ".join(bad))
        stale = sorted(pid for pid, v in private["views"].items()
                       if pid in people and people[pid].get("role_rev", 0) > v.get("basis_role_rev", 0))
        if stale:
            add("info", "view_stale", "views older than the person's role: %s" % ", ".join(stale))
    for where, directory in (("home", home / "roster"), ("company", path.parent)):
        for d in roster.drift(directory):
            add("warning", d["problem"] if d["problem"] != "missing" else "roster_file_missing",
                "%s %s (next render overwrites)" % (where, d["file"]))
    report["roster"] = summary


def _plist_source_dirs(plist_path):
    import plistlib
    with open(plist_path, "rb") as fh:
        args = plistlib.load(fh).get("ProgramArguments", [])
    return [args[i + 1] for i, a in enumerate(args[:-1]) if a == "--source-dir"]


def collector_checks(home, plists, dirs, add, report):
    """C6: a collector source dir must never contain a home or lie inside a home's roster/ (private views
    would be fed as documents). Checks every home next to this one, not only this home."""
    if not plists and not dirs:
        return
    from . import roster
    root = roster.root_of_home(home)
    homes = sorted(p.parent.resolve() for p in root.glob("*/state.sqlite3"))
    sources = [(None, d) for d in dirs]
    for plist in plists:
        try:
            sources += [(str(plist), d) for d in _plist_source_dirs(plist)]
        except (OSError, ValueError) as error:
            add("error", "collector_plist_unreadable", "%s: %s" % (plist, error))
    for origin, d in sources:
        src = Path(d).expanduser().resolve()
        for h in homes:
            roster_dir = h / "roster"
            inside = src == h or src in h.parents or src == roster_dir or roster_dir in src.parents
            if inside:
                add("error", "collector_covers_home", "source dir %s%s covers home %s"
                    % (src, " (%s)" % origin if origin else "", h))
    report["collector"] = {"source_dirs": [str(Path(d).expanduser()) for _, d in sources], "homes": len(homes)}


def render(report):
    lines = ["home %s: %s" % (report["home"], "ok" if report.get("ok") else "PROBLEMS")]
    for f in report["findings"]:
        lines.append("  %-7s %s%s" % (f["level"], f["code"], (": " + f["detail"]) if f.get("detail") else ""))
    for key in ("identity", "runtime", "worker", "launch_agent", "relay", "outbox", "capture", "compare_legacy",
                "roster", "collector"):
        if key in report:
            lines.append("  %s: %s" % (key, json.dumps(report[key], sort_keys=True)))
    return "\n".join(lines)


def add_command(sub):
    p = sub.add_parser("doctor", help="Read-only health check of this home (exit 1 on errors).")
    p.add_argument("--label", help="Launch agent label to check (com.synthetics.worker.*).")
    p.add_argument("--key-file", action="append", default=[], help="Channel key file to check for 0600 (repeatable).")
    p.add_argument("--compare-legacy", metavar="LEGACY_JSON",
                   help="Compare roles and limit bypass under humans.json with the pre-M25 values in this file "
                        "(senders, channel_prefixes, secretary_prefixes, bypass_sources).")
    p.add_argument("--collector-plist", action="append", default=[],
                   help="Collector launch agent plist whose --source-dir values must not cover a home (C6).")
    p.add_argument("--collector-dir", action="append", default=[],
                   help="Collector source dir that must not cover a home or a roster/ dir (repeatable, C6).")
    p.add_argument("--relay-routes", metavar="ROUTES_JSON",
                   help="Relay route file ({aliases, routes:[{sender_id, sender_home, recipient_id}]}) to compare "
                        "with this home's channel routes (M27).")
    p.add_argument("--relay-channel", default="fieldpost", help="Channel record the relay delivers as (default fieldpost).")
    p.add_argument("--json", action="store_true", help="Print the report as JSON.")
