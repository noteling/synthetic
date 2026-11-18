"""Optional real-Claude check of older conversation retrieval and durable reply.

Consumes the configured Claude CLI allocation. Uses an isolated temporary employee
with synthetic history; no Telegram requests or live employee mutations occur.
"""

import argparse
import json
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from synthetics.brain import ClaudeBrain
from synthetics.engine import Engine
from synthetics.store import Store


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", required=True)
    parser.add_argument("--claude", default="claude")
    parser.add_argument("--model", default="claude-sonnet-4-6")
    parser.add_argument("--budget", type=float, default=1)
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix="synthetics-channel-model-check-") as temporary:
        home = Path(temporary) / "employee"
        with Store(home) as store:
            store.initialize("Mira", "Temporary conversation acceptance tester",
                             "Answer one source-backed conversation question with durable tools. No coding or external communication.")
            store.entity_put("david", "David", "person")
            items = [dict(id="source-0", sender="david", direction="incoming",
                          body="For our temporary acceptance exercise, the release codeword is amber.")]
            items += [dict(id="source-" + str(i), sender="david" if i % 2 else "past-assistant",
                           direction="incoming" if i % 2 else "outgoing",
                           body="Historical acknowledgement " + str(i)) for i in range(1, 41)]
            store.thread_import("history-check", items, source="synthetic-test-fixture")
            event = store.feed("david", "This is a temporary acceptance check, no real project changes. "
                               "What release codeword did I give at the start of this conversation? "
                               "Retrieve the older conversation page if needed. Send one concise outbox reply "
                               "in this same thread, linked to this input, and remember the sourced codeword. "
                               "Do not contact any external service or modify files.",
                               thread="history-check", external_id="history-question")
        brain = ClaudeBrain(home, executable=args.claude, model=args.model, timeout=180, budget=args.budget)
        completed = Engine(home, brain).run()
        with Store(home) as store:
            outbox, memories = store.outbox_list(), store.memory_search(limit=200)
            receipt_file = home / "runs" / (completed[0]["id"] + ".mcp.jsonl")
            receipts = [json.loads(line) for line in receipt_file.read_text().splitlines()] if receipt_file.exists() else []
            checks = dict(completed=completed[0]["status"] == "finished", one_reply=len(outbox) == 1,
                          correct_answer=bool(outbox) and "amber" in outbox[0]["body"].lower(),
                          same_thread=bool(outbox) and outbox[0].get("thread") == "history-check",
                          reply_link=bool(outbox) and outbox[0].get("in_reply_to") == event["id"],
                          older_history_requested=any(r["tool"] == "thread_history" and r["success"] for r in receipts),
                          retained_sourced_memory=any("amber" in m["value"].lower() and m["source"] for m in memories))
            report = dict(checks=checks, cost_usd=completed[0].get("cost"), outbox=outbox, memories=memories,
                          receipts=receipts, fixture="isolated employee; no Telegram requests or live memory changes")
            path = Path(args.report).expanduser().resolve()
            path.write_text(json.dumps(report, indent=2) + "\n")
            path.chmod(0o600)
            print(json.dumps(dict(checks=checks, cost_usd=report["cost_usd"], report=str(path))))
            return 0 if all(checks.values()) else 1


if __name__ == "__main__":
    raise SystemExit(main())
