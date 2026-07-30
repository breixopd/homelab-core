"""Automated maintenance task implementations.

These are the scheduled, idempotent, guardrailed chores run_maintenance()
orchestrates. Each is a small, testable function; subprocess calls are the
only side effects and they're guarded (best-effort, non-fatal).

- :class:`MaintenanceConfig` — tunable maintenance policy.
- :func:`check_cert_expiries` — emit a warning line per cert under the warning window.
- :func:`scan_image_updates` — diff current vs latest image versions per service.
"""

from __future__ import annotations

from pathlib import Path

from toolkit.core.config.config import MaintenanceConfig

__all__ = [
    "MaintenanceConfig",
    "check_cert_expiries",
    "scan_image_updates",
]


# --- Cert-expiry check -----------------------------------------------------


def check_cert_expiries(
    domain: str,
    *,
    days_left: int,
    warning_days: int = 14,
) -> list[str]:
    """Emit a warning log line when the cert is under the warning window.

    Renewal itself stays delegated to Caddy ACME — this is the proactive
    "notify before it expires" half of closed-loop cert maintenance.
    """
    logs: list[str] = []
    if days_left < 0:
        logs.append(f"CRITICAL: cert for {domain} expired {-days_left}d ago — investigate Caddy ACME")
    elif days_left < warning_days:
        logs.append(f"WARN: cert for {domain} expires in {days_left}d (< {warning_days}d warning)")
    return logs


# --- Image-update scan -----------------------------------------------------


def _fetch_image_versions(root: Path) -> dict[str, dict[str, str]]:
    """Refresh and return controller-compatible image update candidates."""
    from toolkit.core.config.config import load_config
    from toolkit.core.config.storage import config_path
    from toolkit.core.ops.update_plan import load_current_update_plan, write_update_scan_compose
    from toolkit.core.ops.updates import run_check

    cfg = load_config(config_path(root))
    report = run_check(root, refresh=True, compose_file=write_update_scan_compose(root, cfg))
    if not report:
        return {}
    plan = load_current_update_plan(root, cfg)
    if plan is None:
        return {}
    return {
        candidate.service: {"current": candidate.current, "latest": candidate.target} for candidate in plan.candidates
    }


def scan_image_updates(
    *,
    root: Path | None = None,
    cfg: MaintenanceConfig | None = None,
) -> list[dict[str, str]]:
    """Return update proposals: {service, current, latest} for out-of-date services.

    The maintenance runner surfaces proposals through its notification path;
    applying an update remains a separate, explicit operation.
    """
    cfg = cfg or MaintenanceConfig()
    if not cfg.image_update_scan:
        return []

    versions = _fetch_image_versions((root or Path.cwd()).resolve())
    proposals: list[dict[str, str]] = []
    for service, info in (versions or {}).items():
        current = (info or {}).get("current", "")
        latest = (info or {}).get("latest", "")
        if current and latest and current != latest:
            proposals.append({"service": service, "current": current, "latest": latest})
    return proposals


__all__ = [
    "MaintenanceConfig",
    "check_cert_expiries",
    "scan_image_updates",
]
