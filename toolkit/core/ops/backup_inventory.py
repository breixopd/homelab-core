"""Bounded, secret-free projection of Kopia snapshot state."""

from __future__ import annotations

import json
import logging
import re
import shlex
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal, cast

from toolkit.core.config.config import Config
from toolkit.core.config.roles import deployed_roles
from toolkit.core.manifest.schema import NodeId

BackupNodeStatus = Literal["fresh", "stale", "missing", "error"]
_MAX_BODY_BYTES = 2 * 1024 * 1024
_MAX_SNAPSHOTS = 1_000
_MAX_AGE_HOURS = 26.0
_SNAPSHOT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_OBJECT_ID = re.compile(r"^k[0-9a-f]{32,127}$")
logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class BackupNodeState:
    role: NodeId
    status: BackupNodeStatus
    ok: bool
    snapshot_id: str = ""
    root_object_id: str = ""
    snapshot_count: int = 0
    last_snapshot_at: datetime | None = None
    age_hours: float | None = None
    size_bytes: int = 0


@dataclass(frozen=True, slots=True)
class BackupInventory:
    nodes: tuple[BackupNodeState, ...]
    error: str = ""

    @property
    def ok(self) -> bool:
        return not self.error and all(node.ok for node in self.nodes)


def _roles(cfg: Config) -> tuple[NodeId, ...]:
    return deployed_roles(cfg)


def _error_inventory(cfg: Config, message: str) -> BackupInventory:
    return BackupInventory(
        tuple(BackupNodeState(role, "error", False) for role in _roles(cfg)),
        message[:180],
    )


def _timestamp(value: object) -> datetime | None:
    if not isinstance(value, str) or len(value) > 64:
        return None
    try:
        parsed = datetime.fromisoformat(value.removesuffix("Z") + ("+00:00" if value.endswith("Z") else ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def parse_snapshot_inventory(
    body: str,
    cfg: Config,
    *,
    now: datetime | None = None,
) -> BackupInventory:
    """Parse Kopia JSON without exposing paths, usernames, or repository details."""
    if len(body.encode("utf-8", errors="replace")) > _MAX_BODY_BYTES:
        return _error_inventory(cfg, "snapshot inventory exceeds size limit")
    try:
        raw = json.loads(body)
    except json.JSONDecodeError:
        return _error_inventory(cfg, "snapshot inventory is invalid")
    if not isinstance(raw, list) or len(raw) > _MAX_SNAPSHOTS:
        return _error_inventory(cfg, "snapshot inventory has an invalid shape")
    current = now or datetime.now(UTC)
    by_role: dict[NodeId, list[tuple[datetime, str, str, int, bool]]] = {role: [] for role in _roles(cfg)}
    for item in raw:
        if not isinstance(item, dict):
            continue
        source = item.get("source")
        if not isinstance(source, dict):
            continue
        host = source.get("host")
        if not isinstance(host, str):
            continue
        role = cast(NodeId, host.removeprefix("homelab-"))
        if role not in by_role:
            continue
        started = _timestamp(item.get("startTime"))
        if started is None or started > current:
            continue
        snapshot_id = item.get("id")
        safe_id = snapshot_id if isinstance(snapshot_id, str) and _SNAPSHOT_ID.fullmatch(snapshot_id) else ""
        root_entry = item.get("rootEntry")
        root_object = root_entry.get("obj") if isinstance(root_entry, dict) else ""
        safe_root_object = root_object if isinstance(root_object, str) and _OBJECT_ID.fullmatch(root_object) else ""
        stats = item.get("stats")
        size = stats.get("totalSize", 0) if isinstance(stats, dict) else 0
        size_bytes = size if isinstance(size, int) and 0 <= size <= 2**63 - 1 else 0
        error_count = stats.get("errorCount", 0) if isinstance(stats, dict) else 0
        summary = root_entry.get("summ") if isinstance(root_entry, dict) else None
        failed_count = summary.get("numFailed", 0) if isinstance(summary, dict) else 0
        complete = (
            isinstance(error_count, int) and error_count == 0 and isinstance(failed_count, int) and failed_count == 0
        )
        by_role[role].append((started, safe_id, safe_root_object, size_bytes, complete))
    nodes: list[BackupNodeState] = []
    for role, snapshots in by_role.items():
        if not snapshots:
            nodes.append(BackupNodeState(role, "missing", False))
            continue
        newest = max(snapshots, key=lambda item: item[0])
        age = (current - newest[0]).total_seconds() / 3600
        fresh = age < _MAX_AGE_HOURS
        complete = newest[4]
        nodes.append(
            BackupNodeState(
                role=role,
                status="fresh" if fresh and complete else ("error" if not complete else "stale"),
                ok=fresh and complete,
                snapshot_id=newest[1],
                root_object_id=newest[2],
                snapshot_count=len(snapshots),
                last_snapshot_at=newest[0],
                age_hours=round(age, 2),
                size_bytes=newest[3],
            )
        )
    return BackupInventory(tuple(nodes))


def read_backup_inventory(cfg: Config, root: Path) -> BackupInventory:
    """Read the central repository inventory through the infra deployment transport."""
    resolved_root = Path(root).resolve()
    command = ["kopia", "snapshot", "list", "--all", "--max-results=50", "--json"]
    if cfg.proxmox.provision_machines:
        from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
        from toolkit.core.manifest.placement import service_address

        shell_command = "docker exec -e KOPIA_CONFIG_PATH=/app/config/repository.config kopia " + " ".join(
            shlex.quote(part) for part in command
        )
        rc, output, error = ssh_run_on_vm(
            cfg,
            service_address(cfg, "kopia"),
            shell_command,
            root=resolved_root,
            timeout=45,
        )
    else:
        from toolkit.core.ops.automation import docker_exec

        rc, output = docker_exec("kopia", command, timeout=45)
        error = ""
    if rc != 0 or not output:
        logger.warning(
            "Kopia snapshot inventory unavailable (exit=%d, output=%s)",
            rc,
            bool(error or output),
        )
        return _error_inventory(cfg, "snapshot inventory is unavailable on the backup node")
    return parse_snapshot_inventory(output, cfg)
