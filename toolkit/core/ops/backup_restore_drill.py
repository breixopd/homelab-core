"""Bounded Kopia content restores for unattended backup verification."""

from __future__ import annotations

import json
import os
import re
import shlex
import time
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from toolkit.core.config.config import Config
from toolkit.core.config.roles import uses_remote_nodes
from toolkit.core.deploy.operation_lease import LeaseBusyError, OperationLease
from toolkit.core.manifest.schema import NodeId
from toolkit.core.ops.logical_backups import logical_dump_names
from toolkit.core.state.audit_log import AuditAction, audit
from toolkit.core.state.files import atomic_write_json

_ROOT_OBJECT = re.compile(r"^k[0-9a-f]{32,127}$")
_COUNT = re.compile(r"^ARTIFACT_COUNT=(\d+)$", re.MULTILINE)
_MAX_EVIDENCE_BYTES = 64 * 1024


@dataclass(frozen=True, slots=True)
class BackupDrillNodeResult:
    role: NodeId
    ok: bool
    artifact_count: int = 0
    error: str = ""


@dataclass(frozen=True, slots=True)
class BackupRestoreDrillResult:
    ok: bool
    nodes: tuple[BackupDrillNodeResult, ...]
    errors: tuple[str, ...] = ()
    deferred: bool = False


@dataclass(frozen=True, slots=True)
class BackupDrillEvidence:
    checked_at: datetime
    ok: bool
    node_count: int
    artifact_count: int
    error_count: int


