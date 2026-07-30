from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from toolkit.controller.contracts import JobKind, WebhookHealOperation
from toolkit.controller.operations import (
    OperationExecutionError,
    OperationPolicyDisabledError,
    build_operation_registry,
)
from toolkit.core.ops.watchdog import HealResult, HealthIssue, Watchdog, WatchdogReport


def _operation(service: str = "sonarr") -> WebhookHealOperation:
    return WebhookHealOperation(service=service, alert_fingerprint="a" * 64)


def test_handler_reobserves_target_and_uses_narrow_heal(tmp_path, monkeypatch) -> None:
    context = MagicMock(actor="webhook:grafana")
    service = SimpleNamespace(name="sonarr")
    view = SimpleNamespace(categories=[SimpleNamespace(services=[service])])
    watchdog = MagicMock()
    watchdog.restartable_services.return_value = frozenset({"sonarr"})
    issue = HealthIssue(
        service="sonarr",
        category="media",
        severity="critical",
        message="observed exited",
        auto_fixable=True,
    )
    watchdog.check_all.return_value = WatchdogReport(issues=[issue])
    watchdog.heal_targeted.return_value = HealResult(logs=["ok"], attempted=1, succeeded=1)
    monkeypatch.setattr("toolkit.controller.inventory_api.read_services_view", lambda *_args, **_kwargs: view)
    monkeypatch.setattr("toolkit.core.config.config.load_config", lambda _path: MagicMock())
    monkeypatch.setattr("toolkit.core.ops.watchdog.Watchdog", lambda *_args: watchdog)

    result = build_operation_registry(tmp_path).resolve(JobKind.WEBHOOK_HEAL)(context, _operation())

    assert result == {"ok": True, "service": "sonarr", "action": "succeeded"}
    watchdog.check_all.assert_called_once_with()
    targeted = watchdog.heal_targeted.call_args.args[0]
    assert [item.service for item in targeted.issues] == ["sonarr"]
    assert watchdog.heal_targeted.call_args.kwargs == {"service": "sonarr"}


def test_handler_rejects_non_webhook_actor_before_observation(tmp_path) -> None:
    context = MagicMock(actor="mtls:homelab-ui")

    with pytest.raises(OperationPolicyDisabledError):
        build_operation_registry(tmp_path).resolve(JobKind.WEBHOOK_HEAL)(context, _operation())


def test_targeted_watchdog_heal_never_calls_broad_vm_recovery(tmp_path, monkeypatch) -> None:
    watchdog = Watchdog.__new__(Watchdog)
    watchdog.root = tmp_path
    watchdog._restart_counts = {}
    watchdog._restart_timestamps = {}
    monkeypatch.setattr(watchdog, "restartable_services", lambda: frozenset({"sonarr"}))
    broad_recovery = MagicMock()
    monkeypatch.setattr(watchdog, "_maybe_auto_trigger_recover", broad_recovery)
    monkeypatch.setattr(watchdog, "structured_heal_services", lambda: frozenset())
    issue = HealthIssue(
        service="sonarr",
        category="media",
        severity="warning",
        message="notification only",
        auto_fixable=False,
    )

    result = watchdog.heal_targeted(WatchdogReport(issues=[issue]), service="sonarr")

    assert result.attempted == 0
    assert result.deferred == 0
    broad_recovery.assert_not_called()


def test_handler_fails_job_when_targeted_remedy_fails(tmp_path, monkeypatch) -> None:
    context = MagicMock(actor="webhook:grafana")
    view = SimpleNamespace(categories=[SimpleNamespace(services=[SimpleNamespace(name="sonarr")])])
    watchdog = MagicMock()
    watchdog.restartable_services.return_value = frozenset({"sonarr"})
    watchdog.check_all.return_value = WatchdogReport(
        issues=[HealthIssue("sonarr", "media", "critical", "down", auto_fixable=True)]
    )
    watchdog.heal_targeted.return_value = HealResult(attempted=1, failed=1)
    monkeypatch.setattr("toolkit.controller.inventory_api.read_services_view", lambda *_args, **_kwargs: view)
    monkeypatch.setattr("toolkit.core.config.config.load_config", lambda _path: MagicMock())
    monkeypatch.setattr("toolkit.core.ops.watchdog.Watchdog", lambda *_args: watchdog)

    with pytest.raises(OperationExecutionError, match="remedy failed"):
        build_operation_registry(tmp_path).resolve(JobKind.WEBHOOK_HEAL)(context, _operation())


def test_targeted_watchdog_heal_never_starts_missing_dependencies(tmp_path, monkeypatch) -> None:
    watchdog = Watchdog.__new__(Watchdog)
    watchdog.root = tmp_path
    watchdog._restart_counts = {}
    watchdog._restart_timestamps = {}
    monkeypatch.setattr(Watchdog, "_dependency_links", property(lambda _self: {"roundcube": ["mailserver"]}))
    monkeypatch.setattr(watchdog, "restartable_services", lambda: frozenset({"roundcube"}))
    monkeypatch.setattr(watchdog, "_docker_action", MagicMock(return_value=(0, "ok")))
    monkeypatch.setattr(watchdog, "verify_post_restart", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(watchdog, "_save_restart_state", lambda: None)
    monkeypatch.setattr(watchdog, "_log_event", lambda *_args: None)
    monkeypatch.setattr(watchdog, "_verify_cascade_consumers", lambda _service: [])
    monkeypatch.setattr(watchdog, "_maybe_auto_trigger_recover", MagicMock())
    issue = HealthIssue(
        service="roundcube",
        category="email",
        severity="critical",
        message="container exited",
        auto_fixable=True,
    )

    watchdog.heal_targeted(WatchdogReport(issues=[issue]), service="roundcube")

    actions = watchdog._docker_action.call_args_list
    assert [call.args[:2] for call in actions] == [("roundcube", "restart")]


def test_restart_budget_is_isolated_by_fleet_node(tmp_path, monkeypatch) -> None:
    watchdog = Watchdog.__new__(Watchdog)
    watchdog.root = tmp_path
    watchdog._restart_counts = {"media/edge": 3}
    watchdog._restart_timestamps = {}
    monkeypatch.setattr(watchdog, "restartable_services", lambda: frozenset({"edge"}))
    monkeypatch.setattr(watchdog, "structured_heal_services", lambda: frozenset())
    monkeypatch.setattr(Watchdog, "_dependency_links", property(lambda _self: {}))
    monkeypatch.setattr(watchdog, "_docker_action", MagicMock(return_value=(0, "ok")))
    monkeypatch.setattr(
        watchdog,
        "_docker_capture",
        MagicMock(return_value=SimpleNamespace(returncode=0, stdout="running:unhealthy")),
    )
    monkeypatch.setattr(watchdog, "verify_post_restart", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(watchdog, "_save_restart_state", lambda: None)
    monkeypatch.setattr(watchdog, "_log_event", lambda *_args: None)
    monkeypatch.setattr(watchdog, "_verify_cascade_consumers", lambda _service: [])
    issue = HealthIssue(
        service="edge",
        category="management",
        severity="critical",
        message="down",
        auto_fixable=True,
        node="apps",
    )

    result = watchdog.heal_targeted(WatchdogReport(issues=[issue]), service="edge")

    assert result.succeeded == 1
    assert watchdog._restart_counts == {"media/edge": 3, "apps/edge": 1}
    assert watchdog._docker_action.call_args.kwargs["node"] == "apps"
