"""Host-provisioned reasoning configuration, separate from an employee's lifetime.

``load(path)`` reads one bounded version-1 JSON file.  It returns an immutable
``BrainConfig`` snapshot with ``executor`` and ``connection`` mappings, an absolute
``path`` and a canonical ``fingerprint``.  No credential value is read here:
authentication settings name environment variables for the selected adapter.

``metadata()`` is the deliberately smaller public/audit view.  ``to_dict()`` is a
detached normalized configuration for the adapter, including credential *names*
but never their values.  A snapshot does not follow later changes to its file.
"""

from dataclasses import dataclass
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
from types import MappingProxyType
from typing import Any, Mapping
from urllib.parse import urlsplit, urlunsplit


MAX_CONFIG_BYTES = 64 * 1024
VERSION = 1
DEFAULT_CLAUDE_MODEL = "claude-opus-5-5"
_ADAPTER_PROTOCOLS = {
    "claude-cli": frozenset(("claude-login",)),
    "codex-cli": frozenset(("chatgpt-login", "openai-responses")),
}
_ENV = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")
_HEADER = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}\Z")
_RESERVED_HEADERS = frozenset(("authorization", "cookie", "host"))


class BrainConfigError(ValueError):
    """A configuration is unavailable or invalid; messages omit supplied values."""


def _freeze(value):
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    return value


def _thaw(value):
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    return value


@dataclass(frozen=True)
class BrainConfig:
    """Validated executor/model-connection snapshot. Construct through ``load``."""

    path: Path
    fingerprint: str
    executor: Mapping[str, Any]
    connection: Mapping[str, Any]

    @property
    def adapter(self):
        return self.executor["adapter"]

    @property
    def executable(self):
        return self.executor["executable"]

    @property
    def protocol(self):
        return self.connection["protocol"]

    @property
    def model(self):
        return self.connection["model"]

    def metadata(self):
        """Safe run/export metadata; never endpoint, header or credential names."""
        return dict(adapter=self.adapter, protocol=self.protocol, model=self.model,
                    config_sha256=self.fingerprint)

    def to_dict(self):
        """Return a detached normalized version-1 config for adapter consumption."""
        return dict(version=VERSION, executor=_thaw(self.executor),
                    connection=_thaw(self.connection))


def _object(value, allowed, required, label):
    if not isinstance(value, dict):
        raise BrainConfigError("%s must be an object." % label)
    if set(value) - set(allowed):
        raise BrainConfigError("%s contains unsupported fields." % label)
    if set(required) - set(value):
        raise BrainConfigError("%s lacks required fields." % label)


def _string(value, label, empty=False, maximum=4096):
    if (not isinstance(value, str) or len(value) > maximum or "\x00" in value
            or any(ord(char) < 32 or ord(char) == 127 or 0xD800 <= ord(char) <= 0xDFFF
                   for char in value)
            or value.strip() != value or (not empty and not value)):
        raise BrainConfigError("%s must be a valid string." % label)
    return value


def _environment_reference(value):
    if (not isinstance(value, str) or not _ENV.fullmatch(value)
            or value.upper().startswith("SYNTHETICS_")):
        raise BrainConfigError("Credential environment reference is invalid or reserved.")
    return value


def _executable(value, directory):
    value = _string(value, "executor.executable")
    # A bare name explicitly means PATH lookup. A relative path (./bin/codex or
    # bin/codex) is relative to the configuration, never the worker's cwd.
    try:
        candidate = Path(value).expanduser()
        if not candidate.is_absolute() and "/" not in value:
            found = shutil.which(value)
            if found is None:
                raise BrainConfigError("executor.executable was not found on PATH.")
            candidate = Path(found)
        elif not candidate.is_absolute():
            candidate = directory / candidate
        candidate = candidate.resolve()
        if not candidate.is_file() or not os.access(str(candidate), os.X_OK):
            raise BrainConfigError("executor.executable must be an executable file.")
    except (OSError, RuntimeError, ValueError) as exc:
        if isinstance(exc, BrainConfigError):
            raise
        raise BrainConfigError("executor.executable cannot be resolved.") from None
    return str(candidate)


def _base_url(value):
    value = _string(value, "connection.base_url")
    if "?" in value or "#" in value or "\\" in value or any(char.isspace() for char in value):
        raise BrainConfigError("connection.base_url must have no query, fragment or whitespace.")
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if (parsed.scheme not in ("http", "https") or not host
                or parsed.username is not None or parsed.password is not None
                or (port is not None and not 1 <= port <= 65535)):
            raise ValueError()
        # Do not resolve names: an arbitrary DNS name pointing at loopback must
        # still use HTTPS. Only literal loopback addresses and localhost qualify.
        loopback = host.lower() == "localhost"
        try:
            address = ipaddress.ip_address(host)
            loopback = (address.is_loopback if address.version == 4
                        else address == ipaddress.IPv6Address("::1"))
        except ValueError:
            pass
        if parsed.scheme == "http" and not loopback:
            raise ValueError()
        netloc = "[" + host.lower() + "]" if ":" in host else host.lower()
        if port is not None:
            netloc += ":" + str(port)
        return urlunsplit((parsed.scheme, netloc, parsed.path.rstrip("/"), "", ""))
    except ValueError:
        raise BrainConfigError("connection.base_url must use HTTPS, or HTTP on loopback, without URL credentials.") from None


