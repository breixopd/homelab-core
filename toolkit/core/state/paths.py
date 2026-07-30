"""Runtime state paths (deploy logs, verify cache, hook audit)."""

from __future__ import annotations

import json
from pathlib import Path

# Operational artifacts from deploy/verify/hooks — not source code.
STATE_DIR_NAME = ".homelab-state"


def state_dir(root: Path) -> Path:
    return root.resolve() / STATE_DIR_NAME


def https_probe_cache_path(root: Path) -> Path:
    return state_dir(root) / "https-probe-cache.json"


def last_verify_path(root: Path) -> Path:
    return state_dir(root) / "last-verify.json"


def load_last_verify_summary(root: Path) -> dict | None:
    """Read the last-verify report JSON. Returns ``None`` if missing or unreadable."""
    path = last_verify_path(root)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def last_hooks_path(root: Path) -> Path:
    return state_dir(root) / "last-hooks.json"


def watchdog_events_path(root: Path) -> Path:
    return state_dir(root) / "watchdog-events.jsonl"


def watchdog_state_path(root: Path) -> Path:
    return state_dir(root) / "watchdog-state.json"


def deploy_log_dir(root: Path) -> Path:
    return state_dir(root)
