"""Lossless presentation of repeated inputs at the model boundary.

The Engine and durable tools retain ordinary records. Only the JSON sent to an
executor uses JSON Pointers for repeated event arrays and repeated event bodies.
No lookup, summarization, clipping, or mutation of source context happens here.
"""
import copy
import json


REFERENCE_HEADER = "Snapshot references (JSON Pointers): "
REFERENCE_GUIDANCE = (
    "Only paths declared in the reference map are presentation placeholders: their null "
    "values reuse the exact material value at the target JSON Pointer in this snapshot. "
    "All other values, including literal reference-looking objects, remain source data. "
    "Resolve declared references here; each record keeps its own provenance and authority."
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


def project_context(context):
    """Detach and share only identical event payloads; all unique values remain in the snapshot.

    Return (view, references), with references kept outside the caller's data.
    Each reference maps a placeholder's JSON Pointer to an earlier material value.
    Changed auth/sender metadata is never inferred from another record, and a
    changed body never aliases an older version. Literal source keys are untouched.
    """
    arrays, bodies, references = {}, {}, {}

    def visit(value, path):
        if isinstance(value, list):
            if _event_array(path):
                key = _encoded(value)
                if key in arrays:
                    pointer = _pointer(path)
                    if _worth_ref(value, {pointer: arrays[key]}):
                        references[pointer] = arrays[key]
                        return None
            before = len(references)
            result = [visit(v, path + (i,)) for i, v in enumerate(value)]
            if _event_array(path) and len(references) == before:
                # An array containing a placeholder must never become a target.
                arrays.setdefault(key, _pointer(path))
            return result
        if not isinstance(value, dict):
            return copy.deepcopy(value)
        body_ref = None
        if (_event_record(path) and str(value.get("id", "")).startswith("event-")
                and isinstance(value.get("body"), str)):
            key = (value["id"], value["body"])
            if key in bodies:
                pointer = _pointer(path + ("body",))
                if _worth_ref(value["body"], {pointer: bodies[key]}):
                    body_ref = bodies[key]
            else:
                bodies[key] = _pointer(path + ("body",))
        result = {k: (None if k == "body" and body_ref is not None else visit(v, path + (k,)))
                  for k, v in value.items()}
        if body_ref is not None:
            references[_pointer(path + ("body",))] = body_ref
        return result

    return visit(context, ()), references


def render_context(context, preamble):
    """Complete model message; reference instructions must not outweigh saved data."""
    view, references = project_context(context)
    original = json.dumps(context, ensure_ascii=False, indent=2)
    if references:
        presented = json.dumps(view, ensure_ascii=False, indent=2)
        header = " " + REFERENCE_GUIDANCE + "\n" + REFERENCE_HEADER + _encoded(references)
        if len((header + presented).encode("utf-8")) < len(original.encode("utf-8")):
            return preamble + header + "\n\n" + presented
    return preamble + "\n\n" + original
