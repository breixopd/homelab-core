"""Node-local Kopia snapshot execution shared by timers, CLI, and controller jobs."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from toolkit.core.config.storage import env_path
from toolkit.core.manifest.schema import NodeId
from toolkit.core.manifest.storage import read_role_environment
from toolkit.core.ops.automation import docker_exec
from toolkit.core.state.audit_log import AuditAction, audit


@dataclass(frozen=True, slots=True)
class BackupResult:
    ok: bool
    role: NodeId
    message: str
    actions: tuple[str, ...] = ()


def _connect_agent(root: Path, role: NodeId, actions: list[str]) -> tuple[bool, str]:
    environment = read_role_environment(env_path(role, root))
    server = environment.get("KOPIA_SERVER_HOST", "").strip()
    fingerprint = environment.get("KOPIA_SERVER_CERT_FINGERPRINT", "").strip()
    if not server or not fingerprint:
        return False, "generated backup server endpoint or certificate fingerprint is missing"
    rc, output = docker_exec("kopia-agent", ["kopia", "repository", "status"], timeout=30)
    if rc == 0:
        actions.append("Repository connection verified")
        return True, ""
    rc, output = docker_exec(
        "kopia-agent",
        [
            "kopia",
            "repository",
            "connect",
            "server",
            f"--url=https://{server}:51515",
            f"--server-cert-fingerprint={fingerprint}",
            "--override-username=homelab",
            f"--override-hostname=homelab-{role}",
        ],
        timeout=120,
    )
    if rc != 0:
        return False, f"repository connection failed: {(output or 'unknown error')[:180]}"
    actions.append("Repository agent enrolled")
    return True, ""


def run_node_snapshot(root: Path, role: NodeId, *, actor: str = "systemd") -> BackupResult:
    """Create one encrypted snapshot from the current node and return bounded evidence."""
    started = time.monotonic()
    actions: list[str] = []
    desired_state = root / "config.yaml"
    cfg = None
    if desired_state.is_file():
        from toolkit.core.config.config import load_config
        from toolkit.core.ops.logical_backups import prepare_logical_dumps

        cfg = load_config(desired_state)
        dumps = prepare_logical_dumps(cfg, root, role)
        if not dumps.ok:
            result = BackupResult(False, role, f"logical database export failed: {dumps.errors[0]}")
            audit(
                root,
                AuditAction.BACKUP,
                actor=actor,
                ok=False,
                detail=result.message,
                vm=role,
                duration_s=time.monotonic() - started,
            )
            return result
        if dumps.artifacts:
            actions.append(f"Refreshed {len(dumps.artifacts)} consistent database export(s)")
    from toolkit.core.manifest.placement import service_node

    owns_server = cfg is not None and role == service_node(cfg, "kopia")
    container = "kopia" if owns_server else "kopia-agent"
    if owns_server:
        rc, output = docker_exec(container, ["kopia", "repository", "status"], timeout=30)
        connected, error = rc == 0, f"repository is unavailable: {(output or 'unknown error')[:180]}"
    else:
        connected, error = _connect_agent(root, role, actions)
    if connected:
        policy_rc, policy_output = docker_exec(
            container,
            ["kopia", "policy", "set", "/source", "--manual", "--compression=zstd"],
            timeout=60,
        )
        if policy_rc != 0:
            connected = False
            error = f"snapshot policy failed: {(policy_output or 'unknown error')[:180]}"
        else:
            actions.append("Snapshot policy reconciled")
    if connected:
        timestamp = datetime.now(UTC).isoformat(timespec="seconds")
        rc, output = docker_exec(
            container,
            [
                "kopia",
                "snapshot",
                "create",
                "/source",
                f"--description=Automated {role} snapshot {timestamp}",
                f"--tags=node:{role}",
                "--tags=trigger:scheduled",
                "--json",
            ],
            timeout=3_600,
        )
        if rc == 0:
            actions.append("Encrypted snapshot completed")
            result = BackupResult(True, role, f"{role} snapshot completed", tuple(actions))
        else:
            result = BackupResult(False, role, f"snapshot failed: {(output or 'unknown error')[:180]}", tuple(actions))
    else:
        result = BackupResult(False, role, error, tuple(actions))
    audit(
        root,
        AuditAction.BACKUP,
        actor=actor,
        ok=result.ok,
        detail=result.message,
        vm=role,
        duration_s=time.monotonic() - started,
        extra={"actions": list(result.actions)},
    )
    return result
