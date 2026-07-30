"""Role-aware orchestration for manual cluster maintenance runs."""

from __future__ import annotations

import shlex
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from toolkit.core.config.config import Config
from toolkit.core.config.roles import deployed_roles
from toolkit.core.manifest.schema import NodeId
from toolkit.core.state.files import atomic_write_json

ProgressCallback = Callable[[str, dict[str, Any]], None]
CancelCallback = Callable[[], None]
_REMOTE_ROOT = "/opt/homelab"


@dataclass(frozen=True, slots=True)
class NodeMaintenanceState:
    role: NodeId
    maintenance_ok: bool
    snapshot_ok: bool | None


@dataclass(slots=True)
class ClusterMaintenanceResult:
    timestamp: float = field(default_factory=time.time)
    nodes: list[NodeMaintenanceState] = field(default_factory=list)
    actions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "ok": self.ok,
            "actions": self.actions,
            "errors": self.errors,
            "nodes": [asdict(node) for node in self.nodes],
        }


def _emit(callback: ProgressCallback | None, message: str, **payload: Any) -> None:
    if callback is not None:
        callback(message, payload)


def _remote_cli(command: list[str]) -> str:
    return shlex.join(
        [
            f"{_REMOTE_ROOT}/.venv/bin/python3",
            "-m",
            "toolkit.cli",
            "--root",
            _REMOTE_ROOT,
            "maintenance",
            *command,
        ]
    )


def _write_state(root: Path, result: ClusterMaintenanceResult) -> None:
    path = root / "data" / "maintenance" / "last-run.json"
    atomic_write_json(path, result.to_dict())


def _notify_failure(root: Path, result: ClusterMaintenanceResult) -> None:
    from toolkit.core.ops.notifications import send_ntfy

    send_ntfy(
        "Cluster maintenance needs attention:\n" + "\n".join(f"- {error}" for error in result.errors[:6]),
        "Homelab maintenance failed",
        "high",
        root,
        tags="warning",
    )


def run_cluster_maintenance(
    cfg: Config,
    root: Path,
    *,
    actor: str = "controller",
    on_log: ProgressCallback | None = None,
    check_cancelled: CancelCallback | None = None,
) -> ClusterMaintenanceResult:
    """Run maintenance and optional snapshots across every physical runtime node."""
    resolved_root = root.resolve()
    started = time.monotonic()
    result = ClusterMaintenanceResult()
    cancel = check_cancelled or (lambda: None)
    remote = cfg.proxmox.provision_machines

    if remote:
        from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

    for role in deployed_roles(cfg):
        cancel()
        _emit(on_log, f"Starting maintenance on {role}", role=role, stage="maintenance")
        if remote:
            maintenance_command = _remote_cli(["run", "--node", role, "--no-notify"])
            maintenance_rc, _output, _error = ssh_run_on_vm(
                cfg,
                cfg.node_ip(role),
                maintenance_command,
                root=resolved_root,
                timeout=1_800,
            )
            maintenance_ok = maintenance_rc == 0
        else:
            from toolkit.core.ops.maintenance import run_maintenance

            maintenance_ok = run_maintenance(
                resolved_root,
                vm=role,
                notify_on_attention=False,
                actor=actor,
            ).ok
        if maintenance_ok:
            result.actions.append(f"{role} maintenance completed")
        else:
            result.errors.append(f"{role} maintenance failed")
        _emit(
            on_log,
            f"Maintenance {'completed' if maintenance_ok else 'failed'} on {role}",
            role=role,
            stage="maintenance",
            ok=maintenance_ok,
        )

        snapshot_ok: bool | None = None
        if cfg.backups.enabled:
            cancel()
            _emit(on_log, f"Starting backup snapshot on {role}", role=role, stage="backup")
            if remote:
                snapshot_command = _remote_cli(["snapshot", "--node", role])
                snapshot_rc, _output, _error = ssh_run_on_vm(
                    cfg,
                    cfg.node_ip(role),
                    snapshot_command,
                    root=resolved_root,
                    timeout=3_900,
                )
                snapshot_ok = snapshot_rc == 0
            else:
                from toolkit.core.ops.backups import run_node_snapshot

                snapshot_ok = run_node_snapshot(resolved_root, role, actor=actor).ok
            if snapshot_ok:
                result.actions.append(f"{role} snapshot completed")
            else:
                result.errors.append(f"{role} snapshot failed")
            _emit(
                on_log,
                f"Backup snapshot {'completed' if snapshot_ok else 'failed'} on {role}",
                role=role,
                stage="backup",
                ok=snapshot_ok,
            )
        result.nodes.append(NodeMaintenanceState(role, maintenance_ok, snapshot_ok))

    if cfg.backups.enabled and all(node.snapshot_ok for node in result.nodes):
        from toolkit.core.ops.backup_inventory import read_backup_inventory

        inventory = read_backup_inventory(cfg, resolved_root)
        if not inventory.ok:
            unhealthy = {node.role: node.status for node in inventory.nodes if not node.ok}
            if unhealthy:
                result.nodes = [
                    NodeMaintenanceState(node.role, node.maintenance_ok, False) if node.role in unhealthy else node
                    for node in result.nodes
                ]
                result.errors.extend(f"{role} backup verification is {status}" for role, status in unhealthy.items())
            else:
                result.errors.append(inventory.error or "backup inventory verification failed")
        _emit(on_log, "Backup inventory verification completed", stage="backup", ok=inventory.ok)

    try:
        _write_state(resolved_root, result)
    except OSError:
        result.errors.append("aggregate maintenance state could not be persisted")
    if not result.ok:
        _notify_failure(resolved_root, result)
    from toolkit.core.state.audit_log import AuditAction, audit

    audit(
        resolved_root,
        AuditAction.MAINTENANCE,
        actor=actor,
        ok=result.ok,
        detail="cluster maintenance completed" if result.ok else "cluster maintenance completed with errors",
        duration_s=time.monotonic() - started,
        extra={
            "nodes": [node.role for node in result.nodes],
            "action_count": len(result.actions),
            "error_count": len(result.errors),
        },
    )
    return result
