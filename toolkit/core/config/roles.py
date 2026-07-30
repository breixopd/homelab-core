"""Runtime role selection derived from desired state and deployment mode."""

from __future__ import annotations

from toolkit.core.config.config import Config
from toolkit.core.manifest.schema import NodeId


def uses_remote_nodes(cfg: Config) -> bool:
    """Return whether runtime operations must cross the provisioned-node transport."""
    return cfg.proxmox.provision_machines


def deployed_roles(cfg: Config) -> tuple[NodeId, ...]:
    """Return runtime nodes, collapsing local deployments to the control node."""
    if not uses_remote_nodes(cfg):
        return (cfg.control_node,)
    return tuple(cfg.enabled_nodes)
