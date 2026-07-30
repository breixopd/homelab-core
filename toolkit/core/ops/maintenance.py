"""Automated host maintenance: Docker cleanup, journal rotation, log trimming."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

_MAX_STDERR = 300
_DEFAULT_IMAGE_AGE_HOURS = 168  # 7 days
_DEFAULT_JOURNAL_SIZE = "400M"
_DEFAULT_JOURNAL_TIME = "14d"


@dataclass
class MaintenanceResult:
    """Outcome of a maintenance run."""

    timestamp: float = field(default_factory=time.time)
    actions: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    reboot_required: bool = False
    os_updates_healthy: bool | None = None

    @property
    def ok(self) -> bool:
        return not self.errors

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "ok": self.ok,
            "actions": self.actions,
            "errors": self.errors,
            "reboot_required": self.reboot_required,
            "os_updates_healthy": self.os_updates_healthy,
        }


@dataclass(frozen=True)
class OsPatchState:
    """Observed unattended-upgrade and reboot state for one runtime node."""

    reboot_required: bool
    updates_healthy: bool | None
    notices: list[str] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


def _run(cmd: list[str], *, timeout: int = 300) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
        return proc.returncode, (proc.stdout or "").strip(), (proc.stderr or "").strip()
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, "", str(exc)


def _disk_summary() -> str:
    rc, out, _ = _run(["df", "-h", "/"], timeout=15)
    if rc != 0:
        return "disk: unknown"
    line = out.splitlines()[-1] if out else ""
    return f"disk root: {line}" if line else "disk: n/a"


def check_os_patch_state(
    *,
    reboot_flag: Path = Path("/var/run/reboot-required"),
    systemd_runtime: Path = Path("/run/systemd/system"),
) -> OsPatchState:
    """Inspect guest OS patch health without initiating a disruptive reboot."""
    reboot_required = reboot_flag.is_file()
    notices = ["WARN: operating-system updates require a reboot"] if reboot_required else []
    if not systemd_runtime.is_dir():
        return OsPatchState(reboot_required, None, notices)

    rc, output, error = _run(
        [
            "systemctl",
            "show",
            "apt-daily-upgrade.service",
            "--property=LoadState",
            "--property=ActiveState",
            "--property=Result",
            "--no-pager",
        ],
        timeout=15,
    )
    if rc != 0:
        detail = (error or output or "unknown systemd error")[:_MAX_STDERR]
        return OsPatchState(
            reboot_required,
            False,
            notices,
            [f"CRITICAL: unattended operating-system upgrade status check failed: {detail}"],
        )

    properties = dict(line.split("=", 1) for line in output.splitlines() if "=" in line)
    if properties.get("LoadState") != "loaded":
        return OsPatchState(
            reboot_required,
            False,
            notices,
            ["CRITICAL: unattended operating-system upgrades are unavailable"],
        )
    result = properties.get("Result", "")
    if properties.get("ActiveState") == "failed" or result not in {"", "success"}:
        return OsPatchState(
            reboot_required,
            False,
            notices,
            [f"CRITICAL: unattended operating-system upgrades failed ({result or 'unknown'})"],
        )
    return OsPatchState(reboot_required, True, notices)


def _classify_maintenance_alerts(actions: list[str]) -> tuple[list[str], list[str]]:
    """Split action logs into run failures and non-fatal operator notices."""
    failures: list[str] = []
    notices: list[str] = []
    for line in actions:
        lower = line.lower()
        if line.startswith("CRITICAL:") or " failed:" in lower or lower.endswith(" failed"):
            failures.append(line)
        elif line.startswith("WARN:") or line.startswith("Image updates available:"):
            notices.append(line)
    return failures, notices


def _notify_maintenance_attention(root: Path, result: MaintenanceResult, *, vm: str | None, notices: list[str]) -> None:
    """Best-effort ntfy alert for maintenance failures, warnings, and update notices."""
    from toolkit.core.ops.notifications import send_email, send_ntfy

    scope = f" ({vm})" if vm else ""
    title = f"Homelab maintenance attention{scope}"
    lines = [f"Maintenance completed with {'errors' if result.errors else 'notices'}{scope}."]
    if result.errors:
        lines.append("")
        lines.append("Failures:")
        lines.extend(f"- {err}" for err in result.errors[:8])
    if notices:
        lines.append("")
        lines.append("Notices:")
        lines.extend(f"- {notice}" for notice in notices[:8])
    if len(result.errors) + len(notices) > 16:
        lines.append("")
        lines.append("Additional items were recorded in data/maintenance/last-run.json.")

    body = "\n".join(lines)
    send_ntfy(
        body,
        title,
        "high" if result.errors else "default",
        root,
        tags="warning" if result.errors else "bell",
    )
    send_email(title, body, root)


def run_docker_cleanup(
    *,
    image_max_age_hours: int = _DEFAULT_IMAGE_AGE_HOURS,
    prune_build_cache: bool = True,
    docker_bin: str = "docker",
) -> list[str]:
    """Remove stale images/containers/networks without touching named volumes."""
    logs: list[str] = []
    until = f"{image_max_age_hours}h"

    for label, args in (
        ("stopped containers", [docker_bin, "container", "prune", "-f"]),
        ("dangling images", [docker_bin, "image", "prune", "-f"]),
        (
            "old unused images",
            [docker_bin, "image", "prune", "-af", "--filter", f"until={until}"],
        ),
        ("unused networks", [docker_bin, "network", "prune", "-f"]),
    ):
        rc, out, err = _run(args, timeout=600)
        if rc == 0:
            summary = (out.splitlines()[-1] if out else "ok")[:120]
            logs.append(f"Docker {label}: {summary}")
        else:
            logs.append(f"Docker {label} failed: {(err or out)[:_MAX_STDERR]}")

    if prune_build_cache:
        rc, out, err = _run(
            [docker_bin, "builder", "prune", "-af", "--filter", f"until={until}"],
            timeout=600,
        )
        if rc == 0:
            summary = (out.splitlines()[-1] if out else "ok")[:120]
            logs.append(f"Docker build cache: {summary}")
        else:
            logs.append(f"Docker build cache failed: {(err or out)[:_MAX_STDERR]}")

    return logs


def root_disk_usage_percent(path: Path = Path("/")) -> float | None:
    try:
        usage = shutil.disk_usage(path)
        return (usage.used / usage.total) * 100
    except OSError:
        return None


def maybe_prune_docker_before_deploy(*, aggressive: bool = False, threshold_pct: float = 75.0) -> list[str]:
    """Prune unused Docker layers before compose pull when disk is tight or force-redeploy."""
    from toolkit.core.ops.controller_guard import skip_message, skip_on_workstation

    if skip_on_workstation("docker_prune"):
        return [skip_message("docker_prune")]

    pct = root_disk_usage_percent()
    if pct is None:
        return []
    if not aggressive and pct < threshold_pct:
        return [f"Docker prune skipped (root {pct:.0f}% used)"]
    hours = 24 if aggressive else 72
    logs = [f"Docker prune before deploy (root {pct:.0f}% used)"]
    logs.extend(run_docker_cleanup(image_max_age_hours=hours))
    return logs


def vacuum_journal(
    *,
    max_size: str = _DEFAULT_JOURNAL_SIZE,
    max_time: str = _DEFAULT_JOURNAL_TIME,
) -> list[str]:
    """Shrink systemd journal on the host."""
    logs: list[str] = []
    for args in (
        ["journalctl", "--vacuum-size", max_size],
        ["journalctl", "--vacuum-time", max_time],
    ):
        rc, out, err = _run(args, timeout=120)
        if rc == 0:
            freed = next((ln for ln in out.splitlines() if "vacuumed" in ln.lower() or "freed" in ln.lower()), out[:80])
            logs.append(f"Journal: {freed or 'ok'}")
        else:
            logs.append(f"Journal vacuum failed: {(err or out)[:_MAX_STDERR]}")
    return logs


def trim_homelab_logs(root: Path, *, max_age_days: int = 14) -> list[str]:
    """Delete rotated compose/deploy logs older than max_age_days under homelab root."""
    logs: list[str] = []
    root = root.resolve()
    cutoff = time.time() - max_age_days * 86400
    patterns = (".log", ".log.", ".status")
    removed = 0
    freed = 0
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        name = path.name
        if path.suffix not in (".log",) and not any(p in name for p in patterns):
            continue
        if path.stat().st_mtime >= cutoff:
            continue
        try:
            size = path.stat().st_size
            path.unlink()
            removed += 1
            freed += size
        except OSError:
            continue
    if removed:
        logs.append(f"Removed {removed} old log file(s) ({freed // 1024} KiB)")
    return logs


def run_maintenance(
    root: Path,
    *,
    vm: str | None = None,
    docker_bin: str = "docker",
    notify_on_attention: bool = True,
    actor: str | None = None,
) -> MaintenanceResult:
    """Full maintenance pass for a homelab guest or controller."""
    started = time.monotonic()
    result = MaintenanceResult()
    from toolkit.core.config.config import Config, load_config
    from toolkit.core.config.storage import config_path

    desired_state = config_path(root)
    try:
        runtime_config = load_config(desired_state) if desired_state.is_file() else Config()
    except (OSError, ValueError, TypeError) as exc:
        runtime_config = Config()
        result.errors.append(f"desired maintenance policy is unavailable: {str(exc)[:180]}")
    operation_node = vm or os.environ.get("HOMELAB_NODE") or runtime_config.control_node
    maintenance_config = runtime_config.maintenance
    result.actions.append(_disk_summary())
    patch_state = check_os_patch_state()
    result.reboot_required = patch_state.reboot_required
    result.os_updates_healthy = patch_state.updates_healthy
    result.actions.extend(patch_state.notices)
    result.errors.extend(patch_state.failures)
    result.actions.extend(run_docker_cleanup(docker_bin=docker_bin))
    result.actions.extend(vacuum_journal())
    result.actions.extend(trim_homelab_logs(root))
    try:
        from toolkit.core.ops.maintenance_tasks import (
            check_cert_expiries,
            scan_image_updates,
        )

        try:
            import subprocess as _sp

            domain = runtime_config.domain
            try:
                proc = _sp.run(
                    ["openssl", "s_client", "-connect", f"{domain}:443", "-servername", domain],
                    input="",
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if proc.returncode == 0 or proc.stdout:
                    end_date_proc = _sp.run(
                        ["openssl", "x509", "-noout", "-enddate"],
                        input=proc.stdout,
                        capture_output=True,
                        text=True,
                        timeout=5,
                    )
                    import datetime as _dt

                    if end_date_proc.returncode == 0:
                        line = end_date_proc.stdout.strip()
                        # notAfter=Jun 25 12:00:00 2026 GMT
                        if line.startswith("notAfter="):
                            date_str = line.split("=", 1)[1]
                            try:
                                end_dt = _dt.datetime.strptime(date_str, "%b %d %H:%M:%S %Y %Z")
                                days_left = (end_dt - _dt.datetime.utcnow()).days
                                result.actions.extend(
                                    check_cert_expiries(
                                        domain,
                                        days_left=days_left,
                                        warning_days=maintenance_config.cert_warning_days,
                                    )
                                )
                            except ValueError:
                                pass
            except (OSError, _sp.TimeoutExpired):
                pass
        except Exception as exc:
            result.errors.append(f"cert-expiry check: {exc}")
        try:
            proposals = (
                scan_image_updates(root=root, cfg=maintenance_config)
                if operation_node == runtime_config.control_node
                else []
            )
            if proposals:
                pnames = ", ".join(f"{p['service']} {p['current']}→{p['latest']}" for p in proposals)
                result.actions.append(f"Image updates available: {pnames}")
        except Exception as exc:
            result.errors.append(f"image-update scan: {exc}")
    except Exception as exc:
        result.errors.append(f"maintenance tasks block: {exc}")

    # Toolkit event log retention (same path as Watchdog._log_event)
    from toolkit.core.state.paths import watchdog_events_path

    events = watchdog_events_path(root)
    if events.is_file() and events.stat().st_size > 2_000_000:
        try:
            backup = events.with_suffix(".jsonl.bak")
            shutil.copy2(events, backup)
            lines = events.read_text().splitlines()
            events.write_text("\n".join(lines[-500:]) + "\n")
            result.actions.append("Trimmed watchdog events.jsonl to last 500 lines")
        except OSError as exc:
            result.errors.append(f"watchdog events trim: {exc}")

    result.actions.append(_disk_summary())
    action_failures, action_notices = _classify_maintenance_alerts(result.actions)
    for failure in action_failures:
        if failure not in result.errors:
            result.errors.append(failure)

    state_path = root / "data" / "maintenance" / "last-run.json"
    payload = result.to_dict()
    payload["vm"] = operation_node
    try:
        from toolkit.core.state.files import atomic_write_json

        atomic_write_json(state_path, payload)
    except OSError as exc:
        result.errors.append(f"state write: {exc}")

    if notify_on_attention and (result.errors or action_notices):
        _notify_maintenance_attention(root, result, vm=operation_node, notices=action_notices)
    from toolkit.core.state.audit_log import AuditAction, audit

    audit(
        root,
        AuditAction.MAINTENANCE,
        actor=actor or ("systemd" if os.environ.get("HOMELAB_NODE") else "cli"),
        ok=result.ok,
        detail="maintenance completed" if result.ok else "maintenance completed with errors",
        vm=operation_node,
        duration_s=time.monotonic() - started,
        extra={"action_count": len(result.actions), "error_count": len(result.errors)},
    )
    return result


def prometheus_metrics(root: Path) -> str:
    """Expose last maintenance run for Prometheus text scraping."""
    state_path = root / "data" / "maintenance" / "last-run.json"
    ts = 0.0
    ok = 1
    reboot_required = 0
    os_updates_healthy = -1
    if state_path.is_file():
        try:
            data = json.loads(state_path.read_text())
            ts = float(data.get("timestamp", 0))
            ok = 1 if data.get("ok", True) else 0
            reboot_required = 1 if data.get("reboot_required", False) else 0
            patch_state = data.get("os_updates_healthy")
            os_updates_healthy = -1 if patch_state is None else int(bool(patch_state))
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            ok = 0

    rc, df_out, _ = _run(["df", "--output=pcent", "/"], timeout=10)
    disk_pct = 0.0
    if rc == 0 and df_out:
        lines = [ln.strip().rstrip("%") for ln in df_out.splitlines() if ln.strip() and ln.strip() != "Use%"]
        if lines:
            try:
                disk_pct = float(lines[-1])
            except ValueError:
                pass

    return "\n".join(
        [
            "# HELP homelab_maintenance_last_run_timestamp_seconds Unix time of last maintenance",
            "# TYPE homelab_maintenance_last_run_timestamp_seconds gauge",
            f"homelab_maintenance_last_run_timestamp_seconds {ts:.0f}",
            "# HELP homelab_maintenance_last_ok Whether last maintenance succeeded (1/0)",
            "# TYPE homelab_maintenance_last_ok gauge",
            f"homelab_maintenance_last_ok {ok}",
            "# HELP homelab_root_disk_used_percent Root filesystem used percent",
            "# TYPE homelab_root_disk_used_percent gauge",
            f"homelab_root_disk_used_percent {disk_pct:.1f}",
            "# HELP homelab_os_reboot_required Whether installed OS updates require a reboot (1/0)",
            "# TYPE homelab_os_reboot_required gauge",
            f"homelab_os_reboot_required {reboot_required}",
            "# HELP homelab_os_updates_healthy Whether unattended OS upgrades are healthy (1/0, -1 unknown)",
            "# TYPE homelab_os_updates_healthy gauge",
            f"homelab_os_updates_healthy {os_updates_healthy}",
            "",
        ]
    )
