"""Verify data models — the types used across the verify pipeline.

Extracted from hook_verify.py to break the near-circular import between
hook_verify.py and the plugin files. Plugins import VerifyCheck from here
instead of from hook_verify, so the dependency flows one way.

Classes: VerifyCheck, HookVerifyResult
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class VerifyCheck:
    """A single verification check result."""

    service: str
    check: str
    passed: bool
    detail: str = ""
    retryable: bool = True


@dataclass
class HookVerifyResult:
    """Result of running verify_hooks() — a collection of VerifyCheck items."""

    checks: list[VerifyCheck] = field(default_factory=list)

    @property
    def all_passed(self) -> bool:
        return all(c.passed for c in self.checks)

    @property
    def summary(self) -> str:
        passed = sum(1 for c in self.checks if c.passed)
        total = len(self.checks)
        return f"{passed}/{total} checks passed"

    @property
    def passed_count(self) -> int:
        return sum(1 for c in self.checks if c.passed)

    @property
    def failed_count(self) -> int:
        return sum(1 for c in self.checks if not c.passed)

    @property
    def failed_checks(self) -> list[VerifyCheck]:
        return [c for c in self.checks if not c.passed]

    @property
    def retryable_failures(self) -> list[VerifyCheck]:
        """Return failed checks that may improve on a subsequent attempt."""
        return [check for check in self.failed_checks if check.retryable]


def format_verify_report(result: HookVerifyResult) -> str:
    """Format a HookVerifyResult as a human-readable multi-line report."""
    lines: list[str] = []
    for check in sorted(result.checks, key=lambda c: (c.service, c.check)):
        flag = "✓" if check.passed else "✗"
        lines.append(f"  {flag} {check.service}.{check.check}: {check.detail}")
    lines.append("")
    lines.append(f"Summary: {result.summary}")
    return "\n".join(lines)
