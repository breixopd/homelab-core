"""Unified append-only audit log for all homelab operations.

Every significant action — deploy, hook, verify, heal, reconcile, secret rotation —
appends a structured entry here. This gives a single timeline for "what happened
and when" across the whole homelab, queryable by the UI and CLI.

The log is JSONL (one JSON object per line) for easy streaming/grep, stored at
``<root>/.homelab-state/audit.log``. Rotated when it exceeds 10 MB.

Usage:
    from toolkit.core.state.audit_log import audit, AuditAction

    audit(root, AuditAction.DEPLOY, actor="cli", detail="media up", vm="media")
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

_MAX_LOG_BYTES = 10 * 1024 * 1024  # 10 MB
_KEEP_ON_ROTATE_LINES = 50_000  # keep the most recent 50k lines on rotation


class AuditAction(StrEnum):
    DEPLOY = "deploy"
    HOOK = "hook"
    VERIFY = "verify"
    HEAL = "heal"
    RECONCILE = "reconcile"
    SECRET_ROTATE = "secret_rotate"
    BACKUP = "backup"
    MAINTENANCE = "maintenance"
    RESTORE = "restore"
    WATCHDOG = "watchdog"
    MANUAL = "manual"
    DESTROY = "destroy"
    SYNC_DNS = "sync_dns"
    CONFIG_SAVE = "config_save"


@dataclass(slots=True)
class AuditEntry:
    """One audit event."""

    ts: float  # unix epoch
    action: str  # AuditAction value
    actor: str = "system"  # who/what triggered it (cli, timer, watchdog, username)
    ok: bool = True
    detail: str = ""
    vm: str | None = None  # infra / media / apps / None
    duration_s: float | None = None  # how long the action took
    extra: dict[str, Any] = field(default_factory=dict)


def _audit_log_path(root: Path) -> Path:
    """Return the audit log path, creating the state dir if needed."""
    d = root / ".homelab-state"
    d.mkdir(parents=True, exist_ok=True)
    return d / "audit.log"


def _maybe_rotate(path: Path) -> None:
    """Rotate the log if it exceeds the size limit (keep last 50k lines)."""
    try:
        size = path.stat().st_size
    except OSError:
        return
    if size < _MAX_LOG_BYTES:
        return
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    keep = "\n".join(lines[-_KEEP_ON_ROTATE_LINES:]) + "\n"
    backup = path.with_suffix(".1.log")
    try:
        path.replace(backup)
        path.write_text(keep, encoding="utf-8")
    except OSError:
        pass


def audit(
    root: Path,
    action: AuditAction | str,
    *,
    actor: str = "system",
    ok: bool = True,
    detail: str = "",
    vm: str | None = None,
    duration_s: float | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """Append one audit entry. Never raises — logging is best-effort."""
    entry = AuditEntry(
        ts=time.time(),
        action=str(action.value if isinstance(action, AuditAction) else action),
        actor=actor,
        ok=ok,
        detail=detail[:500],  # cap detail length
        vm=vm,
        duration_s=duration_s,
        extra=extra or {},
    )
    try:
        path = _audit_log_path(root)
        _maybe_rotate(path)
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(entry), separators=(",", ":")) + "\n")
    except OSError:
        pass  # audit logging must never break the operation


def read_audit(
    root: Path,
    *,
    action: str | None = None,
    vm: str | None = None,
    actor: str | None = None,
    since_ts: float | None = None,
    limit: int = 200,
) -> list[dict[str, Any]]:
    """Read recent audit entries, newest last. Optional filters."""
    path = _audit_log_path(root)
    if not path.exists():
        return []
    results: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if action and entry.get("action") != action:
                    continue
                if vm and entry.get("vm") != vm:
                    continue
                if actor and entry.get("actor") != actor:
                    continue
                if since_ts and entry.get("ts", 0) < since_ts:
                    continue
                results.append(entry)
                if len(results) >= limit:
                    break
    except OSError:
        pass
    return results