def _connection(value, adapter):
    _object(value, ("protocol", "model", "base_url", "auth", "headers_from_env"),
            ("protocol",), "connection")
    protocol = value["protocol"]
    if not isinstance(protocol, str) or protocol not in _ADAPTER_PROTOCOLS[adapter]:
        raise BrainConfigError("connection.protocol is unsupported by executor.adapter.")
    gateway = protocol == "openai-responses"
    if not gateway and set(value) - {"protocol", "model"}:
        raise BrainConfigError("Login connections cannot contain gateway settings.")
    if gateway and not {"model", "base_url", "auth"}.issubset(value):
        raise BrainConfigError("Gateway connection requires model, base_url and auth.")
    default = DEFAULT_CLAUDE_MODEL if adapter == "claude-cli" else ""
    model = _string(value.get("model", default), "connection.model",
                    empty=adapter == "codex-cli" and not gateway, maximum=512)
    result = dict(protocol=protocol, model=model)
    if not gateway:
        return result
    result["base_url"] = _base_url(value["base_url"])
    auth = value["auth"]
    _object(auth, ("type", "variable"), ("type", "variable"), "connection.auth")
    if auth["type"] != "bearer-env":
        raise BrainConfigError("connection.auth.type is unsupported.")
    result["auth"] = dict(type="bearer-env", variable=_environment_reference(auth["variable"]))
    headers = value.get("headers_from_env", {})
    if not isinstance(headers, dict) or len(headers) > 32:
        raise BrainConfigError("connection.headers_from_env must be an object of at most 32 headers.")
    normalized = {}
    for name, reference in headers.items():
        if (not isinstance(name, str) or not _HEADER.fullmatch(name)
                or name.lower() in _RESERVED_HEADERS or name.lower() in normalized):
            raise BrainConfigError("connection.headers_from_env contains an invalid, duplicate or reserved header.")
        normalized[name.lower()] = _environment_reference(reference)
    result["headers_from_env"] = normalized
    return result


def _pairs(items):
    result = {}
    for key, value in items:
        if key in result:
            raise BrainConfigError("Brain config contains a duplicate field.")
        result[key] = value
    return result


def _nonfinite(_):
    raise BrainConfigError("Brain config contains a non-finite JSON number.")


def load(path):
    """Load one host-owned JSON snapshot; never resolve credential references.

    Supported connections are Claude login through Claude CLI, and ChatGPT login
    or a Responses-compatible gateway through Codex CLI. Unsupported combinations
    fail explicitly. A gateway protocol declaration is not proof that its server
    or upstream model implements tools; adapters must qualify the actual loop.
    """
    try:
        config_path = Path(path).expanduser().resolve()
        with config_path.open("rb") as handle:
            raw = handle.read(MAX_CONFIG_BYTES + 1)
    except (OSError, RuntimeError, TypeError, ValueError):
        raise BrainConfigError("Brain config cannot be read.") from None
    if len(raw) > MAX_CONFIG_BYTES:
        raise BrainConfigError("Brain config exceeds 64 KiB.")
    try:
        config = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs,
                            parse_constant=_nonfinite)
    except BrainConfigError:
        raise
    except (UnicodeError, ValueError, RecursionError):
        raise BrainConfigError("Brain config must be valid UTF-8 JSON.") from None
    _object(config, ("version", "executor", "connection"),
            ("version", "executor", "connection"), "Brain config")
    if type(config["version"]) is not int or config["version"] != VERSION:
        raise BrainConfigError("Brain config version must be 1.")
    executor = config["executor"]
    _object(executor, ("adapter", "executable"), ("adapter", "executable"), "executor")
    adapter = executor["adapter"]
    if not isinstance(adapter, str) or adapter not in _ADAPTER_PROTOCOLS:
        raise BrainConfigError("executor.adapter is unsupported.")
    normalized = dict(version=VERSION,
                      executor=dict(adapter=adapter, executable=_executable(
                          executor["executable"], config_path.parent)),
                      connection=_connection(config["connection"], adapter))
    canonical = json.dumps(normalized, ensure_ascii=True, sort_keys=True,
                           separators=(",", ":")).encode("utf-8")
    return BrainConfig(path=config_path, fingerprint=hashlib.sha256(canonical).hexdigest(),
                       executor=_freeze(normalized["executor"]),
                       connection=_freeze(normalized["connection"]))
