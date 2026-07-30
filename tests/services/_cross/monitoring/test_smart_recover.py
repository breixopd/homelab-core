"""Node-local watchdog recovery policy tests."""

from __future__ import annotations

import subprocess
from unittest.mock import patch

from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.ops.watchdog import HealthIssue, Watchdog, WatchdogReport
from toolkit.core.ops.watchdog.recover_policy import (
    RecoverAutoConfig,
    RecoverSignal,
    recover_decisions,
)

_NOW = 2_000_000_000.0


def _signal(service: str, *, severity: str = "critical", terminal: bool = False) -> RecoverSignal:
    return RecoverSignal(service=service, severity=severity, terminal=terminal)


def test_no_current_failures_produces_no_recovery_decisions():
    assert recover_decisions([], vm_for_service={}, now=_NOW) == {}


def test_multi_failure_is_scoped_to_the_affected_node():
    decisions = recover_decisions(
        [_signal("a"), _signal("b"), _signal("c"), _signal("healthy-warning", severity="warning")],
        vm_for_service={"a": "apps", "b": "apps", "c": "apps", "healthy-warning": "media"},
        now=_NOW,
    )

    assert set(decisions) == {"apps", "media"}
    assert decisions["apps"].trigger is True
    assert "3 critical" in decisions["apps"].reason
    assert decisions["media"].trigger is False


def test_failures_are_never_aggregated_across_nodes():
    decisions = recover_decisions(
        [_signal("a"), _signal("b"), _signal("c")],
        vm_for_service={"a": "apps", "b": "apps", "c": "media"},
        now=_NOW,
    )

    assert decisions["apps"].trigger is False
    assert decisions["media"].trigger is False


def test_terminal_failures_include_exhausted_restart_budgets():
    cfg = RecoverAutoConfig(terminal_threshold=2, multi_failure_min=10)
    decisions = recover_decisions(
        [
            _signal("a", severity="warning", terminal=True),
            RecoverSignal("b", severity="warning", restart_count=3),
        ],
        vm_for_service={"a": "apps", "b": "apps"},
        now=_NOW,
        cfg=cfg,
    )

    assert decisions["apps"].trigger is True
    assert "2 terminal" in decisions["apps"].reason


def test_duplicate_issues_for_one_service_count_once():
    decisions = recover_decisions(
        [_signal("a"), _signal("a"), _signal("b")],
        vm_for_service={"a": "apps", "b": "apps"},
        now=_NOW,
    )

    assert decisions["apps"].trigger is False


def test_cooldown_is_evaluated_per_node():
    signals = [_signal("a"), _signal("b"), _signal("c"), _signal("x"), _signal("y"), _signal("z")]
    decisions = recover_decisions(
        signals,
        vm_for_service={"a": "apps", "b": "apps", "c": "apps", "x": "media", "y": "media", "z": "media"},
        last_recover_at={"apps": _NOW - 1_800, "media": _NOW - 7_200},
        now=_NOW,
    )

    assert decisions["apps"].trigger is False
    assert "cooldown" in decisions["apps"].reason
    assert decisions["media"].trigger is True


def test_kill_switch_disables_every_node():
    decisions = recover_decisions(
        [_signal("a"), _signal("b"), _signal("c")],
        vm_for_service={"a": "apps", "b": "apps", "c": "apps"},
        now=_NOW,
        cfg=RecoverAutoConfig(enabled=False),
    )

    assert decisions["apps"].trigger is False
    assert "disabled" in decisions["apps"].reason


def test_recovery_is_always_non_destructive():
    decisions = recover_decisions(
        [_signal("a"), _signal("b"), _signal("c")],
        vm_for_service={"a": "apps", "b": "apps", "c": "apps"},
        now=_NOW,
    )

    assert decisions["apps"].trigger is True
    assert decisions["apps"].destroy_first is False


def _control_failure_report() -> WatchdogReport:
    return WatchdogReport(
        issues=[
            HealthIssue("caddy", "management", "critical", "down"),
            HealthIssue("homelab-ui", "management", "critical", "down"),
            HealthIssue("komodo-core", "management", "critical", "down"),
        ]
    )


def test_watchdog_runs_one_scoped_recovery_for_current_node_failures(tmp_path):
    cfg = Config(domain="localhost", services=ServicesConfig())
    watchdog = Watchdog(tmp_path, cfg)
    logs: list[str] = []

    with (
        patch.object(
            watchdog,
            "_run",
            return_value=subprocess.CompletedProcess([], 0, "", ""),
        ) as run,
        patch("toolkit.core.state.audit_log.audit"),
    ):
        watchdog._maybe_auto_trigger_recover(_control_failure_report(), logs)

    run.assert_called_once()
    command = run.call_args.args[0]
    assert command[-3:] == ["--node", cfg.control_node, "-y"]
    assert watchdog._last_auto_recover_at[cfg.control_node] > 0
    assert any("recover completed" in line for line in logs)


def test_watchdog_recovery_kill_switch_is_manifest_configured(tmp_path):
    cfg = Config(
        domain="localhost",
        services=ServicesConfig(),
        service_settings={"homelab-ui": {"auto-recover": False}},
    )
    watchdog = Watchdog(tmp_path, cfg)

    with patch.object(watchdog, "_run") as run:
        watchdog._maybe_auto_trigger_recover(_control_failure_report(), [])

    run.assert_not_called()


def test_failed_recovery_attempt_persists_node_cooldown(tmp_path):
    cfg = Config(domain="localhost", services=ServicesConfig())
    watchdog = Watchdog(tmp_path, cfg)

    with (
        patch.object(watchdog, "_run", side_effect=OSError("unavailable")),
        patch("toolkit.core.state.audit_log.audit"),
    ):
        watchdog._maybe_auto_trigger_recover(_control_failure_report(), [])

    restored = Watchdog(tmp_path, cfg)
    assert restored._last_auto_recover_at[cfg.control_node] > 0
