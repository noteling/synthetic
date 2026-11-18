"""Summarize explicit access attempts in an experiment's Claude stream traces.

This is a secondary contamination check, not a replacement for the process
sandbox. Shell commands can construct paths that are absent from their arguments;
a denial in one tool result also does not prove every access in that tool failed.
No employee state or trace is modified.
"""

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import shlex


ROOT = Path(__file__).resolve().parents[1]
PATH_KEYS = {"file_path", "path", "directory", "cwd", "workdir", "root"}
BUILTINS = {"Read", "Grep", "Glob", "Bash", "Write", "Edit", "MultiEdit"}
DENIAL = re.compile(r"operation not permitted|permission denied|sandbox[^\n]*den(?:y|ied)", re.I)
EXPLICIT_PATH = re.compile(r"(?:~/|/|\.\.?/)[^\s\"'`;|<>()\[\]{},]+")


def within(path, root):
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def blocks(value):
    """Stream-json can contain complete messages or nested content blocks."""
    if isinstance(value, dict):
        if value.get("type") in {"tool_use", "tool_result"}:
            yield value
        else:
            for item in value.values():
                yield from blocks(item)
    elif isinstance(value, list):
        for item in value:
            yield from blocks(item)


def path_fields(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key in PATH_KEYS and isinstance(item, str):
                yield key, item
            elif isinstance(item, (dict, list)):
                yield from path_fields(item)
    elif isinstance(value, list):
        for item in value:
            yield from path_fields(item)


def shell_paths(command):
    """Best effort only: retain the command for review of computed shell access."""
    found = set(EXPLICIT_PATH.findall(command))
    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = []
    for token in tokens:
        if "=" in token and not token.startswith(("/", "./", "../", "~/")):
            token = token.split("=", 1)[1]
        # Bare relative files/directories matter too; code and URLs do not.
        if ("/" in token or re.search(r"\.(?:py|md|json|jsonl|sqlite3|txt)$", token)):
            if not any(c in token for c in ("\n", " ", "://", "(", ")", ";")):
                found.add(token)
    return sorted(found)


def canonical(raw, cwd, environment):
    for key, value in environment.items():
        raw = raw.replace("${%s}" % key, value).replace("$" + key, value)
    if "$" in raw or raw.startswith("-") or "://" in raw:
        return None
    raw = raw.rstrip(".:,")
    if not raw:
        return None
    path = Path(raw).expanduser()
    return (path if path.is_absolute() else cwd / path).resolve()


def classify(path, case_root, project_root, shared_read_roots):
    # Evaluator material is forbidden even though it resides within the case.
    if within(path, case_root / "grading"):
        return "evaluator_material"
    for root in (Path.home() / ".codex", Path.home() / ".claude/projects"):
        if within(path, root):
            return "assistant_history"
    if path == Path.home() / ".claude/history.jsonl":
        return "assistant_history"
    if within(path, case_root):
        return None
    if any(within(path, root) for root in shared_read_roots):
        return None
    if within(path, project_root):
        return "outside_case_project_content"
    if within(path, Path.home()):
        return "outside_case_user_content"
    return None


def audit_trace(trace, case_root, project_root=None, shared_read_roots=None):
    trace, case_root = Path(trace), Path(case_root).resolve()
    project_root = Path(project_root or ROOT.parent).resolve()
    shared_read_roots = [Path(p).resolve() for p in (shared_read_roots or [ROOT / "synthetics"])]
    cwd = case_root / "workspace"
    environment = {
        "FIXTURE_WORKSPACE": str(cwd),
        "FIXTURE_SOURCE_DIR": str(cwd / "docs-source"),
        "FIXTURE_EMPLOYEE_HOME": str(case_root / "receiver-home"),
        "FIXTURE_RECEIVER": str(cwd / "receiver.py"),
        "SYNTHETICS_HOME": str(case_root),
    }
    uses, results, invalid_lines = {}, {}, 0
    with trace.open(encoding="utf-8") as stream:
        for line_number, line in enumerate(stream, 1):
            try:
                event = json.loads(line)
            except ValueError:
                invalid_lines += 1
                continue
            for block in blocks(event):
                if block["type"] == "tool_use":
                    tool_id = block.get("id") or "line-%d" % line_number
                    uses.setdefault(tool_id, dict(block, line=line_number))
                else:
                    results.setdefault(block.get("tool_use_id"), block)
    counts = Counter()
    suspicious, bash_commands = [], []
    for tool_id, tool in uses.items():
        name = tool.get("name", "unknown")
        counts[name] += 1
        arguments = tool.get("input", {})
        if not isinstance(arguments, dict):
            continue
        paths = list(path_fields(arguments))
        if name == "Bash":
            command = arguments.get("command", "")
            if not isinstance(command, str):
                command = ""
            bash_commands.append({"id": tool_id, "line": tool["line"], "command": command})
            paths += [("command", p) for p in shell_paths(command)]
        # Tool names from MCP do not establish OS access. Record their counts;
        # only conventional built-ins/path fields are audited here.
        result = results.get(tool_id)
        result_text = json.dumps(result.get("content", ""), ensure_ascii=False) if result else ""
        denial = bool(DENIAL.search(result_text))
        status = "denial_reported" if denial else ("result_available" if result else "no_result")
        tool_cwd = cwd
        explicit_cwd = arguments.get("cwd", arguments.get("workdir"))
        if isinstance(explicit_cwd, str):
            tool_cwd = canonical(explicit_cwd, cwd, environment) or cwd
        seen = set()
        for source, raw in paths:
            path = canonical(raw, tool_cwd, environment)
            if path is None:
                continue
            reason = classify(path, case_root, project_root, shared_read_roots)
            if name in {"Write", "Edit", "MultiEdit"} and not within(path, case_root):
                reason = reason or "outside_case_write"
            key = str(path), reason
            if reason is None or key in seen:
                continue
            seen.add(key)
            suspicious.append({
                "id": tool_id, "tool": name, "line": tool["line"],
                "argument": source, "path": str(path), "reason": reason,
                "result_status": status, "tool_is_error": result.get("is_error", False) if result else None,
            })
    denied = [item for item in suspicious if item["result_status"] == "denial_reported"]
    possible = [item for item in suspicious if item["result_status"] != "denial_reported"]
    return {
        "trace": str(trace), "unique_tool_calls": len(uses), "tool_counts": dict(sorted(counts.items())),
        "builtin_tool_calls": sum(counts[name] for name in BUILTINS),
        "invalid_json_lines": invalid_lines,
        "denial_reported_attempts": denied, "possible_exposure_or_unresolved": possible,
        "bash_commands_for_manual_review": bash_commands,
    }


def audit_case(case_root, project_root=None, shared_read_roots=None):
    case_root = Path(case_root).resolve()
    traces = [audit_trace(path, case_root, project_root, shared_read_roots)
              for path in sorted((case_root / "runs").glob("*.stream.jsonl"))]
    return {
        "case_root": str(case_root),
        "limitations": [
            "Static argument audit; computed shell paths and subprocess accesses are not exhaustively observable.",
            "A denial is tool-level evidence, not proof that all accesses in that tool were blocked.",
            "Possible exposure requires manual result/sandbox review; directory names alone need not expose file contents.",
        ],
        "summary": {
            "traces": len(traces),
            "unique_tool_calls": sum(t["unique_tool_calls"] for t in traces),
            "denial_reported_attempts": sum(len(t["denial_reported_attempts"]) for t in traces),
            "possible_exposure_or_unresolved": sum(len(t["possible_exposure_or_unresolved"]) for t in traces),
        },
        "traces": traces,
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("case_root", type=Path)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    report = audit_case(args.case_root)
    text = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(text, encoding="utf-8")
        print(json.dumps(report["summary"]))
    else:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
