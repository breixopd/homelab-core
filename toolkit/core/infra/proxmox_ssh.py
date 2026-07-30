"""Canonical Proxmox control-host SSH connection construction."""

from __future__ import annotations

import os
from pathlib import Path

from toolkit.core.config.config import Config


def resolve_proxmox_control_host(cfg: Config) -> str:
    """Resolve the declared control endpoint without guessing a guest address."""
    if cfg.proxmox.resolved_control_host:
        return cfg.proxmox.resolved_control_host
    if cfg.host_capacity.proxmox_host:
        return cfg.host_capacity.proxmox_host.strip()
    return cfg.dns.public_ip.strip()


def configured_proxmox_ssh_key(cfg: Config, root: Path | None = None) -> Path | None:
    """Resolve the configured control key path, whether or not it exists yet."""
    raw = cfg.proxmox.ssh.key_file.strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if not path.is_absolute() and root is not None:
        path = root.resolve() / path
    return path.resolve()


def resolve_proxmox_ssh_key(cfg: Config, root: Path | None = None) -> Path | None:
    """Return the configured control key only when it exists as a file."""
    path = configured_proxmox_ssh_key(cfg, root)
    return path if path is not None and path.is_file() else None


def resolve_proxmox_proxy_key(cfg: Config, root: Path | None = None) -> Path | None:
    """Resolve the key used to reach Proxmox as a guest jump host.

    A workstation must use its explicitly configured Proxmox key.  The
    control-node container intentionally does not receive that private key;
    host setup authorizes the controller-owned identity instead, so only that
    node may fall back to ``ssh/homelab_admin_ed25519``.  Returning ``None``
    when neither identity exists keeps callers fail-closed.
    """
    configured = resolve_proxmox_ssh_key(cfg, root)
    if configured is not None:
        return configured

    if root is None or os.environ.get("HOMELAB_NODE", "").strip() != cfg.control_node:
        return None
    automation_key = root.resolve() / "ssh" / "homelab_admin_ed25519"
    return automation_key.resolve() if automation_key.is_file() else None


def build_proxmox_ssh_command(
    cfg: Config,
    root: Path | None,
    remote_command: str,
    *,
    host: str | None = None,
    connect_timeout: int | None = None,
) -> list[str]:
    """Build a key-only, host-key-verifying SSH command for Proxmox."""
    access = cfg.proxmox.ssh
    target = host or resolve_proxmox_control_host(cfg)
    if not target:
        raise ValueError("Proxmox control host is not configured")
    key = resolve_proxmox_proxy_key(cfg, root)
    if key is None:
        raise ValueError("Proxmox control SSH key is unavailable")
    timeout = connect_timeout if connect_timeout is not None else access.connect_timeout
    known_hosts = (
        root.resolve() / "automation" / "ansible" / "inventory" / "known_hosts"
        if root is not None
        else Path.home() / ".ssh" / "known_hosts"
    )
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        f"UserKnownHostsFile={known_hosts}",
        "-o",
        f"ConnectTimeout={timeout}",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        "ServerAliveCountMax=4",
        "-p",
        str(access.port),
        "-i",
        str(key),
        f"{access.user}@{target}",
        remote_command,
    ]
