"""This host's humans.json v1 (M25 §4 + C1), used as a test fixture. Production code holds no human IDs."""

from synthetics import capture

HOST_HUMANS = {
    "version": 1,
    "default_human": "david",
    "senders": ["david"],
    "channel_prefixes": ["codex-desktop:", "telegram:"],
    "secretary_prefixes": ["desktop-secretary-"],
    "operators": {"senders": ["codex-desktop"],
                  "source_prefixes": ["codex-desktop-", "operator-", "desktop-secretary-"]},
    "limit_bypass": {"senders": ["david"], "channel_prefixes": ["codex-desktop:", "telegram:"],
                     "require_auth": True},
}


def write_host_humans(home):
    return capture.write_humans(home, HOST_HUMANS)


# Pre-M25 behaviour on this host (base capture.LEGACY_HUMANS + engine.LIMIT_BYPASS_SOURCES), for doctor --compare-legacy.
HOST_LEGACY = {"senders": ["david"], "channel_prefixes": ["codex-desktop:", "telegram:"],
               "secretary_prefixes": ["desktop-secretary-"], "bypass_sources": ["telegram:", "codex-desktop:"]}
