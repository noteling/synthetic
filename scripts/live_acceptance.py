"""Opt-in real-Claude exercise. Uses a NEW test employee and consumes model usage."""

import argparse
import json
from pathlib import Path
import sys

# Support execution from the checkout without requiring an editable install.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from synthetics.brain import ClaudeBrain
from synthetics.engine import Engine
from synthetics.store import Store


def exercise(home, model="sonnet", executable="claude"):
    home = Path(home).expanduser().resolve()
    if (home / "state.sqlite3").exists():
        raise ValueError("Choose a NEW home for the test employee; existing memory is never reused.")
    with Store(home) as store:
        identity = store.initialize("Rowan", "Noteling maintainer in a fictional acceptance-test organization",
                                    "Demonstrate onboarding, communication, and memory using only assigned fixture material.")
        store.entity_put("david", "David", "person", "Colleague providing test onboarding.")
        store.entity_put("mina", "Mina", "person", "A colleague introduced in the test notes.")
        store.workspace.joinpath("onboarding.txt").write_text(
            "Fictional acceptance-test organization, unrelated to the real Noteling release policy.\n"
            "In this organization, blue hour means our weekly release readiness review.\n"
            "Mina coordinates that review. We have not documented who decides to publish.\n"
            "No repository access is assigned. Use only this workspace.\n", encoding="utf-8")
        work = store.work_put("Prepare an onboarding handoff", description=
                              "Read onboarding.txt, retain useful knowledge about people and terminology. "
                              "Ask David who decides to publish; this cannot be established from the assigned material. "
                              "After his reply, write handoff.md documenting the term, Mina's role, and publication authority. "
                              "Wait for that answer before finishing the assignment.")
        store.feed("david", "Welcome. Please start the onboarding handoff assignment. Investigate the assigned notes, "
                   "ask me for the missing decision, and retain what you learn. This is fictional test data, not real Noteling policy.",
                   source="acceptance", external_id="welcome")
    def progress(event):
        print(json.dumps(event), flush=True)
    brain = ClaudeBrain(home, executable=executable, model=model, timeout=240, budget=1.0)
    first = Engine(home, brain, on_update=progress).run()
    with Store(home) as store:
        questions = store.question_list("open")
        assert questions, "The employee did not ask for the missing publication decision."
        question = next(q for q in questions if q["to"] == "david")
        assert store.work_get(work["id"])["status"] == "waiting", "The work did not wait for its answer."
        answer = store.feed("david", "Mina makes the final publication decision. I can explain history, but Mina decides when we publish.",
                            reply_to=question["id"], source="acceptance", external_id="publication-answer")
    # Construct a new Engine: no CLI session is resumed.
    second = Engine(home, brain, on_update=progress).run(max_turns=2)
    with Store(home) as store:
        assert store.work_get(work["id"])["status"] == "done", "The assignment did not finish after its answer."
        artifact = store.workspace.joinpath("handoff.md").read_text(encoding="utf-8")
        assert "mina" in artifact.lower() and "blue hour" in artifact.lower(), "The handoff omitted learned context."
        memories = store.memory_search()
        assert memories, "No learning was retained."
        # Move original input beyond the five-session context window without model
        # calls. Original events must still be accessible through durable tools.
        for _ in range(6):
            run = store.run_begin()
            store.run_finish(run["id"], "Offline fixture housekeeping; no new experience.")
        later = store.work_put("Apply memory in another project", description=
                               "Write cross-project.md answering who Mina is to you, what blue hour means, and who makes the publication decision. "
                               "Revisit the original evidence using your event tools. Cite original answer event " + answer["id"] +
                               " and onboarding.txt. Treat this as a different project; do not ask for the same KT again.")
        store.feed("david", "You are helping on another project now. Complete the cross-project memory assignment "
                   "using what you learned, and revisit the cited original event for the decision. Keep fictional facts labeled as test data.",
                   source="acceptance", external_id="other-project")
    third = Engine(home, brain, on_update=progress).run(max_turns=2)
    receipts = []
    for run in third:
        path = home / "runs" / (run["id"] + ".mcp.jsonl")
        if path.exists():
            receipts.extend(json.loads(line) for line in path.read_text(encoding="utf-8").splitlines())
    assert any(row.get("tool") == "event_get" and row.get("arguments", {}).get("id") == answer["id"]
               and row.get("success") for row in receipts), "No successful tool receipt proves the original answer was retrieved."
    with Store(home) as store:
        assert store.work_get(later["id"])["status"] == "done", "Cross-project assignment unfinished."
        artifact = store.workspace.joinpath("cross-project.md").read_text(encoding="utf-8")
        assert answer["id"] in artifact, "The original answer source was not cited."
        assert "mina" in artifact.lower() and "blue hour" in artifact.lower(), "Prior learning was lost."
        assert store.identity()["id"] == identity["id"], "The employee identity changed."
        report = {
            "passed": True, "employee_id": identity["id"], "home": str(home),
            "checks": ["investigation and question", "waiting for correlated answer", "fresh-session resumption",
                       "retained learning", "completed workspace artifact", "cross-project memory",
                       "original evidence retrieved beyond recent sessions (tool receipt)", "stable employee identity"],
            "model_sessions": len(first + second + third),
            "reported_cost_usd": sum(r.get("cost") or 0 for r in first + second + third),
            "artifacts": [str(store.workspace / name) for name in ("handoff.md", "cross-project.md")],
        }
    (home / "acceptance.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", required=True)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--claude", default="claude")
    args = parser.parse_args()
    print(json.dumps(exercise(args.home, args.model, args.claude), indent=2))


if __name__ == "__main__":
    main()