def read_backup_drill_evidence(root: Path) -> BackupDrillEvidence | None:
    """Read validated restore-drill evidence without following links."""
    path = root.resolve() / ".homelab-state" / "backup-drills" / "latest.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            content = os.read(descriptor, _MAX_EVIDENCE_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(content) > _MAX_EVIDENCE_BYTES:
            return None
        raw = json.loads(content)
        if not isinstance(raw, dict) or not isinstance(raw.get("ok"), bool):
            return None
        from toolkit.core.config.config import load_config

        enabled_nodes = set(load_config(root.resolve() / "config.yaml").enabled_nodes)
        checked_at = datetime.fromisoformat(raw["checked_at"])
        if checked_at.tzinfo is None:
            return None
        nodes = raw.get("nodes")
        errors = raw.get("errors")
        if (
            not isinstance(nodes, list)
            or len(nodes) > len(enabled_nodes)
            or not isinstance(errors, list)
            or len(errors) > 100
        ):
            return None
        if any(not isinstance(error, str) or len(error) > 500 for error in errors):
            return None
        artifact_count = 0
        roles: set[str] = set()
        for node in nodes:
            if not isinstance(node, dict) or node.get("role") not in enabled_nodes:
                return None
            role = node["role"]
            if role in roles:
                return None
            roles.add(role)
            count = node.get("artifact_count")
            if (
                not isinstance(node.get("ok"), bool)
                or not isinstance(count, int)
                or isinstance(count, bool)
                or count < 0
                or count > 100
            ):
                return None
            artifact_count += count
        if artifact_count > 100:
            return None
        return BackupDrillEvidence(
            checked_at=checked_at.astimezone(UTC),
            ok=raw["ok"],
            node_count=len(nodes),
            artifact_count=artifact_count,
            error_count=len(errors),
        )
    except (OSError, UnicodeError, ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _restore_script(root_object: str, role: NodeId, artifacts: tuple[str, ...]) -> str:
    lines = [
        "set -eu",
        f'base="$(mktemp -d /tmp/homelab-backup-drill-{role}.XXXXXX)"',
        "trap 'rm -rf \"$base\"' EXIT",
        f'kopia restore {shlex.quote(f"{root_object}/config.yaml")} "$base/config.yaml" '
        "--write-files-atomically --no-progress",
        'test -s "$base/config.yaml"',
    ]
    if artifacts:
        lines.extend(
            [
                'mkdir -p "$base/dumps"',
                f'kopia restore {shlex.quote(f"{root_object}/backup-dumps/{role}")} "$base/dumps" '
                "--write-files-atomically --no-progress",
            ]
        )
        for artifact in artifacts:
            target = f"$base/dumps/{artifact}"
            lines.append(f'test -s "{target}"')
            lines.append(f'gzip -t "{target}"')
    lines.append(f"printf 'ARTIFACT_COUNT=%s\\n' {len(artifacts)}")
    return "\n".join(lines)


def _execute(cfg: Config, root: Path, script: str) -> tuple[int, str]:
    if uses_remote_nodes(cfg):
        from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
        from toolkit.core.manifest.placement import service_address

        command = "docker exec -e KOPIA_CONFIG_PATH=/app/config/repository.config kopia sh -ec " + shlex.quote(script)
        code, output, _error = ssh_run_on_vm(
            cfg,
            service_address(cfg, "kopia"),
            command,
            root=root,
            timeout=900,
        )
        return code, output

    from toolkit.core.ops.automation import docker_exec

    return docker_exec("kopia", ["sh", "-ec", script], timeout=900)


def _write_evidence(root: Path, result: BackupRestoreDrillResult) -> None:
    atomic_write_json(
        root / ".homelab-state" / "backup-drills" / "latest.json",
        {
            "checked_at": datetime.now(UTC).isoformat(),
            "ok": result.ok,
            "nodes": [asdict(node) for node in result.nodes],
            "errors": list(result.errors),
        },
    )


def run_backup_restore_drill(
    cfg: Config,
    root: Path,
    *,
    actor: str = "system",
) -> BackupRestoreDrillResult:
    """Restore bounded evidence from every fresh node snapshot and verify it."""
    resolved_root = root.resolve()
    started = time.monotonic()
    if not cfg.backups.enabled:
        return BackupRestoreDrillResult(False, (), ("backups are disabled",))
    from toolkit.core.ops.backup_inventory import read_backup_inventory

    nodes: list[BackupDrillNodeResult] = []
    errors: list[str] = []
    lease: OperationLease | None = None
    try:
        lease = OperationLease.acquire(resolved_root, "backup-restore-drill")
        inventory = read_backup_inventory(cfg, resolved_root)
        if inventory.error:
            errors.append(inventory.error)
        else:
            for snapshot in inventory.nodes:
                if not snapshot.ok:
                    error = f"{snapshot.role} snapshot is {snapshot.status}"
                    nodes.append(BackupDrillNodeResult(snapshot.role, False, error=error))
                    errors.append(error)
                    continue
                if not _ROOT_OBJECT.fullmatch(snapshot.root_object_id):
                    error = f"{snapshot.role} snapshot has no restorable root object"
                    nodes.append(BackupDrillNodeResult(snapshot.role, False, error=error))
                    errors.append(error)
                    continue
                expected = logical_dump_names(cfg, snapshot.role, resolved_root)
                code, output = _execute(
                    cfg,
                    resolved_root,
                    _restore_script(snapshot.root_object_id, snapshot.role, expected),
                )
                match = _COUNT.search(output or "")
                count = int(match.group(1)) if match else 0
                ok = code == 0 and match is not None and count == len(expected)
                error = "" if ok else f"{snapshot.role} bounded restore verification failed"
                nodes.append(BackupDrillNodeResult(snapshot.role, ok, count, error))
                if error:
                    errors.append(error)
    except LeaseBusyError:
        return BackupRestoreDrillResult(
            False,
            (),
            ("another mutating operation is running",),
            deferred=True,
        )
    finally:
        if lease is not None:
            lease.release()

    result = BackupRestoreDrillResult(not errors, tuple(nodes), tuple(errors))
    _write_evidence(resolved_root, result)
    audit(
        resolved_root,
        AuditAction.RESTORE,
        actor=actor,
        ok=result.ok,
        detail="bounded Kopia restore drill completed" if result.ok else "bounded Kopia restore drill failed",
        duration_s=time.monotonic() - started,
        extra={"node_count": len(result.nodes), "error_count": len(result.errors)},
    )
    if not result.ok:
        from toolkit.core.ops.notifications import send_ntfy

        send_ntfy(
            "Backup restore drill needs attention:\n" + "\n".join(f"- {error}" for error in result.errors[:6]),
            "Homelab backup restore drill failed",
            "high",
            resolved_root,
            tags="warning",
        )
    return result
