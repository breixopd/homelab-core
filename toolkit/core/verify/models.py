"""Verify data models — the types used across the verify pipeline.

Extracted from hook_verify.py to break the near-circular import between
hook_verify.py and the plugin files. Plugins import VerifyCheck from here
instead of from hook_verify, so the dependency flows one way.

Classes: VerifyCheck, HookVerifyResult
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum


class VerifyStatus(StrEnum):
    """Canonical outcome of a verification check.

    ``PASS`` is the only state counted as successful. ``NOT_APPLICABLE`` is
    readiness-neutral; the remaining states block readiness.
    """

    PASS = "pass"
    FAIL = "fail"
    NOT_APPLICABLE = "not_applicable"
    DEGRADED = "degraded"
    NOT_READY = "not_ready"


@dataclass(init=False)
class VerifyCheck:
    """A single verification check result."""

    service: str
    check: str
    passed: bool
    detail: str
    retryable: bool
    status: VerifyStatus

    def __init__(
        self,
        service: str,
        check: str,
        passed: bool,
        detail: str = "",
        retryable: bool = True,
        *,
        status: VerifyStatus | str | None = None,
    ) -> None:
        """Normalize legacy booleans and explicit status values.

        Existing plugins construct checks with a boolean third argument.  We
        retain that interface while making state explicit for new callers.
        Legacy successful checks whose detail clearly says they were skipped
        or not applicable are normalized to ``NOT_APPLICABLE`` so they cannot
        be counted as readiness passes.
        """
        self.service = service
        self.check = check
        self.detail = detail
        self.retryable = retryable

        if status is None:
            normalized_detail = detail.lower()
            legacy_not_applicable = (
                normalized_detail.startswith("skipped (")
                or normalized_detail.endswith("(skipped)")
                or "not applicable" in normalized_detail
                or normalized_detail.endswith(" not enabled")
                or normalized_detail == "single-host skip"
            )
            if passed and legacy_not_applicable:
                resolved_status = VerifyStatus.NOT_APPLICABLE
            else:
                resolved_status = VerifyStatus.PASS if passed else VerifyStatus.FAIL
        else:
            resolved_status = VerifyStatus(status)

        if resolved_status is VerifyStatus.PASS and not passed:
            raise ValueError("PASS status requires passed=True")
        if resolved_status is VerifyStatus.NOT_APPLICABLE and not passed:
            raise ValueError("NOT_APPLICABLE status requires passed=True")
        if resolved_status in {VerifyStatus.FAIL, VerifyStatus.DEGRADED, VerifyStatus.NOT_READY} and passed:
            raise ValueError(f"{resolved_status.value} status requires passed=False")

        # Preserve the existing Boolean interface while status becomes the
        # authoritative aggregate/reporting contract. A legacy skipped check
        # remains non-failing to direct callers but is never counted as PASS.
        self.status = resolved_status
        self.passed = passed


@dataclass
class HookVerifyResult:
    """Result of running verify_hooks() — a collection of VerifyCheck items."""

    checks: list[VerifyCheck] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(c.status in {VerifyStatus.PASS, VerifyStatus.NOT_APPLICABLE} for c in self.checks)

    @property
    def summary(self) -> str:
        passed = sum(1 for c in self.checks if c.status is VerifyStatus.PASS)
        total = sum(1 for c in self.checks if c.status is not VerifyStatus.NOT_APPLICABLE)
        return f"{passed}/{total} checks passed"

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.status is VerifyStatus.PASS)

    @property
    def failed_count(self) -> int:
        return sum(
            1 for c in self.checks if c.status in {VerifyStatus.FAIL, VerifyStatus.DEGRADED, VerifyStatus.NOT_READY}
        )

    @property
    def failed_checks(self) -> list[VerifyCheck]:
        return [
            c for c in self.checks if c.status in {VerifyStatus.FAIL, VerifyStatus.DEGRADED, VerifyStatus.NOT_READY}
        ]

    @property
    def retryable_failures(self) -> list[VerifyCheck]:
        """Return failed checks that may improve on a subsequent attempt."""
        return [check for check in self.failed_checks if check.retryable]


def format_verify_report(result: HookVerifyResult) -> str:
    """Format a HookVerifyResult as a human-readable multi-line report."""
    lines: list[str] = []
    for check in sorted(result.checks, key=lambda c: (c.service, c.check)):
        flag = {
            VerifyStatus.PASS: "✓",
            VerifyStatus.FAIL: "✗",
            VerifyStatus.NOT_APPLICABLE: "○",
            VerifyStatus.DEGRADED: "⚠",
            VerifyStatus.NOT_READY: "…",
        }[check.status]
        lines.append(f"  {flag} {check.service}.{check.check}: {check.detail}")
    lines.append("")
    lines.append(f"Summary: {result.summary}")
    return "\n".join(lines)
