#!/usr/bin/env python3
"""MTG F2 test stub for the Claude CLI in stream-json input mode. Records argv to $STUB_ARGV (one JSON line per
start). For each user line it answers with a result line "echo: <first line>", unless the text contains SLOW (sleeps
STUB_SLOW seconds first) or FAIL (is_error). Cost grows by 0.01 per turn (cumulative, like the CLI)."""
import json
import os
import sys
import time

argv_log = os.environ.get("STUB_ARGV")
if argv_log:
    with open(argv_log, "a") as f:
        f.write(json.dumps(dict(argv=sys.argv[1:], run=os.environ.get("SYNTHETICS_SESSION_RUN"))) + "\n")
cost = 0.0
for raw in sys.stdin:
    try:
        item = json.loads(raw)
    except ValueError:
        continue
    text = item.get("message", {}).get("content", "")
    if "SLOW" in text:
        time.sleep(float(os.environ.get("STUB_SLOW", "30")))
    cost += 0.01
    print(json.dumps({"type": "assistant", "message": {"content": [{"type": "text", "text": "thinking"}]}}), flush=True)
    first = text.splitlines()[0] if text else ""
    print(json.dumps({"type": "result", "subtype": "success", "is_error": "FAIL" in text,
                      "result": "echo: " + first, "total_cost_usd": round(cost, 4)}), flush=True)
