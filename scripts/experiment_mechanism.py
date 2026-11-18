"""Secondary mechanism diagnostic for frozen experiment deliveries.

The primary grade and original artifact stay unchanged. This diagnostic removes
two operator-lifecycle ambiguities from a separate copy, then uses the same blind
grader. Passing here is not passing the originally submitted delivery.
"""

import hashlib
import json
from pathlib import Path
import re
import shutil

from scripts import experiment_trials


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def _manifest(root):
    result = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            result[relative] = {"type": "symlink", "target": str(path.readlink())}
        elif path.is_file():
            result[relative] = {"type": "file", "sha256": _sha(path.read_bytes())}
    return result


def _receiver_start(argv):
    """Recognize a direct Python invocation of the supplied workspace receiver.

    Shell/env wrappers, -m/-c commands, and differently named scripts are retained.
    The exact removed argv is always recorded for inspection.
    """
    if not isinstance(argv, list) or len(argv) < 2 or not all(isinstance(arg, str) for arg in argv):
        return False
    if argv[0] != "${PYTHON}" and not re.fullmatch(r"python(?:\d+(?:\.\d+)*)?", Path(argv[0]).name):
        return False
    index = 1
    while index < len(argv) and argv[index] in ("-u", "-B", "-E", "-I", "-s", "-S", "-O", "-OO", "-q"):
        index += 1
    if index == len(argv):
        return False
    script = argv[index]
    if script in ("${FIXTURE_RECEIVER}", "${FIXTURE_WORKSPACE}/receiver.py", "receiver.py", "./receiver.py"):
        return True
    path = Path(script)
    return path.is_absolute() and path.name == "receiver.py" and path.parent.name == "workspace"


def grade_mechanism(artifact, destination):
    """Grade a normalized copy, recording every change and immutable-source proof.

    Apply this same diagnostic to every case. Clearing tests avoids assuming that
    submitted checks can run before their services. Removing a directly declared
    supplied receiver avoids duplicating the receiver already run by the grader.
    Neither operation changes implementation code or repairs its behavior.
    """
    artifact, destination = Path(artifact).resolve(), Path(destination).resolve()
    if artifact == destination or artifact in destination.parents or destination in artifact.parents:
        raise ValueError("Artifact and diagnostic destination must be separate trees")
    if destination.exists():
        raise ValueError("Diagnostic destination must be new")
    workspace = artifact / "workspace"
    if not workspace.is_dir() or workspace.is_symlink():
        raise ValueError("Artifact needs a regular workspace directory")
    original_bytes = (workspace / "submission.json").read_bytes()
    submission = json.loads(original_bytes)
    if not isinstance(submission, dict):
        raise ValueError("Submission must be an object")
    starts = submission.get("start", [])
    if not isinstance(starts, list):
        raise ValueError("Submission start must be a list")
    original_manifest = _manifest(workspace)
    destination.mkdir(parents=True)
    normalized = destination / "normalized-artifact"
    shutil.copytree(workspace, normalized / "workspace", symlinks=True)
    (destination / "original-submission.json").write_bytes(original_bytes)

    removed = [{"index": index, "argv": argv, "reason": "The unchanged grader provides the supplied receiver independently."}
               for index, argv in enumerate(starts) if _receiver_start(argv)]
    normalized_submission = dict(submission)
    normalized_submission["tests"] = []
    normalized_submission["start"] = [argv for argv in starts if not _receiver_start(argv)]
    normalized_path = normalized / "workspace/submission.json"
    normalized_path.write_text(json.dumps(normalized_submission, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    normalized_manifest = _manifest(normalized / "workspace")
    changed = sorted(key for key in set(original_manifest) | set(normalized_manifest)
                     if original_manifest.get(key) != normalized_manifest.get(key))
    unchanged = all(key == "submission.json" for key in changed)
    if not unchanged:
        raise ValueError("Normalization unexpectedly changed implementation material")

    report = {
        "schema_version": 1,
        "diagnostic_kind": "secondary_mechanism_diagnostic",
        "original_delivery_pass": None,
        "interpretation": "Passing tests the normalized mechanism only; it does not replace the primary grade or establish that the original delivery passed.",
        "artifact": str(artifact), "normalized_artifact": str(normalized),
        "original_submission_sha256": _sha(original_bytes),
        "normalized_submission_sha256": _sha(normalized_path.read_bytes()),
        "normalization": [
            {"field": "tests", "operation": "replace", "before": submission.get("tests", []), "after": [],
             "reason": "Avoid assuming submitted checks run before services; apply uniformly to all cases."},
            {"field": "start", "operation": "remove_direct_supplied_receiver", "removed": removed},
        ],
        "workspace_changes": changed,
        "non_submission_workspace_unchanged": unchanged,
        "secondary_mechanism_pass": False,
    }
    experiment_trials.write_json(destination / "original-workspace-manifest.json", original_manifest)
    experiment_trials.write_json(destination / "normalized-workspace-manifest.json", normalized_manifest)
    try:
        report["grade"] = experiment_trials.grade_artifact(normalized, destination / "grade")
        report["secondary_mechanism_pass"] = report["grade"].get("behavioral_pass") is True
    except Exception as error:
        report["grade"] = {"behavioral_pass": False, "infrastructure_error": str(error),
                           "exception_type": type(error).__name__}
    report["original_artifact_unchanged"] = _manifest(workspace) == original_manifest
    report["normalized_artifact_unchanged_during_grading"] = _manifest(normalized / "workspace") == normalized_manifest
    if not report["original_artifact_unchanged"] or not report["normalized_artifact_unchanged_during_grading"]:
        report["secondary_mechanism_pass"] = False
        report["integrity_error"] = "An artifact changed during secondary grading."
    experiment_trials.write_json(destination / "report.json", report)
    return report
