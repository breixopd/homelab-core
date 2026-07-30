from __future__ import annotations

# Re-export every router that app.py registers, so callers + tests can do
# `from toolkit.webui.routers import graph, services, ...` uniformly.
from toolkit.webui.routers import (
    account,
    auth,
    dashboard,
    deploy,
    dns,
    graph,
    invite,
    machines,
    operations,
    projects,
    secrets,
    services,
    settings,
    setup,
    webhooks,
)

__all__ = [
    "account",
    "auth",
    "dashboard",
    "deploy",
    "dns",
    "graph",
    "invite",
    "machines",
    "operations",
    "projects",
    "secrets",
    "services",
    "settings",
    "setup",
    "webhooks",
]
