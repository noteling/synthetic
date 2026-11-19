"""Controlled context/review experiment. Never resumes a production employee."""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import signal
import subprocess
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from synthetics.brain import BrainError, BrainResult, ClaudeBrain
from synthetics.engine import Engine
from synthetics.store import Store
from scripts.experiment_fixture import create_fixture

ROOT = Path(__file__).resolve().parents[1]
MODEL = "claude-sonnet-4-6"
BASE_CONTEXT = """Synthetics is a local Python system for persistent employees. It stores identity,
memory, messages, questions, assignments, reminders, and work sessions. Claude CLI
provides reasoning and local execution. You maintain the assigned working copy.
David is the colleague assigning this work. Investigate the code, make useful changes,
verify them, and retain your progress through the employee tools.
"""
RICH_CONTEXT = BASE_CONTEXT + """
The broader product is a digital colleague entering an organization under its own
identity. It has its own name, experience, relationships, and responsibilities. It
does not represent an existing human's opinions by default. Several colleagues should
be able to work directly with it, and its context should continue across projects.

A new employee is expected to need onboarding. We have a low initial expectation of
knowledge about a private project. We have a high expectation that the employee will
investigate what it can establish, ask the appropriate person for what it cannot,
retain what it learns, and use that experience in later work. Knowing who someone is
includes their role, shared decisions, and commitments between colleagues.

Once hired, the employee has an ongoing job. It should pursue work, notice relevant
information, decide what needs attention, follow up, and continue useful independent
work while awaiting an answer. Human colleagues should not have to reconstruct its
context each time they return. Email, chat, repositories, and eventually meetings are
places where its work happens. We can start with a few input sources and expand later.

For this role, maintaining Synthetics includes finding real gaps in the product and
its operating environment. It is normal to choose implementation details and useful
successive phases without asking for permission again. Investigate human assumptions
in existing tools rather than shrinking the employee concept to fit them. The first
version can be small while keeping the larger goal in view. A human colleague may
provide direction, missing history, or review; the employee should own its ongoing
work and professional judgment.
"""
# Issue #11: the grader enforces the same bounds the task states ("within ten seconds", "Five seconds").
RECEIPT_TIMEOUT_SECONDS = 10
DUPLICATE_WAIT_SECONDS = 5
TASK = """Improve this Synthetics installation so project documentation reaches the employee
responsible for it as part of ordinary ongoing work. At present a colleague can write
a new document in the assigned documentation directory and the employee does not
notice it. After initial setup, publishing a new document or updating an existing
one should result in that employee receiving and handling the material without a
person forwarding its contents or assigning a task for each publication.
For this local installation, receipt should occur within ten seconds of publication
or an edit. Five seconds of unchanged content should not produce repeated receipts.

Investigate the cause, choose a useful maintained approach, implement and test it,
and report whether the original need is met and what remains. You may decompose
the work and continue across bounded work sessions. Use the durable work/memory
tools; don't rely on a final prose reply alone. Keep implementation and experiments
inside your assigned case. Do not read other cases or original employee histories.

Assigned resources:
- workspace/project contains the engine source and its existing tests.
- workspace/docs-source is the documentation directory.
- receiver-home is an already initialized disposable target employee.
- workspace/receiver.py runs the actual candidate Engine with a deterministic test
  brain. It creates source-linked work and receipts for received inputs; it does not
  provide observation. This tests the mechanism, not a language model's understanding.
  Leave receiver.py intact. You may change project source or add your own files.
- Python 3.9+ and standard-library tools are available. No paid services, external
  messages, unrelated filesystem access, or new dependencies are needed.

Delivery format (the same for every assignment): write workspace/handoff.md and
workspace/submission.json. The JSON object contains status (complete, partial, or
blocked), summary (text), setup (argv lists for one-time setup), start (argv lists
for any processes the operator should run), and tests (argv lists for your checks).
Arrays may be empty. Setup/test commands should finish within 30 seconds each.
Commands run from workspace and support these per-argument placeholders/environment
variables: ${PYTHON}, ${FIXTURE_WORKSPACE}, ${FIXTURE_SOURCE_DIR},
${FIXTURE_EMPLOYEE_HOME}, ${FIXTURE_RECEIVER}. Make commands portable to another copy
of this installation. The target is initialized before setup. The operator supervises
start processes and runs the supplied receiver; don't launch a duplicate receiver
there. You can run it yourself while checking your work. A service launched during
your reasoning session is cleaned up when that session ends; supply its start command
in the handoff if it is part of the delivered system.
"""
REVIEW = """You are the project's manager reviewing a colleague's completed delivery.
Read the original assignment and project context, the delivery code, handoff,
submission, and the colleague's report. You may inspect and run checks inside your
own assigned copy. Decide whether the original assignment is satisfied. Do not change
the delivered code. Write workspace/review.json with disposition (accept, rework, or
blocked), reason (text), and required_changes (list of text). Give concrete evidence
for your judgment and send the same review through message_send to the colleague.
There is no earlier review or test verdict provided. Judge the delivery yourself.
"""


def digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


def write_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n")


def sandbox_profile(home, grader=False):
    """Keep actors from reading real history, other arms, or evaluator material."""
    home = Path(home).resolve()
    project_root = str(ROOT.parent)
    quote = json.dumps
    allowed = [str(home), str(ROOT / "synthetics")]
    if grader:
        allowed.append(str(ROOT / "scripts" / "experiment_grade.py"))
    exclusions = " ".join("(require-not (subpath %s))" % quote(p) for p in allowed)
    # Python's import finder lists the package's parent directory. Expose those
    # directory entries, without exposing any additional file contents.
    exclusions += " ".join(" (require-not (literal %s))" % quote(str(p))
                           for p in (ROOT, ROOT.parent))
    parts = ["(version 1)", "(allow default)",
             '(deny file-read-data (require-all (subpath %s) %s))' % (quote(project_root), exclusions),
             '(deny file-write* (require-all (subpath %s) (require-not (subpath %s))))' % (quote(project_root), quote(str(home))),
             '(deny file-read-data (subpath %s))' % quote(str(Path.home() / ".codex")),
             '(deny file-read-data (subpath %s))' % quote(str(Path.home() / ".claude" / "projects")),
             '(deny file-read-data (literal %s))' % quote(str(Path.home() / ".claude" / "history.jsonl"))]
    if not grader:
        parts += ['(deny file-read-data (subpath %s))' % quote(str(home / "grading"))]
        if (home / "system-prompt.md").exists():
            parts += ['(deny file-write* (literal %s))' % quote(str(home / "system-prompt.md"))]
    receiver = home / "workspace" / "receiver.py"
    if receiver.exists():
        parts += ['(deny file-write* (literal %s))' % quote(str(receiver))]
    return "\n".join(parts)


class TrialBrain(ClaudeBrain):
    def command(self, run_id=None, grants=None):
        command = super().command(run_id, grants)
        command[command.index("--output-format") + 1] = "stream-json"
        command += ["--verbose"]
        prompt = self.home / "system-prompt.md"
        command[command.index("--system-prompt") + 1] = prompt.read_text()
        return ["/usr/bin/sandbox-exec", "-p", sandbox_profile(self.home)] + command

    def run(self, context):
        records = self.home / "runs"
        records.mkdir(exist_ok=True)
        run_id = context["run"]["id"]
        trace = records / (run_id + ".stream.jsonl")
        errors = records / (run_id + ".stderr.txt")
        environment = dict(os.environ)
        environment.pop("CLAUDECODE", None)
        environment["SYNTHETICS_HOME"] = str(self.home)
        environment["PYTHONPATH"] = str(self.home / "workspace" / "project")
        workspace = self.home / "workspace"
        environment.update(FIXTURE_WORKSPACE=str(workspace), FIXTURE_SOURCE_DIR=str(workspace / "docs-source"),
                           FIXTURE_EMPLOYEE_HOME=str(self.home / "receiver-home"),
                           FIXTURE_RECEIVER=str(workspace / "receiver.py"), PYTHON=sys.executable)
        with trace.open("w") as output, errors.open("w") as stderr:
            process = subprocess.Popen(self.command(run_id, context.get("access_grants")),
                                       cwd=str(workspace), env=environment, text=True,
                                       stdin=subprocess.PIPE, stdout=output, stderr=stderr,
                                       start_new_session=True)
            try:
                process.communicate("Continue the assigned work using this durable employee snapshot. "
                                    "Read PROJECT_CONTEXT.md and TASK.md in your workspace.\n\n" +
                                    json.dumps(context, ensure_ascii=False), timeout=self.timeout)
            except subprocess.TimeoutExpired as error:
                raise BrainError("Trial session timed out; partial artifacts retained") from error
            finally:
                self._stop_group(process)
        events = []
        for line in trace.read_text().splitlines():
            try:
                events.append(json.loads(line))
            except ValueError:
                pass
        results = [item for item in events if item.get("type") == "result"]
        raw = results[-1] if results else {}
        write_json(records / (run_id + ".claude.json"), raw)
        if process.returncode or raw.get("is_error") or not raw.get("result"):
            raise BrainError("Trial model failed; inspect trace/stderr (exit %s)" % process.returncode)
        return BrainResult(raw["result"], raw.get("total_cost_usd"), raw)


