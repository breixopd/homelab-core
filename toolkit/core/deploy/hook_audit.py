"""Classify post-start hook lines by severity and persist audit summaries."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from pathlib import Path

from toolkit.core.state.paths import last_hooks_path

_CRITICAL_RE = re.compile(
    r"(?i)(hook error:|plugin error:|traceback|bootstrap failed|admin login failed|"
    r"could not authenticate|credential bootstrap failed|not reachable — skip|"
    r"deploy failed|fatal:|sync skipped \(vault login not ready\)|"
    r"setup error|systemd unit not active|not installed \(deploy security role\))",
)
_WARNING_RE = re.compile(
    r"(?i)(failed to add|failed to configure|sync returned non-zero|"
    r"not ready|not active|pending|skipped|http 4\d\d|error:|setup failed|"
    r"register returned|password login pending|could not add|"
    r"auto-wire failed|sync failed|not ready \()",
)
_INFO_OK_RE = re.compile(
    r"(?i)(already exists|already configured|reachable|configured|"
    r"password updated|created user|sync triggered successfully|"
    r"credentials ok|healthy|ok\b|complete|registered|synced \d+|"
    r"WebUI login probe failed|download clients may still work|"
    r"skipped indexer|cloudflare blocked on vpn|sync skipped \(|Tdarr: API not ready|"
    r"Wazuh Manager: API not reachable|"
    r"Headscale: not ready yet|"
    r"Jellyfin already linked — skipping|"
    r"reachable \(HTTP 40[14]\)|adguard: unexpected status HTTP 401|"
    r"pending removal)",
)


class HookSeverity(StrEnum):
    CRITICAL = "critical"
    WARNING = "warning"
    INFO = "info"
    OK = "ok"


@dataclass
class HookLine:
    vm: str
    category: str
    message: str
    severity: HookSeverity


@dataclass
class HookAuditSummary:
    vm: str
    critical: int = 0
    warning: int = 0
    info: int = 0
    ok: int = 0
    lines: list[HookLine] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        """Only CRITICAL failures abort the deploy (enable hands-off deploys).

        WARNINGs (soft failures like 'not ready yet', transient races, sync
        issues) are preserved in the summary for the manual-steps section but
        do NOT flip the deploy to failed — so a fresh cluster where AdGuard
        hasn't finished first-boot (the AdGuard race) doesn't abort everything.
        """
        return self.critical == 0

    @property
    def strict_passed(self) -> bool:
        """Return true only when no classified hook warning remains."""
        return self.critical == 0 and self.warning == 0


def classify_hook_message(message: str) -> HookSeverity:
    text = (message or "").strip()
    if not text:
        return HookSeverity.INFO
    if _CRITICAL_RE.search(text):
        return HookSeverity.CRITICAL
    # Explicit "WARNING:" prefix from _hook_warning() is always a warning,
    # unless it matches an info/ok allowlist entry (e.g. known-soft-fail messages).
    if _INFO_OK_RE.search(text):
        return HookSeverity.OK
    if text.upper().startswith("WARNING:"):
        return HookSeverity.WARNING
    if _WARNING_RE.search(text):
        return HookSeverity.WARNING
    if text.endswith("— skipping") or "skip" in text.lower():
        return HookSeverity.INFO
    return HookSeverity.OK


def audit_hook_results(results: dict[str, list[str]], *, vm_hint: str = "") -> HookAuditSummary:
    """Build severity summary from category→lines hook output."""
    summary = HookAuditSummary(vm=vm_hint or "all")
    for category, lines in results.items():
        for raw in lines:
            msg = raw.strip()
            if not msg:
                continue
            sev = classify_hook_message(msg)
            summary.lines.append(HookLine(vm=vm_hint or category, category=category, message=msg, severity=sev))
            if sev == HookSeverity.CRITICAL:
                summary.critical += 1
            elif sev == HookSeverity.WARNING:
                summary.warning += 1
            elif sev == HookSeverity.OK:
                summary.ok += 1
            else:
                summary.info += 1
    return summary


def save_last_hooks_report(root: Path, audits: dict[str, HookAuditSummary]) -> None:
    path = last_hooks_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        vm: {
            "passed": audit.passed,
            "critical": audit.critical,
            "warning": audit.warning,
            "info": audit.info,
            "ok": audit.ok,
            "lines": [asdict(line) for line in audit.lines[:200]],
        }
        for vm, audit in audits.items()
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def load_last_hooks_report(root: Path) -> dict[str, dict[str, object]] | None:
    """Load the most recent deploy hook audit without trusting malformed state."""
    path = last_hooks_path(root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    return {str(vm): value for vm, value in payload.items() if isinstance(value, dict)}


def strict_hooks_passed(root: Path) -> tuple[bool, str]:
    """Evaluate the persisted hook audit for a final, warning-free QA gate."""
    payload = load_last_hooks_report(root)
    if payload is None:
        return False, "no persisted post-start hook audit is available"
    if not payload:
        return False, "persisted post-start hook audit is empty"

    def count(value: object, key: str) -> int:
        raw = value.get(key, 0) if isinstance(value, dict) else 0
        if raw is None:
            return 0
        if isinstance(raw, bool):
            raise TypeError(f"{key} must be an integer")
        if isinstance(raw, int):
            return raw
        if isinstance(raw, str):
            return int(raw)
        raise TypeError(f"{key} must be an integer")

    try:
        warnings = sum(count(value, "warning") for value in payload.values())
        critical = sum(count(value, "critical") for value in payload.values())
    except (TypeError, ValueError):
        return False, "persisted post-start hook audit is malformed"
    if critical or warnings:
        return False, f"{critical} critical and {warnings} warning hook result(s) remain"
    return True, f"{len(payload)} node hook audit(s) clean"


def format_hook_audit(audit: HookAuditSummary) -> str:
    status = "OK" if audit.passed else "ISSUES"
    lines = [f"Hooks [{status}]: {audit.ok} ok, {audit.info} info, {audit.warning} warning, {audit.critical} critical"]
    for line in audit.lines:
        if line.severity in (HookSeverity.CRITICAL, HookSeverity.WARNING):
            lines.append(f"  [{line.severity.value}] {line.message}")
    return "\n".join(lines)
