"""FastAPI + htmx web UI for the homelab toolkit."""

from __future__ import annotations

from pathlib import Path

from toolkit.core.config.storage import INSTALL_ROOT, resolve_homelab_root

__all__ = ["create_app", "current_root", "init_webui"]

_homelab_root: Path | None = None


def current_root() -> Path:
    """Return the configured homelab root for the web UI."""
    if _homelab_root is not None:
        return _homelab_root
    return INSTALL_ROOT


def init_webui(root: Path | None = None) -> Path:
    """Store homelab root for the lifetime of the web UI process."""
    global _homelab_root
    resolved = resolve_homelab_root(root)
    _homelab_root = resolved
    return resolved


def create_app(root: Path | None = None):
    """Build the FastAPI application (lazy import keeps CLI startup fast)."""
    from toolkit.webui.app import create_app as _create_app

    return _create_app(root=root)