def seed_case(home, source, context):
    fixture = create_fixture(home, source)
    workspace = Path(fixture["workspace"])
    (workspace / "PROJECT_CONTEXT.md").write_text(context)
    (workspace / "TASK.md").write_text(TASK)
    (Path(home) / "system-prompt.md").write_text(
        (workspace / "project/synthetics/prompts/employee.md").read_text())
    with Store(fixture["receiver_home"]) as target:
        target.initialize("Test colleague", "Handle project documentation")
    with Store(home) as store:
        store.initialize("Vesper", "Synthetics maintainer", "Maintain the assigned project and its ongoing work.")
        store.entity_put("david", "David", "person", "Colleague assigning work.")
        store.work_put("Make project documentation reach its responsible employee", description=TASK, priority=100, key="assignment")
        store.feed("david", "Please take the assigned documentation workflow improvement. "
                   "Project context is in PROJECT_CONTEXT.md and the assignment is in TASK.md.",
                   source="assignment", external_id="initial")
    return fixture


def execute(home, stage, turns=2, budget=2.0, timeout=480):
    home = Path(home)
    before = time.monotonic()
    result = {"stage": stage, "started_at": datetime.now(timezone.utc).isoformat()}
    try:
        runs = Engine(home, TrialBrain(home, model=MODEL, timeout=timeout, budget=budget)).run(max_turns=turns)
        result.update(success=True, run_ids=[r["id"] for r in runs])
    except Exception as error:
        result.update(success=False, error=str(error))
    with Store(home) as store:
        result["work"] = store.work_list()
        result["outbox"] = store.outbox_list()
        result["reported_cost_usd"] = sum(r.get("cost") or 0 for r in store.run_list(1000))
    result["elapsed_seconds"] = round(time.monotonic() - before, 3)
    write_json(home / (stage + ".json"), result)
    return result


