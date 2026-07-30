"""Watchdog data models — dataclasses + constants extracted from watchdog.py.

Library-level leaf module: no toolkit imports, only stdlib + dataclasses.
The Watchdog class (in engine.py / watchdog.py) imports from here.

Constants:
- MAX_LOG_TAIL, MAX_STDERR_LEN, RESTART_BACKOFF_BASE
- NOTIFY_COOLDOWN_CRITICAL_S, NOTIFY_COOLDOWN_WARNING_S, NOTIFY_COOLDOWN_INFRA_S

Classes: HealthIssue, WatchdogEvent, WatchdogReport, HealResult
Functions: _issue_key, _parse_docker_uptime, check_all_report_kind
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import asdict, dataclass, field

MAX_LOG_TAIL = 30
MAX_STDERR_LEN = 200
# Restart backoff: attempt N waits BASE * 2^N seconds before retry
RESTART_BACKOFF_BASE = 5

# Notification cooldown: re-page a known-persistent issue after this many
# seconds, not every cycle. Critical infra containers are persistent failures
# (e.g. fleet unreachable) — paging every 5 min for 2 days is the alert-storm
# regression we're closing. Higher value = fewer repeated pages.
NOTIFY_COOLDOWN_CRITICAL_S = 30 * 60  # 30 min — persistent critical re-pages half-hourly
NOTIFY_COOLDOWN_WARNING_S = 2 * 60 * 60  # 2 h — warnings re-page twice-hourly at most
NOTIFY_COOLDOWN_INFRA_S = 6 * 60 * 60  # 6 h — "watchdog can't reach fleet/containers" only


def check_all_report_kind() -> str:
    """Severity kind to use for the 'no containers seen' check_all condition.

    The audit embraces this as a non-page severity. The ``watchdog-infra``
    kind participates in the long-cooldown path (six hours) instead of the
    per-cycle critical that historically drove the multi-day alert storm.
    """
    return "infra"


def _parse_docker_uptime(status: str) -> float:
    """Parse Docker status string to uptime in seconds.

    Docker reports: "Up 3 hours", "Up 2 days", "Up 45 minutes", "Up About an hour".
    Returns 0 if unable to parse.
    """
    lower = status.lower()
    if "up" not in lower:
        return 0
    total = 0.0
    for match in re.finditer(r"(\d+)\s+(second|minute|hour|day|week|month)", lower):
        value = int(match.group(1))
        unit = match.group(2)
        multipliers = {
            "second": 1,
            "minute": 60,
            "hour": 3600,
            "day": 86400,
            "week": 604800,
            "month": 2592000,
        }
        total += value * multipliers.get(unit, 0)
    # Handle "About an hour" / "About a minute"
    if total == 0:
        if "hour" in lower:
            total = 3600
        elif "minute" in lower:
            total = 60
    return total


@dataclass
class HealthIssue:
    """A detected health problem."""

    service: str
    category: str
    severity: str  # "critical", "warning", "info", "infra"
    message: str
    auto_fixable: bool = False
    diagnosis: str = ""
    node: str = ""
    # Set when ``heal()`` gave up on the issue: exceeded restart budget, or
    # the issue class is non-auto-fixable from inside the watchdog loop.
    # ``notify()`` honours this by silencing re-pages after the first one
    # so a permanently-broken component doesn't storm the channel.
    terminal: bool = False


@dataclass(frozen=True, slots=True)
class ContainerHealth:
    """Healthy container identity, including its fleet node when applicable."""

    name: str
    node: str = ""


def _issue_key(issue: HealthIssue) -> str:
    """Stable identity for an issue used by notify() dedup + cooldown.

    Hashed from service + category + message template (not message text —
    dynamic suffixes like '… N retries' would otherwise defeat dedup).
    Severity is NOT part of the key so escalation warning↔critical can be
    detected (and re-paged) on the same underlying issue.
    """
    raw = f"{issue.node}|{issue.service}|{issue.category}|{issue.message}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class WatchdogEvent:
    """A logged watchdog action."""

    timestamp: float
    action: str  # "check", "heal", "notify", "prune"
    service: str
    detail: str


@dataclass
class WatchdogReport:
    """Result of a watchdog health scan."""

    timestamp: float = field(default_factory=time.time)
    healthy: list[ContainerHealth] = field(default_factory=list)
    issues: list[HealthIssue] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        # 'infra' severity ("watchdog can't reach its fleet / no containers")
        # is a separate long-cooldown channel; it must not flip the whole
        # report into a per-cycle ERROR that pages forever. Only real
        # container/runtime criticals page via report.ok=False.
        return not any(i.severity == "critical" for i in self.issues)

    @property
    def has_infra_state(self) -> bool:
        """True when 'cannot reach Docker daemon / no containers' was emitted.

        A separate signal from ``ok``: surface it in the UI/audit but apply
        the long cooldown instead of treating it as a page-able critical.
        """
        return any(i.category == "watchdog-infra" for i in self.issues)

    def summary(self) -> str:
        n_ok = len(self.healthy)
        n_warn = sum(1 for i in self.issues if i.severity == "warning")
        n_crit = sum(1 for i in self.issues if i.severity == "critical")
        return f"{n_ok} healthy, {n_warn} warnings, {n_crit} critical"

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "healthy": [asdict(container) for container in self.healthy],
            "issues": [asdict(i) for i in self.issues],
            "summary": self.summary(),
            "ok": self.ok,
        }


@dataclass
class HealResult:
    """Verified healing outcomes for CLI, controller, audit, and API consumers."""

    logs: list[str] = field(default_factory=list)
    attempted: int = 0
    succeeded: int = 0
    failed: int = 0
    deferred: int = 0

    def __post_init__(self) -> None:
        counts = (self.attempted, self.succeeded, self.failed, self.deferred)
        if any(count < 0 for count in counts):
            raise ValueError("heal outcome counts cannot be negative")
        if self.attempted != self.succeeded + self.failed:
            raise ValueError("attempted remedies must equal succeeded plus failed remedies")

    @property
    def ok(self) -> bool:
        return self.failed == 0
