"""Matched worker revision branches from frozen original experiment deliveries.

Only this module's explicit run entry point invokes actors. Snapshot helpers and
``--initialize-snapshots`` never invoke a model or expose evaluator feedback.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing, contextmanager
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts.experiment_trials import execute, grade_artifact, write_json
from synthetics.store import Store


SELF_REVIEW = """Revisit your delivery independently against the original assignment.
Read the original project context and assignment again, inspect your implementation
and handoff, and run checks you consider appropriate. Decide whether the original
need is satisfied and whether your completion claim is supported. Investigate and
correct any problems you identify, update the delivery files, and report what is
complete and what remains. Continue using your durable work and memory tools.
"""
MANAGER_REWORK = """Consider the following project manager review against your original
assignment. Investigate the evidence, make any needed corrections, verify the result,
update the delivery files, and report what is complete and what remains. Continue
using your durable work and memory tools. The review is reproduced verbatim below.

"""


def _now():
    return datetime.now(timezone.utc).isoformat()


def _separate(first, second):
    if first == second or first in second.parents or second in first.parents:
        raise ValueError("Snapshot and employee home must be separate trees")


@contextmanager
def _quiescent(home):
    """Hold existing worker locks without rewriting their contents."""
    handles = []
    try:
        for path in sorted(home.rglob("worker.lock")):
            if path.is_symlink():
                raise ValueError("Worker lock must not be a symlink")
            handle = path.open("r")
            handles.append(handle)
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise ValueError("Employee snapshot requires stopped workers") from error
        yield
    finally:
        for handle in reversed(handles):
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()


def _sqlite_files(home):
    result = []
    for path in home.rglob("*"):
        if path.is_symlink() or not path.is_file():
            continue
        with path.open("rb") as handle:
            if handle.read(16) == b"SQLite format 3\x00":
                result.append(path)
    return result


def _manifest(root):
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = {"symlink": os.readlink(str(path))}
        elif path.is_file():
            result[relative] = {"sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                                "mode": path.stat().st_mode & 0o777}
        elif path.is_dir():
            result[relative] = {"directory": True, "mode": path.stat().st_mode & 0o777}
        else:
            raise ValueError("Unsupported artifact type: %s" % path)
    return result


def snapshot_case(home, destination):
    """Freeze a stopped employee's complete state, including committed WAL data.

    The destination is new and outside the employee home. SQLite databases are
    backed up through SQLite, rather than copying a possibly incomplete main file.
    Other files and symlinks retain their original contents and permissions.
    """
    raw_home, raw_destination = Path(home).expanduser(), Path(destination).expanduser()
    if raw_home.is_symlink() or raw_destination.is_symlink():
        raise ValueError("Home and snapshot roots must not be symlinks")
    home, destination = raw_home.resolve(), raw_destination.resolve()
    _separate(home, destination)
    if not home.is_dir() or not (home / "state.sqlite3").is_file():
        raise ValueError("Snapshot needs an initialized employee home")
    if destination.exists():
        raise ValueError("Snapshot destination must be new")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".snapshot-", dir=str(destination.parent)))
    try:
        with _quiescent(home):
            databases = _sqlite_files(home)
            sidecars = {str(path) + suffix for path in databases for suffix in ("-wal", "-shm", "-journal")}

            def ignore(directory, names):
                return [name for name in names if str(Path(directory) / name) in sidecars]

            frozen = temporary / "home"
            shutil.copytree(home, frozen, symlinks=True, ignore=ignore)
            for path in databases:
                target = frozen / path.relative_to(home)
                # The copied main file is replaced with one consistent snapshot.
                target.unlink()
                with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as source:
                    with closing(sqlite3.connect(str(target))) as copied:
                        source.backup(copied)
                shutil.copystat(path, target)
            metadata = {"version": 1, "employee_home": str(home), "created_at": _now(),
                        "files": _manifest(frozen),
                        "sqlite_databases": [path.relative_to(home).as_posix() for path in databases]}
            write_json(temporary / "snapshot.json", metadata)
        temporary.rename(destination)
        return metadata
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _read_snapshot(snapshot):
    snapshot = Path(snapshot).resolve()
    metadata = json.loads((snapshot / "snapshot.json").read_text())
    if metadata.get("version") != 1 or _manifest(snapshot / "home") != metadata.get("files"):
        raise ValueError("Snapshot content does not match its manifest")
    return snapshot, metadata


def restore_case(snapshot, home):
    """Restore at the original path, so both revision arms see identical paths."""
    snapshot, metadata = _read_snapshot(snapshot)
    raw_home = Path(home).expanduser()
    if raw_home.is_symlink():
        raise ValueError("Employee home must not be a symlink")
    home = raw_home.resolve()
    _separate(home, snapshot)
    if str(home) != metadata["employee_home"]:
        raise ValueError("Restore must use the snapshot's original employee home")
    # Copy completely before replacing the current tree. Original branch outputs
    # must already have been archived outside this tree by the caller.
    temporary = Path(tempfile.mkdtemp(prefix=".restore-", dir=str(home.parent)))
    try:
        shutil.copytree(snapshot / "home", temporary / "home", symlinks=True)
        if home.exists():
            with _quiescent(home):
                shutil.rmtree(home)
        (temporary / "home").rename(home)
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    return metadata


def initialize_snapshots(output):
    """Freeze all original workers before allowing either revision branch."""
    output = Path(output).resolve()
    protocol = json.loads((output / "protocol.json").read_text())
    destination = output / "followup" / "snapshots"
    if destination.exists():
        raise ValueError("Original worker snapshots already exist")
    homes = [output / "cases" / case["id"] for case in protocol["cases"]]
    if any(not (home / "worker.json").is_file() for home in homes):
        raise ValueError("Every original worker must finish before snapshots are initialized")
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=".snapshots-", dir=str(destination.parent)))
    try:
        for home in homes:
            snapshot_case(home, temporary / home.name)
        temporary.rename(destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return destination


def eligible_branches(result):
    """Evaluator results choose branches but are never sent to an actor."""
    grade = result.get("grade") or {}
    if grade.get("behavioral_pass") is not False or grade.get("infrastructure_error"):
        return []
    branches = []
    review = result.get("review") or {}
    if review.get("disposition") == "rework":
        branches.append("manager-rework")
    if (result["case"]["condition"] == "baseline"
            and grade.get("claimed_complete_but_failed") is True):
        branches.append("self-review")
    return branches


def _frozen_review(result):
    path = Path(result["manager_home"]) / "workspace" / "review.json"
    verbatim = path.read_text()
    if json.loads(verbatim) != result["review"]:
        raise ValueError("Frozen manager review no longer matches the original result")
    return verbatim


def run_followups(output):
    """Run at most two cases concurrently; each case's revisions stay sequential."""
    output = Path(output).resolve()
    protocol = json.loads((output / "protocol.json").read_text())
    originals = json.loads((output / "results.json").read_text())
    followup = output / "followup"
    snapshots = followup / "snapshots"
    if not snapshots.exists():
        initialize_snapshots(output)
    # Verify the complete original cohort before starting any branch.
    for case in protocol["cases"]:
        _, frozen = _read_snapshot(snapshots / case["id"])
        if frozen["employee_home"] != str(output / "cases" / case["id"]):
            raise ValueError("Snapshot belongs to a different original worker")
    turns = protocol["worker_turns"]
    budget = protocol["worker_budget_per_turn"]
    timeout = protocol["worker_timeout_per_turn"]
    branch_plan = [{"case": result["case"]["id"], "branches": eligible_branches(result)}
                   for result in originals]
    plan_path = followup / "protocol.json"
    if plan_path.exists():
        raise ValueError("Follow-up protocol already exists; branches are not rerun")
    write_json(plan_path, {"version": 1, "created_at": _now(), "cases": branch_plan,
                          "worker_turns_per_branch": turns, "worker_budget_per_turn": budget,
                          "worker_timeout_per_turn": timeout, "self_review": SELF_REVIEW,
                          "max_concurrent_cases": 2, "branches_within_case": "sequential",
                          "manager_rework_prefix": MANAGER_REWORK,
                          "feedback_policy": "No grader verdict or held-out evidence is supplied to actors.",
                          "budget_note": "Worker revision turns and budgets are matched. The manager arm also used the original independent review budget; total system compute is not equal.",
                          "manager_review_budget": protocol["manager_budget"]})
    def revise_case(original):
        case_results = []
        case_id = original["case"]["id"]
        home, snapshot = output / "cases" / case_id, snapshots / case_id
        for branch in eligible_branches(original):
            branch_root = followup / "branches" / case_id / branch
            branch_root.mkdir(parents=True, exist_ok=False)
            progress = {"case": case_id, "branch": branch}
            print(json.dumps(dict(progress, stage="followup-start")), flush=True)
            finished = dict(progress, stage="followup-finish", success=False)
            try:
                restore_case(snapshot, home)
                if branch == "manager-rework":
                    review = _frozen_review(original)
                    (home / "workspace" / "MANAGER_REVIEW.json").write_text(review)
                    # Store.feed trims outer whitespace. A closing label keeps
                    # the frozen review's own trailing whitespace verbatim.
                    instruction = MANAGER_REWORK + review + "\nEnd of project manager review."
                    sender = "project-manager"
                    (branch_root / "manager-review.verbatim.json").write_text(review)
                else:
                    instruction, sender = SELF_REVIEW, "david"
                write_json(branch_root / "input.json", {"sender": sender, "body": instruction,
                                                       "source": branch})
                with Store(home) as store:
                    store.work_put("Reassess the assigned delivery", description=instruction,
                                   priority=100, key="followup-reassessment")
                    store.feed(sender, instruction, source=branch, external_id="followup-reassessment")
                worker = execute(home, branch, turns=turns, budget=budget, timeout=timeout)
                write_json(branch_root / "worker.json", worker)
                snapshot_case(home, branch_root / "delivery")
                grade = grade_artifact(branch_root / "delivery" / "home", branch_root / "grade")
                result = {"case": original["case"], "branch": branch, "worker": worker,
                          "grade": grade, "artifact": str(branch_root / "delivery"),
                          "original_behavioral_pass": original["grade"].get("behavioral_pass")}
                write_json(branch_root / "result.json", result)
                case_results.append(result)
                finished.update(success=True, worker_success=worker["success"],
                                behavioral_pass=grade.get("behavioral_pass"))
            except BaseException as error:
                finished["error"] = str(error)
                write_json(branch_root / "error.json", {"type": type(error).__name__, "error": str(error)})
                # Preserve partial traces and changes even when execution or grading fails.
                if home.exists() and not (branch_root / "delivery").exists():
                    snapshot_case(home, branch_root / "delivery")
                raise
            finally:
                restore_case(snapshot, home)
                print(json.dumps(finished), flush=True)
        return case_results

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [result for case_results in pool.map(revise_case, originals) for result in case_results]
    write_json(followup / "results.json", results)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--initialize-snapshots", action="store_true")
    args = parser.parse_args(argv)
    if args.initialize_snapshots:
        print(initialize_snapshots(args.output))
    else:
        results = run_followups(args.output)
        print(json.dumps({"branches": len(results), "output": str(args.output.resolve() / "followup")}))


if __name__ == "__main__":
    main()