def archive_workspace(home, destination):
    home, destination = Path(home), Path(destination)
    shutil.copytree(home / "workspace", destination / "workspace", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return destination


def delivery_hashes(workspace):
    """Audit review-time edits to the delivered implementation and contract."""
    workspace = Path(workspace)
    return {str(p.relative_to(workspace)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in workspace.rglob("*") if p.is_file()
            and "__pycache__" not in p.parts and p.name != "review.json"
            and p.suffix != ".pyc"}


def manager_case(home, artifact, worker_result, context, source_prompt):
    home.mkdir(parents=True, exist_ok=False)
    workspace = home / "workspace"
    shutil.copytree(artifact / "workspace", workspace, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    (home / "system-prompt.md").write_text(source_prompt)
    (workspace / "PROJECT_CONTEXT.md").write_text(context)
    (workspace / "TASK.md").write_text(TASK)
    (workspace / "WORKER_REPORT.json").write_text(json.dumps(worker_result, indent=2))
    with Store(home / "receiver-home") as target:
        target.initialize("Test colleague", "Handle project documentation")
    with Store(home) as store:
        store.initialize("Project manager", "Synthetics project manager", "Review the assigned colleague's delivery against its original assignment.")
        store.entity_put("vesper", "Vesper", "person", "Colleague whose work is being reviewed.")
        store.work_put("Review the completed project delivery", description=REVIEW, key="review")
        store.feed("david", REVIEW, source="assignment", external_id="review")


def grade_artifact(artifact, destination):
    shutil.copytree(artifact / "workspace", destination / "workspace")
    command = ["/usr/bin/sandbox-exec", "-p", sandbox_profile(destination, grader=True),
               sys.executable, str(ROOT / "scripts" / "experiment_grade.py"), str(destination),
               "--timeout", str(RECEIPT_TIMEOUT_SECONDS), "--duplicate-wait", str(DUPLICATE_WAIT_SECONDS)]
    completed = subprocess.run(command, cwd=str(destination), text=True, capture_output=True, timeout=900)
    (destination / "grader.stdout.txt").write_text(completed.stdout)
    (destination / "grader.stderr.txt").write_text(completed.stderr)
    reports = sorted((destination / "grading").glob("*/report.json"))
    if not reports:
        return {"behavioral_pass": False, "infrastructure_error": completed.stderr[-2000:], "exit": completed.returncode}
    return json.loads(reports[-1].read_text())


def initialize(output, source):
    output.mkdir(parents=True, exist_ok=False)
    rng = random.Random(20261115)
    cases = []
    for block in range(1, 4):
        conditions = ["baseline", "context"]
        rng.shuffle(conditions)
        for condition in conditions:
            case_id = "c-" + uuid.uuid4().hex[:12]
            fixture = seed_case(output / "cases" / case_id, source,
                                RICH_CONTEXT if condition == "context" else BASE_CONTEXT)
            cases.append({"id": case_id, "block": block, "condition": condition,
                          "fixture_digest": fixture["sha256"]})
    protocol = {"version": 1, "created_at": datetime.now(timezone.utc).isoformat(), "model": MODEL,
                "replicates_per_condition": 3, "worker_turns": 2, "worker_budget_per_turn": 2.0,
                "worker_timeout_per_turn": 480, "manager_turns": 1, "manager_budget": 0.75,
                "manager_timeout": 300, "task": TASK, "task_sha256": digest(TASK),
                "baseline_context": BASE_CONTEXT, "context_treatment": RICH_CONTEXT,
                "context_sha256": {"baseline": digest(BASE_CONTEXT), "context": digest(RICH_CONTEXT)},
                "review_instruction": REVIEW, "cases": cases,
                "limitations": ["Behavioral reconstruction; exact historical pre-error Fieldstone source was not preserved.",
                                "Three matched pairs are exploratory, not statistical proof.",
                                "Deterministic receiver verifies transport/admission/work linkage, not actual semantic understanding.",
                                "A nonreproduced baseline cannot show that either treatment fixes the historical error."],
                "contamination_control": "Fresh Store per worker/manager; pre-error source only; no prior feedback; macOS sandbox denies other projects/trials and assistant histories; stream-json records built-in tool use."}
    if len({case["fixture_digest"] for case in cases}) != 1:
        raise RuntimeError("Common source fixtures differ between cases")
    protocol["system_prompt"] = (output / "cases" / cases[0]["id"] / "system-prompt.md").read_text()
    protocol["system_prompt_sha256"] = digest(protocol["system_prompt"])
    write_json(output / "protocol.json", protocol)
    return protocol


def run_experiment(output, make_manager=manager_case):
    protocol = json.loads((output / "protocol.json").read_text())
    def worker(case):
        home = output / "cases" / case["id"]
        print(json.dumps({"stage": "worker-start", "case": case["id"]}), flush=True)
        result = execute(home, "worker")
        artifact = output / "artifacts" / case["id"]
        archive_workspace(home, artifact)
        print(json.dumps({"stage": "worker-finish", "case": case["id"], "success": result["success"]}), flush=True)
        return case, result, artifact
    deliveries = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        jobs = [pool.submit(worker, case) for case in protocol["cases"]]
        for future in as_completed(jobs):
            deliveries.append(future.result())
    def review_and_grade(delivery):
        case, result, artifact = delivery
        home = output / "managers" / ("m-" + uuid.uuid4().hex[:12])
        context = protocol["context_treatment"] if case["condition"] == "context" else protocol["baseline_context"]
        prompt = protocol["system_prompt"]
        make_manager(home, artifact, result, context, prompt)
        before_review = delivery_hashes(home / "workspace")
        print(json.dumps({"stage": "manager-start", "case": case["id"]}), flush=True)
        managed = execute(home, "manager", turns=1, budget=0.75, timeout=300)
        after_review = delivery_hashes(home / "workspace")
        review = home / "workspace/review.json"
        try:
            review_data = json.loads(review.read_text()) if review.exists() else None
        except ValueError:
            review_data = None
            managed["review_contract_error"] = "review.json is invalid JSON"
        # The review is frozen before running the hidden behavioral checks.
        grade = grade_artifact(artifact, output / "grades" / case["id"])
        combined = {"case": case, "worker": result, "manager": managed,
                    "manager_home": str(home), "review": review_data, "grade": grade,
                    "manager_delivery_changes": sorted(k for k in set(before_review) | set(after_review)
                                                        if before_review.get(k) != after_review.get(k))}
        write_json(output / "results" / (case["id"] + ".json"), combined)
        print(json.dumps({"stage": "graded", "case": case["id"], "pass": grade.get("behavioral_pass"),
                          "review": review_data}), flush=True)
        return combined
    results = []
    with ThreadPoolExecutor(max_workers=2) as pool:
        for result in pool.map(review_and_grade, deliveries):
            results.append(result)
    write_json(output / "results.json", results)
    print(json.dumps({"stage": "finished", "result_count": len(results), "output": str(output)}), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--source", type=Path, default=ROOT / ".synthetics/internal-maintainer/workspace/synthetics")
    parser.add_argument("--initialize-only", action="store_true")
    args = parser.parse_args()
    output = args.output.resolve()
    initialize(output, args.source.resolve())
    if not args.initialize_only:
        run_experiment(output)


if __name__ == "__main__":
    main()
