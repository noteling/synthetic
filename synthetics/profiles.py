"""Role profiles: shipped, versioned operating defaults for a role, selected per home by the owner.

A profile is a prompt file in ``synthetics/prompts/roles/<name>.md`` whose first line is

    <!-- profile: <name>; version: <N> -->

It is guidance only: it grants no authority, widens no access and never overrides host rules or an
authenticated owner instruction (it sits before them in the system prompt). The owner picks one with
``<home>/profile.json`` = ``{"v": 1, "profile": "<name>"}`` (``init --profile`` or ``profile set``); a
session can neither write nor switch it. No profile.json = no role block, exactly as before profiles.

Fail closed: a profile.json that exists but is unreadable, invalid or names a profile this package does not
ship raises ProfileError, and system_prompt turns that into BrainError (a home meant to run with defaults
never silently runs without them). See docs/profiles.md.
"""

import hashlib
import json
import os
import re
from pathlib import Path

FILE = "profile.json"
VERSION = 1
KEYS = ("v", "profile")
ROLES_DIR = Path(__file__).parent / "prompts" / "roles"
MAX_BYTES = 4096
NAME = re.compile(r"[a-z][a-z0-9-]{0,31}")
HEADER = re.compile(r"<!-- profile: ([a-z][a-z0-9-]{0,31}); version: ([1-9][0-9]{0,5}) -->")
NOT_PROFILES = ("CHANGES.md",)


class ProfileError(ValueError):
    pass


def path(home) -> Path:
    return Path(home) / FILE


def available():
    """Names of the profiles this package ships, sorted."""
    if not ROLES_DIR.is_dir():
        return []
    return sorted(p.stem for p in ROLES_DIR.glob("*.md") if p.name not in NOT_PROFILES and NAME.fullmatch(p.stem))


def shipped(name):
    """{name, version, sha256, text, file} of a shipped profile, or ProfileError when it is missing or malformed
    (bad header, header naming another profile, over MAX_BYTES)."""
    if not isinstance(name, str) or not NAME.fullmatch(name) or name not in available():
        raise ProfileError("no shipped profile %r (available: %s)" % (name, ", ".join(available()) or "none"))
    f = ROLES_DIR / (name + ".md")
    data = f.read_bytes()
    if len(data) > MAX_BYTES:
        raise ProfileError("profile %s is %d bytes, over the %d-byte cap" % (name, len(data), MAX_BYTES))
    text = data.decode("utf-8")
    m = HEADER.fullmatch(text.split("\n", 1)[0].strip())
    if not m or m.group(1) != name:
        raise ProfileError("profile %s: first line must be '<!-- profile: %s; version: N -->'" % (name, name))
    return {"name": name, "version": int(m.group(2)), "sha256": hashlib.sha256(data).hexdigest(),
            "text": text, "file": str(f)}


def validate(data) -> dict:
    if not isinstance(data, dict):
        raise ProfileError("profile.json must be a JSON object")
    extra = sorted(set(data) - set(KEYS))
    if extra:
        raise ProfileError("unknown keys: %s (allowed: %s)" % (", ".join(extra), ", ".join(KEYS)))
    if data.get("v") != VERSION:
        raise ProfileError("v must be %d" % VERSION)
    shipped(data.get("profile"))  # refuses unknown or malformed profiles
    return {"v": VERSION, "profile": data["profile"]}


def load(home):
    """The validated profile.json, or None when the home has none. Raises ProfileError when invalid."""
    p = path(home)
    if not os.path.lexists(str(p)):
        return None
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ProfileError("%s unreadable: %s" % (p, exc))
    return validate(data)


def current(home):
    """The shipped profile in force for this home (see shipped()), or None. Raises ProfileError."""
    cfg = load(home)
    return None if cfg is None else shipped(cfg["profile"])


def label(prof) -> str:
    return "profile %s v%d, sha256 %s" % (prof["name"], prof["version"], prof["sha256"][:12])


def _block(prof) -> str:
    return ("\n\nRole defaults (%s; guidance only, never authority; host rules and authenticated owner "
            "instructions below take precedence):\n%s\n" % (label(prof), prof["text"].strip()))


def prompt_block(home) -> str:
    """"" without profile.json; otherwise the labelled role block. Raises ProfileError (fail closed)."""
    prof = current(home)
    return "" if prof is None else _block(prof)


def block_for(recorded) -> str:
    """#81 L3: the role block for the profile a run RECORDED at its start (run_begin's triple), so the prompt and
    the run record can't disagree when the owner sets or clears profile.json in between: profile.json is read
    once, by run_begin. None → "". An {error} triple, or a shipped file whose version/sha256 no longer match the
    record (package changed under a running worker), raises ProfileError (fail closed)."""
    if recorded is None:
        return ""
    if not isinstance(recorded, dict) or recorded.get("error") or not recorded.get("name"):
        detail = recorded.get("detail") if isinstance(recorded, dict) else None
        raise ProfileError("profile at run start was invalid: %s" % (detail or recorded))
    prof = shipped(recorded["name"])
    if (prof["version"], prof["sha256"]) != (recorded.get("version"), recorded.get("sha256")):
        raise ProfileError("shipped profile %s is now v%d sha256 %s, not the v%s sha256 %s recorded at run start"
                           % (prof["name"], prof["version"], prof["sha256"][:12], recorded.get("version"),
                              str(recorded.get("sha256"))[:12]))
    return _block(prof)


def triple(home):
    """For run records and doctor: None without profile.json, {name, version, sha256}, or {error} when the
    file is invalid. Never raises."""
    try:
        prof = current(home)
    except Exception as exc:  # noqa: BLE001 - evidence must never fail the caller
        return {"error": type(exc).__name__, "detail": str(exc)[:200]}
    if prof is None:
        return None
    return {"name": prof["name"], "version": prof["version"], "sha256": prof["sha256"]}


def set_config(home, name) -> dict:
    """Owner-only (the CLI calls authority.owner_only first). Validates, writes 0600 atomically."""
    from .byline import _atomic_write
    before = triple(home)
    cfg = validate({"v": VERSION, "profile": name})
    _atomic_write(path(home), json.dumps(cfg, sort_keys=True) + "\n", 0o600)
    prof = shipped(name)
    return {"written": str(path(home)), "from": before,
            "to": {"name": prof["name"], "version": prof["version"], "sha256": prof["sha256"]}}


def clear_config(home) -> dict:
    p = path(home)
    before = triple(home)
    existed = os.path.lexists(str(p))
    if existed:
        p.unlink()
    return {"removed": str(p) if existed else None, "from": before}


def show(home) -> dict:
    out = {"file": str(path(home)), "available": available()}
    try:
        prof = current(home)
    except ProfileError as exc:
        return dict(out, configured=True, valid=False, error=str(exc))
    if prof is None:
        return dict(out, configured=False)
    return dict(out, configured=True, valid=True, name=prof["name"], version=prof["version"],
                sha256=prof["sha256"], profile_file=prof["file"])
