"""Host-selected runtime and model connection, independent of employee state."""

from pathlib import Path

from .brain import BrainError, ClaudeBrain, executor_capabilities
from .brainconfig import load


def capabilities(configuration):
    """Runtime guarantees and redacted connection provenance; never credential values."""
    backend = {"claude-cli": "claude", "codex-cli": "codex"}[configuration.adapter]
    result = executor_capabilities(backend)
    result.update(configuration.metadata(), backend="configured", live_meetings=False)
    return result


class ConfiguredBrain:
    """An immutable connection selection for a worker lifetime.

    The adapter owns its agent/tool loop. Synthetics owns employee state and tool
    authority. New wire protocols belong in adapters, not in the work engine.
    Operational timeouts may change; model routing requires a new host launch.
    """

    backend = "configured"

    def __init__(self, home, config_path, timeout=300, budget=100.0, stream_output=True):
        self.configuration = load(config_path)
        self.home = Path(home).expanduser().resolve()
        config = self.configuration
        options = dict(executable=config.executable, model=config.model, timeout=timeout,
                       budget=budget, stream_output=stream_output)
        if config.adapter == "codex-cli":
            from .codexbrain import CodexBrain
            self.delegate = CodexBrain(home, connection=config.to_dict()["connection"], **options)
        else:
            self.delegate = ClaudeBrain(home, configured_login=True, **options)

    @property
    def capabilities(self):
        return capabilities(self.configuration)

    @property
    def executable(self):
        return self.configuration.executable

    @property
    def model(self):
        return self.configuration.model

    def apply_runtime(self, effective):
        """Apply operational settings without allowing runtime requests to reroute models."""
        config = self.configuration
        if (Path(effective.get("brain_config") or "").expanduser().resolve() != config.path
                or effective.get("brain_config_sha256") != config.fingerprint
                or effective.get("model") != config.model):
            raise BrainError("Configured model connection differs from the host's pinned selection.")
        self._check_current()
        self.delegate.timeout = float(effective["timeout"])
        self.delegate.budget = float(effective["budget"])
        self.delegate.stream_output = bool(effective.get("stream_output", True))

    def _check_current(self):
        if load(self.configuration.path).fingerprint != self.configuration.fingerprint:
            raise BrainError("Brain configuration changed; restart with the host-selected configuration.")

    def run(self, context):
        self._check_current()
        # Pass the configuration actually used, even to callers outside Engine.
        context = dict(context, executor=self.capabilities)
        return self.delegate.run(context)

    def yield_stop(self):
        return self.delegate.yield_stop()
