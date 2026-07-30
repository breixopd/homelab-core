"""kopia service plugin.

Owns its verify() on top of the base ServicePlugin defaults
(compose_service, env_vars, secrets_needed, credentials) read from
service.yaml.

verify() probes the Kopia repository: docker exec against the kopia
container on multi-VM hosts (the Kopia API server requires CSRF tokens, so
docker exec is used there), and the local HTTP API on single-host deploys.
"""

from __future__ import annotations

import json
import time as _time
from collections.abc import Sequence
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config, ExternalHost
    from toolkit.core.generate.artifacts import ArtifactGenerationContext
    from toolkit.services.sdk import VerifyCheck

_SNAPSHOT_MAX_AGE_HOURS = 26.0
_KOPIA_ENV = "docker exec -e KOPIA_CONFIG_PATH=/app/config/repository.config kopia"


class KopiaPlugin(ServicePlugin):
    service = "kopia"
    category = "management"

    def generate_artifacts(self, context: ArtifactGenerationContext) -> None:
        from toolkit.core.manifest.placement import service_address
        from toolkit.core.ops.backup_tls import ensure_kopia_server_certificate

        certificate = ensure_kopia_server_certificate(
            context.root,
            service_address(context.config, self.service),
        )
        context.claim(str(certificate.cert_path.relative_to(context.root)))
        context.claim(str(certificate.key_path.relative_to(context.root)))

    def configure_host_integrations(
        self,
        cfg: Config,
        *,
        previous: ExternalHost | None,
        current: ExternalHost | None,
    ) -> Config:
        updated = cfg.model_copy(deep=True)
        if current is not None and "backup-storage" in current.services:
            updated.backups = updated.backups.model_copy(
                update={"enabled": True, "target": "remote", "storage_host": current.name}
            )
        elif previous is not None and updated.backups.storage_host == previous.name:
            updated.backups = updated.backups.model_copy(update={"target": "local", "storage_host": ""})
        return updated

    def reconcile_host_integration(
        self,
        integration: str,
        cfg: Config,
        host: ExternalHost,
        root: Path,
        *,
        selected: bool,
    ) -> list[str]:
        if integration != "backup-storage":
            raise ValueError(f"unsupported Kopia host integration: {integration}")
        if not selected:
            return []
        from toolkit.core.ops.backup_ssh import ensure_backup_ssh_identity, write_remote_known_hosts

        ensure_backup_ssh_identity(root)
        write_remote_known_hosts(root, host.ip, host.ssh_port)
        return [f"Backups: pinned SFTP repository target {host.name}"]

    def cleanup_host_integration(self, integration: str, cfg: Config, host: ExternalHost, root: Path) -> list[str]:
        if integration != "backup-storage":
            raise ValueError(f"unsupported Kopia host integration: {integration}")
        import subprocess

        from toolkit.core.infra.hosts import host_ssh_args

        args = host_ssh_args(root, host)
        if args is None:
            raise RuntimeError("controller SSH identity is unavailable")
        args.append("sed -i '/ homelab-kopia-backup$/d' ~/.ssh/authorized_keys")
        result = subprocess.run(args, capture_output=True, text=True, timeout=30, check=False)
        if result.returncode != 0:
            raise RuntimeError((result.stderr or result.stdout or "remote cleanup failed")[:120])
        return [f"Backups: removed restricted repository identity from {host.name}"]

    def host_integration_ansible_variables(
        self,
        integration: str,
        cfg: Config,
        host: ExternalHost,
        root: Path,
    ) -> dict[str, str]:
        if integration != "backup-storage":
            return {}
        from toolkit.core.ops.backup_ssh import ensure_backup_ssh_identity

        return {"kopia_backup_public_key": ensure_backup_ssh_identity(root).public_key}

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Connect/create the Kopia filesystem repository + apply retention policies."""
        import importlib

        bootstrap = importlib.import_module("toolkit.services.kopia.bootstrap")
        logs = bootstrap.bootstrap_kopia_repository(cfg, secrets)
        for line in logs:
            lower = line.lower()
            if (
                "bootstrap error" in lower
                or "repository create failed" in lower
                or "repository connect failed" in lower
            ):
                raise RuntimeError(f"Kopia repository bootstrap failed: {line}")
        return logs

    def _snapshot_age_hours(self, snap: dict, now: float) -> float | None:
        start_time = snap.get("startTime")
        if start_time is None:
            return None
        if isinstance(start_time, int | float):
            return (now - float(start_time)) / 3600.0
        if isinstance(start_time, str):
            try:
                if start_time.endswith("Z"):
                    start_time = start_time[:-1] + "+00:00"
                ts = datetime.fromisoformat(start_time).timestamp()
                return (now - ts) / 3600.0
            except ValueError:
                return None
        return None

    def _check_snapshots_from_json(self, body: str, now: float) -> tuple[bool, str]:
        try:
            snapshots = json.loads(body)
        except json.JSONDecodeError:
            return False, "invalid snapshot JSON"
        entries = snapshots if isinstance(snapshots, list) else [snapshots] if snapshots else []
        if not entries:
            return False, "no snapshots"
        newest_age: float | None = None
        for snap in entries:
            age = self._snapshot_age_hours(snap, now)
            if age is not None and (newest_age is None or age < newest_age):
                newest_age = age
        if newest_age is None:
            return False, f"{len(entries)} snapshot(s) but no timestamps"
        ok = newest_age < _SNAPSHOT_MAX_AGE_HOURS
        if newest_age < 24:
            detail = f"{len(entries)} snapshot(s), last {newest_age:.1f}h ago"
        else:
            detail = f"{len(entries)} snapshot(s), last {newest_age / 24:.1f}d ago"
        return ok, detail

    def _check_role_snapshots_from_json(
        self, body: str, now: float, roles: Sequence[str]
    ) -> list[tuple[str, bool, str]]:
        try:
            snapshots = json.loads(body)
        except json.JSONDecodeError:
            return [(role, False, "invalid snapshot JSON") for role in roles]
        entries = snapshots if isinstance(snapshots, list) else [snapshots] if snapshots else []
        newest: dict[str, float] = {}
        counts: dict[str, int] = {}
        for snapshot in entries:
            if not isinstance(snapshot, dict):
                continue
            source = snapshot.get("source")
            host = source.get("host", "") if isinstance(source, dict) else ""
            role = host.removeprefix("homelab-")
            if role not in roles:
                continue
            counts[role] = counts.get(role, 0) + 1
            age = self._snapshot_age_hours(snapshot, now)
            if age is not None and age < newest.get(role, float("inf")):
                newest[role] = age
        results: list[tuple[str, bool, str]] = []
        for role in roles:
            if role not in counts:
                results.append((role, False, "snapshot missing"))
                continue
            age = newest.get(role)
            if age is None:
                results.append((role, False, f"{counts[role]} snapshot(s) without timestamps"))
                continue
            detail = f"{counts[role]} snapshot(s), newest {age:.1f}h ago"
            results.append((role, age < _SNAPSHOT_MAX_AGE_HOURS, detail))
        return results

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Verify the Kopia repository is connected and has recent snapshots."""
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm

        try:
            if not cfg.backups.enabled:
                return [VerifyCheck("kopia", "repository", True, "backups disabled")]
            if cfg.domain == "localhost":
                return [VerifyCheck("kopia", "repository", True, "skipped (localhost)")]
            if not container_exists_on_vm(cfg, vm_ip, "kopia", root):
                return [VerifyCheck("kopia", "repository", False, "container missing")]

            now = _time.time()
            if cfg.is_multi_node:
                from toolkit.services.sdk import ssh_on_vm

                status_cmd = f"{_KOPIA_ENV} kopia repository status 2>&1"
                rc, out, _ = ssh_on_vm(cfg, vm_ip, status_cmd, root=root, timeout=20)
                if rc != 0 or not out or "not connected" in out.lower():
                    return [VerifyCheck("kopia", "repository", False, "not connected")]
                expected_backend = "sftp" if cfg.backups.target == "remote" else "filesystem"
                if expected_backend not in out.lower():
                    return [
                        VerifyCheck(
                            "kopia", "repository", False, f"configured {expected_backend.upper()} backend is not active"
                        )
                    ]
                checks = [
                    VerifyCheck(
                        "kopia",
                        "repository",
                        True,
                        f"connected ({len(out.strip().splitlines())} status lines)",
                    )
                ]
                snap_cmd = f"{_KOPIA_ENV} kopia snapshot list --all --json 2>&1"
                rc2, snap_out, _ = ssh_on_vm(cfg, vm_ip, snap_cmd, root=root, timeout=30)
                if rc2 != 0 or not snap_out:
                    for role in cfg.enabled_nodes:
                        checks.append(VerifyCheck("kopia", f"snapshot-{role}", False, "snapshot list failed"))
                    return checks
                for role, ok, detail in self._check_role_snapshots_from_json(snap_out, now, cfg.enabled_nodes):
                    checks.append(VerifyCheck("kopia", f"snapshot-{role}", ok, detail))
                return checks

            from toolkit.core.ops.automation import docker_exec

            status_rc, status_out = docker_exec("kopia", ["kopia", "repository", "status"], timeout=20)
            if status_rc != 0 or "not connected" in (status_out or "").lower():
                return [VerifyCheck("kopia", "repository", False, "not connected")]
            expected_backend = "sftp" if cfg.backups.target == "remote" else "filesystem"
            if expected_backend not in (status_out or "").lower():
                return [
                    VerifyCheck(
                        "kopia", "repository", False, f"configured {expected_backend.upper()} backend is not active"
                    )
                ]
            checks = [VerifyCheck("kopia", "repository", True, "connected")]
            snapshot_rc, snapshot_out = docker_exec(
                "kopia", ["kopia", "snapshot", "list", "--all", "--json"], timeout=30
            )
            if snapshot_rc != 0 or not snapshot_out:
                checks.append(VerifyCheck("kopia", f"snapshot-{cfg.control_node}", False, "snapshot list failed"))
                return checks
            for role, ok, detail in self._check_role_snapshots_from_json(snapshot_out, now, (cfg.control_node,)):
                checks.append(VerifyCheck("kopia", f"snapshot-{role}", ok, detail))
            return checks
        except Exception as exc:
            return [VerifyCheck("kopia", "repository", False, str(exc)[:80])]
