"""Workstation safety — block host mutations on the deploy laptop by default."""

from __future__ import annotations

import os


def is_guest_runtime() -> bool:
    """True when hooks/compose run inside an LXC guest (HOMELAB_NODE set)."""
    return bool(os.environ.get("HOMELAB_NODE", "").strip())


def is_dedicated_deploy_controller() -> bool:
    """Explicit opt-in: this machine is the homelab deploy controller, not a dev laptop."""
    return os.environ.get("HOMELAB_DEPLOY_CONTROLLER", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def allow_env(operation: str) -> bool:
    """Per-operation override: HOMELAB_ALLOW_<OPERATION>=1."""
    key = f"HOMELAB_ALLOW_{operation.upper().replace('-', '_')}"
    return os.environ.get(key, "").strip().lower() in ("1", "true", "yes")


def skip_on_workstation(operation: str) -> bool:
    """Return True when a host mutation should be skipped on the developer workstation."""
    if is_guest_runtime() or is_dedicated_deploy_controller() or allow_env(operation):
        return False
    if os.environ.get("HOMELAB_CONTROLLER_SAFE", "").strip().lower() in ("1", "true", "yes"):
        return True
    # Default: treat non-guest, non-dedicated hosts as workstations (safe mode on).
    return True


def skip_message(operation: str) -> str:
    return (
        f"{operation}: skipped on workstation "
        "(set HOMELAB_DEPLOY_CONTROLLER=1 on a dedicated controller, "
        "or HOMELAB_ALLOW_{op}=1 to override)".format(op=operation.upper().replace("-", "_"))
    )
