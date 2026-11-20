# Model-visible context

The engine keeps full incoming events, current work, memory, relationships and execution records.
Only the JSON sent to an ordinary reasoning executor uses a lossless presentation of repeated
event data. This applies to Claude CLI, Codex CLI and their configured connections.

For example, when `events`, `pending_events` and the current run all contain the same input:

```json
{
  "events": [{"id": "event-example", "body": "The complete source text stays here."}],
  "pending_events": {"context_ref": "/events"},
  "run": {"events": {"context_ref": "/events"}}
}
```

`context_ref` replaces an identical event array. `body_ref` replaces the repeated body of the
same event ID, while keeping that record's own sender, authentication, source, thread, reply
and sequence metadata. Both use JSON Pointers into the same snapshot. Slash and tilde in a
pointer component are escaped as `~1` and `~0`. Targets are earlier material values; references
do not point to another reference. Short values stay inline when a reference would cost more.

The executor's instruction explains how to read these references. Every unique historical body
and every changed version remains present. No memory or source record is deleted, summarized,
clipped or promoted to a stronger authority. Expanding the pointers reproduces the original
context, including provenance. Input arrays outside the known engine fields and literal user
data are left alone.

This is a presentation boundary: Engine callbacks, Store/CLI state, run fencing, input admission,
profile selection, questions, delivery and tool effects retain their existing contracts. A fresh
session still starts from Synthetics-owned state. The selected model, budget and routine cadence
are unchanged.

`tests/test_prompt_context_acceptance.py` drives a normal interrupted/restarted engine through
actual CLI subprocess boundaries for all four runtime selections. It verifies that a long
authenticated source appears once in stdin, that expansion gives the exact engine snapshot,
and that the original event remains retrievable intact. `tests/test_promptcontext.py` covers
metadata differences, changed bodies, escaped pointer keys, small inputs and determinism.

Size measurements refer to serialized UTF-8 bytes. They are not a measured token or subscription
quota saving; executor token telemetry must be compared separately.
