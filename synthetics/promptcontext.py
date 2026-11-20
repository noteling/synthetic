"""Lossless presentation of repeated inputs at the model boundary.

The Engine and durable tools retain ordinary records. Only the JSON sent to an
executor uses JSON Pointers for repeated event arrays and repeated event bodies.
No lookup, summarization, clipping, or mutation of source context happens here.
"""
import copy
import json


REFERENCE_GUIDANCE = (
    "Repeated input arrays may be represented as {\"context_ref\":\"/events\"}; "
    "an event's body_ref similarly points to its exact body. These are JSON Pointers "
    "within this snapshot, not missing information or new messages. Resolve them here; "
    "source/auth and reply metadata still belong to each record."
)


def _event_array(path):
    return (path in (("events",), ("pending_events",), ("run", "events"))
            or (len(path) == 3 and path[0] == "recent_runs" and isinstance(path[1], int)
                and path[2] == "events"))


def _event_record(path):
    if not path or not isinstance(path[-1], int):
        return False
    return (_event_array(path[:-1])
            or (len(path) == 4 and path[0] == "threads" and path[2] == "items"))


def _pointer(path):
    return "/" + "/".join(str(p).replace("~", "~0").replace("/", "~1") for p in path)


def _encoded(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _worth_ref(original, replacement):
    # Tiny messages should not get larger because their reference is longer.
    return len(_encoded(original).encode("utf-8")) - len(_encoded(replacement).encode("utf-8")) >= 32


def compact_context(context):
    """Detach and share only identical event payloads; all unique values remain in the snapshot.

    context_ref points to a complete earlier event array. body_ref points directly
    to an earlier string of the same event ID with exactly the same text. Changed
    auth/sender metadata is never inferred from another record, and a changed body
    never aliases an older version. References never form chains or cycles.
    """
    arrays, bodies = {}, {}

    def visit(value, path):
        if isinstance(value, list):
            if _event_array(path):
                key = _encoded(value)
                if key in arrays:
                    ref = {"context_ref": arrays[key]}
                    if _worth_ref(value, ref):
                        return ref
                else:
                    arrays[key] = _pointer(path)
            return [visit(v, path + (i,)) for i, v in enumerate(value)]
        if not isinstance(value, dict):
            return copy.deepcopy(value)
        body_ref = None
        if (_event_record(path) and str(value.get("id", "")).startswith("event-")
                and isinstance(value.get("body"), str) and "body_ref" not in value):
            key = (value["id"], value["body"])
            if key in bodies:
                ref = {"body_ref": bodies[key]}
                if _worth_ref({"body": value["body"]}, ref):
                    body_ref = bodies[key]
            else:
                bodies[key] = _pointer(path + ("body",))
        result = {k: visit(v, path + (k,)) for k, v in value.items()
                  if not (k == "body" and body_ref is not None)}
        if body_ref is not None:
            result["body_ref"] = body_ref
        return result

    return visit(context, ())


def render_context(context):
    """Model JSON only; keep whitespace stable for prompt caching and human inspection."""
    return json.dumps(compact_context(context), ensure_ascii=False, indent=2)
