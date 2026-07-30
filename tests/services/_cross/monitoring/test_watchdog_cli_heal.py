"""User-visible watchdog healing outcome tests."""

from __future__ import annotations

from unittest.mock import patch

from click.testing import CliRunner
from toolkit.cli import main
from toolkit.core.ops.watchdog import HealResult, HealthIssue, WatchdogReport
from toolkit.core.state.audit_log import read_audit


class _FailedWatchdog:
    def __init__(self, *_args, **_kwargs):
        pass

    def full_check(self) -> WatchdogReport:
        return WatchdogReport(issues=[HealthIssue("sonarr", "media", "critical", "down", auto_fixable=True)])

    def heal(self, _report: WatchdogReport) -> HealResult:
        return HealResult(
            logs=["HEAL sonarr: restart failed"],
            attempted=1,
            failed=1,
        )

    def notify(self, _report: WatchdogReport) -> list[str]:
        return []


def test_heal_cli_exits_nonzero_and_audits_structured_failure(tmp_path):
    with patch("toolkit.core.ops.watchdog.Watchdog", _FailedWatchdog):
        result = CliRunner().invoke(
            main,
            ["--root", str(tmp_path), "watchdog", "heal", "--no-notify"],
        )

    assert result.exit_code == 1
    assert "Outcome: 0 succeeded, 1 failed, 0 deferred" in result.output
    assert "1 healing remedy failed" in result.output
    entry = read_audit(tmp_path, action="heal", limit=1)[0]
    assert entry["ok"] is False
    assert entry["extra"]["attempted"] == 1
    assert entry["extra"]["succeeded"] == 0
    assert entry["extra"]["failed"] == 1
    assert entry["extra"]["deferred"] == 0
