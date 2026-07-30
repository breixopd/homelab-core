from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from toolkit.controller.contracts import HostReconcileOperation, HostRemoveOperation, JobKind
from toolkit.controller.operations import OperationExecutionError, build_operation_registry
from toolkit.core.config.config import Config, ExternalHost, load_config, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.deploy.external_deploy import ExternalDeployResult
from toolkit.core.infra.hosts import HostIntegrationReconcileResult, managed_host_fingerprint


def test_plain_host_reconcile_applies_integrations_agents_and_status(tmp_path, monkeypatch) -> None:
    host = ExternalHost(name="edge-01", ip="192.0.2.20", services=["monitoring-agent"])
    save_config(Config(domain="example.test", external_hosts=[host]), config_path(tmp_path))
    monkeypatch.setattr("toolkit.core.infra.hosts.trust_host_key", lambda *_args, **_kwargs: ["SSH trusted"])
    monkeypatch.setattr(
        "toolkit.core.infra.hosts.reconcile_host_integrations",
        lambda *_args, **_kwargs: HostIntegrationReconcileResult(("DNS reconciled",), ()),
    )
    monkeypatch.setattr(
        "toolkit.core.deploy.external_deploy.deploy_external_host",
        lambda *_args, **_kwargs: ExternalDeployResult(True, "Agents deployed", ["agent progress"]),
    )
    marked: list[str] = []
    monkeypatch.setattr(
        "toolkit.core.infra.hosts.mark_host_reconciled",
        lambda _root, name, _fingerprint: marked.append(name) or True,
    )
    context = MagicMock()

    result = build_operation_registry(tmp_path).resolve(JobKind.HOST_RECONCILE)(
        context,
        HostReconcileOperation(host_name="edge-01"),
    )

    assert result == {"ok": True, "host_name": "edge-01", "strategy": "external"}
    assert marked == ["edge-01"]
    context.log.assert_any_call("SSH trusted", {"stage": "host"})
    context.log.assert_any_call("DNS reconciled", {"stage": "host"})


def test_plain_host_reconcile_refreshes_runtime_nodes_after_controller_projection(tmp_path, monkeypatch) -> None:
    host = ExternalHost(name="edge-01", ip="192.0.2.20", services=[])
    save_config(Config(domain="example.test", external_hosts=[host]), config_path(tmp_path))
    monkeypatch.setattr("toolkit.core.infra.hosts.trust_host_key", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "toolkit.core.infra.hosts.reconcile_host_integrations",
        lambda *_args, **_kwargs: HostIntegrationReconcileResult((), (), ("media",)),
    )
    monkeypatch.setattr(
        "toolkit.core.infra.hosts.mark_host_reconciled",
        lambda *_args, **_kwargs: True,
    )
    calls: list[dict[str, object]] = []

    async def refresh(_root, _cfg, **kwargs):
        calls.append(kwargs)
        return SimpleNamespace(success=True)

    monkeypatch.setattr("toolkit.core.deploy.deploy_workflow.run_deploy_workflow", refresh)

    result = build_operation_registry(tmp_path).resolve(JobKind.HOST_RECONCILE)(
        MagicMock(),
        HostReconcileOperation(host_name="edge-01"),
    )

    assert result == {"ok": True, "host_name": "edge-01", "strategy": "external"}
    assert calls and calls[0]["targets"] == ("media",)
    assert calls[0]["skip_infra"] is True
    assert calls[0]["skip_dns"] is True


def test_plain_host_reconcile_does_not_mark_failed_integrations_healthy(tmp_path, monkeypatch) -> None:
    host = ExternalHost(name="edge-01", ip="192.0.2.20", services=["monitoring-agent"])
    save_config(Config(domain="example.test", external_hosts=[host]), config_path(tmp_path))
    monkeypatch.setattr("toolkit.core.infra.hosts.trust_host_key", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(
        "toolkit.core.infra.hosts.reconcile_host_integrations",
        lambda *_args, **_kwargs: HostIntegrationReconcileResult(
            ("Media cache: reconciliation failed",),
            ("Media cache: reconciliation failed",),
        ),
    )
    monkeypatch.setattr(
        "toolkit.core.deploy.external_deploy.deploy_external_host",
        lambda *_args, **_kwargs: pytest.fail("agent deployment must wait for required integration reconciliation"),
    )
    monkeypatch.setattr(
        "toolkit.core.infra.hosts.mark_host_reconciled",
        lambda *_args, **_kwargs: pytest.fail("failed host must not be marked reconciled"),
    )

    with pytest.raises(OperationExecutionError, match="integrations did not reconcile"):
        build_operation_registry(tmp_path).resolve(JobKind.HOST_RECONCILE)(
            MagicMock(),
            HostReconcileOperation(host_name="edge-01"),
        )


def test_host_remove_uses_fingerprint_bound_controller_resource(tmp_path, monkeypatch) -> None:
    removed: list[tuple[str, str]] = []
    monkeypatch.setattr(
        "toolkit.controller.managed_hosts_api.remove_managed_host",
        lambda _root, name, fingerprint, on_log=None, **_kwargs: removed.append((name, fingerprint)) or ["removed"],
    )
    context = MagicMock()
    operation = HostRemoveOperation(
        host_name="edge-01",
        expected_fingerprint="a" * 64,
        confirmation="edge-01",
    )

    result = build_operation_registry(tmp_path).resolve(JobKind.HOST_REMOVE)(context, operation)

    assert result == {"ok": True, "host_name": "edge-01"}
    assert removed == [("edge-01", "a" * 64)]


def test_host_remove_reuses_controller_operation_lease_for_real_mutation(tmp_path, monkeypatch) -> None:
    host = ExternalHost(name="edge-01", ip="192.0.2.20")
    save_config(Config(domain="example.test", external_hosts=[host]), config_path(tmp_path))
    monkeypatch.setattr(
        "toolkit.controller.managed_hosts_api.cleanup_host_resources",
        lambda *_args, **_kwargs: ["cleaned"],
    )

    result = build_operation_registry(tmp_path).resolve(JobKind.HOST_REMOVE)(
        MagicMock(),
        HostRemoveOperation(
            host_name=host.name,
            expected_fingerprint=managed_host_fingerprint(host),
            confirmation=host.name,
        ),
    )

    assert result == {"ok": True, "host_name": host.name}
    assert load_config(config_path(tmp_path)).external_hosts == []
