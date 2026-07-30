from __future__ import annotations

import json
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from toolkit.controller.contracts import ErrorBody, JobRecord, JobRequest, JobState, VerifyOperation
from toolkit.controller.dashboard_api import (
    _alerts,
    _job_summary,
    _operations_alerts,
    _portal_container_status,
    _runtime_snapshot,
    _runtime_summary,
    read_dashboard_metrics,
    read_dashboard_metrics_view,
    read_dashboard_view,
    read_portal_status,
)
from toolkit.controller.prometheus_api import RECORD_SEPARATOR
from toolkit.controller.read_models import (
    ContainerInventory,
    ContainerStatus,
    DashboardMetrics,
    DashboardOperationsSummary,
    MetricPoint,
)
from toolkit.core.config.config import Config, ServicesConfig, save_config
from toolkit.core.config.storage import config_path, env_path


def _prometheus(value) -> str:
    return json.dumps({"status": "success", "data": {"result": [{"value": [1, str(value)]}]}})


def test_portal_status_filters_internal_containers(monkeypatch, tmp_path: Path) -> None:
    cfg = Config(domain="example.com")
    save_config(cfg, config_path(tmp_path))
    inventory = ContainerInventory(
        is_available=True,
        unavailable_nodes=[],
        containers=[
            ContainerStatus(
                name="prometheus",
                node="infra",
                status="Up",
                state="running",
                image="prometheus:test",
                health="healthy",
            ),
            ContainerStatus(
                name="postgres",
                node="infra",
                status="Up",
                state="running",
                image="postgres:test",
                health="healthy",
            ),
        ],
    )
    monkeypatch.setattr(
        "toolkit.controller.dashboard_api._runtime_snapshot",
        lambda _root, _cfg: (DashboardMetrics(cpu_history=[]), inventory),
    )

    status = read_portal_status(tmp_path)

    assert status.complete is True
    assert status.services == {"prometheus": "online"}


def test_portal_container_status_is_small_and_stable() -> None:
    assert _portal_container_status("running", "healthy") == "online"
    assert _portal_container_status("running", "none") == "online"
    assert _portal_container_status("running", "starting") == "degraded"
    assert _portal_container_status("running", "unhealthy") == "degraded"
    assert _portal_container_status("restarting", "none") == "degraded"
    assert _portal_container_status("exited", "none") == "offline"
    assert _portal_container_status("dead", "none") == "offline"
    assert _portal_container_status("unknown", "none") == "unknown"


def test_dashboard_alerts_when_backup_content_is_unverified_failed_or_overdue() -> None:
    missing = _operations_alerts(DashboardOperationsSummary(backups_enabled=True), [])
    failed = _operations_alerts(
        DashboardOperationsSummary(
            backups_enabled=True,
            backup_drill_ok=False,
            backup_drill_last_run_at=datetime.now(UTC),
        ),
        [],
    )
    overdue = _operations_alerts(
        DashboardOperationsSummary(
            backups_enabled=True,
            backup_drill_ok=True,
            backup_drill_last_run_at=datetime.now(UTC) - timedelta(days=9),
        ),
        [],
    )

    assert any(alert.message == "Backup content has not been restore-verified" for alert in missing)
    assert any(alert.severity == "critical" and "drill failed" in alert.message.lower() for alert in failed)
    assert any(alert.severity == "critical" and "overdue" in alert.message.lower() for alert in overdue)


def test_dashboard_does_not_treat_archival_or_superseded_failures_as_current_attention() -> None:
    now = datetime.now(UTC)
    stale_failure = JobRecord(
        job_id="job-stale-failed",
        request=JobRequest(idempotency_key="dashboard-stale-failed", operation=VerifyOperation()),
        state=JobState.FAILED,
        actor="ui:homelab-ui",
        created_at=now - timedelta(days=2),
        updated_at=now - timedelta(days=2),
    )
    recent_success = JobRecord(
        job_id="job-recent-success",
        request=JobRequest(idempotency_key="dashboard-recent-success", operation=VerifyOperation()),
        state=JobState.SUCCEEDED,
        actor="ui:homelab-ui",
        created_at=now,
        updated_at=now,
    )
    superseded_failure = JobRecord(
        job_id="job-superseded-failed",
        request=JobRequest(idempotency_key="dashboard-superseded-failed", operation=VerifyOperation()),
        state=JobState.FAILED,
        actor="ui:homelab-ui",
        created_at=now - timedelta(hours=1),
        updated_at=now - timedelta(hours=1),
    )

    recent, _active, attention = _job_summary([recent_success, superseded_failure, stale_failure])
    alerts = _operations_alerts(DashboardOperationsSummary(), recent)

    assert attention == 0
    assert not any("job needs attention" in alert.message for alert in alerts)


def test_dashboard_ignores_manifest_declared_oneshot_and_remote_role_env(tmp_path: Path) -> None:
    cfg = Config(domain="example.test", proxmox={"provision_machines": True})
    inventory = ContainerInventory(
        is_available=True,
        unavailable_nodes=[],
        containers=[
            ContainerStatus(
                name="wazuh-indexer-certs-init",
                node="infra",
                status="Exited (0)",
                state="exited",
                image="example/wazuh",
                health="none",
                completed=True,
            ),
            ContainerStatus(
                name="actual-runtime",
                node="infra",
                status="Exited (1)",
                state="exited",
                image="example/runtime",
                health="none",
            ),
        ],
    )
    alerts = _alerts(
        cfg,
        tmp_path,
        DashboardMetrics(cpu=1, memory=1, disk=1, cpu_history=[]),
        inventory,
        metrics_service_href="/services",
        metrics_dashboard_href="/services",
    )
    messages = [alert.message for alert in alerts]
    assert not any("wazuh-indexer-certs-init" in message for message in messages)
    assert any("actual-runtime" in message for message in messages)
    assert any("infra" in message for message in messages)
    assert not any("media" in message or "apps" in message for message in messages)

    summary = _runtime_summary(cfg, inventory)
    assert summary.total == 1
    assert summary.exited == 1

    failed_oneshot = inventory.model_copy(
        update={
            "containers": [
                inventory.containers[0].model_copy(update={"status": "Exited (1)", "completed": False}),
            ]
        }
    )
    failed_alerts = _alerts(
        cfg,
        tmp_path,
        DashboardMetrics(cpu=1, memory=1, disk=1, cpu_history=[]),
        failed_oneshot,
        metrics_service_href="/services",
        metrics_dashboard_href="/services",
    )
    assert any("wazuh-indexer-certs-init" in alert.message for alert in failed_alerts)
    assert _runtime_summary(cfg, failed_oneshot).exited == 1


def test_dashboard_metrics_parses_one_batched_prometheus_result(monkeypatch, tmp_path: Path) -> None:
    history = json.dumps(
        {
            "status": "success",
            "data": {"result": [{"values": [[1, "12.34"], [2, "25.55"]]}]},
        }
    )
    output = RECORD_SEPARATOR.join(
        [
            _prometheus(12.34),
            _prometheus(45.67),
            _prometheus(78.9),
            _prometheus(8),
            _prometheus(9),
            _prometheus(1),
            history,
            history,
            history,
        ]
    )
    monkeypatch.setattr("toolkit.controller.dashboard_api.run_prometheus_urls", lambda *_args: output)

    metrics = read_dashboard_metrics(tmp_path, Config(proxmox={"provision_machines": False}))

    assert metrics.cpu == 12.3
    assert metrics.memory == 45.7
    assert metrics.disk == 78.9
    assert metrics.containers == 8
    assert metrics.targets_up == 9
    assert metrics.targets_down == 1
    assert metrics.cpu_history == [
        MetricPoint(timestamp_ms=1000, value=12.3),
        MetricPoint(timestamp_ms=2000, value=25.6),
    ]
    assert metrics.memory_history == metrics.cpu_history
    assert metrics.disk_history == metrics.cpu_history


def test_runtime_snapshot_returns_stale_data_during_one_background_refresh(monkeypatch, tmp_path: Path) -> None:
    import toolkit.controller.dashboard_api as dashboard_api

    cfg = Config(proxmox={"provision_machines": False})
    stale_metrics = DashboardMetrics(cpu=12, cpu_history=[])
    stale_inventory = ContainerInventory(is_available=True, unavailable_nodes=[], containers=[])
    refreshed_metrics = DashboardMetrics(cpu=24, cpu_history=[])
    refreshed_inventory = ContainerInventory(is_available=True, unavailable_nodes=[], containers=[])
    key = dashboard_api._runtime_key(tmp_path, cfg)
    dashboard_api._runtime_cache[key] = (
        time.monotonic() - dashboard_api._RUNTIME_CACHE_TTL_SECONDS - 1,
        stale_metrics,
        stale_inventory,
        False,
    )
    release = threading.Event()
    started = threading.Event()
    calls = 0

    def refresh(*_args):
        nonlocal calls
        calls += 1
        started.set()
        assert release.wait(timeout=2)
        return refreshed_metrics, refreshed_inventory

    monkeypatch.setattr(dashboard_api, "_build_runtime_snapshot", refresh)

    assert _runtime_snapshot(tmp_path, cfg) == (stale_metrics, stale_inventory)
    assert started.wait(timeout=1)
    assert _runtime_snapshot(tmp_path, cfg) == (stale_metrics, stale_inventory)
    assert calls == 1
    release.set()
    for _ in range(100):
        if _runtime_snapshot(tmp_path, cfg) == (refreshed_metrics, refreshed_inventory):
            break
        time.sleep(0.01)
    assert _runtime_snapshot(tmp_path, cfg) == (refreshed_metrics, refreshed_inventory)
    assert calls == 1


def test_runtime_snapshot_bounds_cold_wait_and_reports_unavailable(monkeypatch, tmp_path: Path) -> None:
    import toolkit.controller.dashboard_api as dashboard_api

    cfg = Config(proxmox={"provision_machines": False})
    release = threading.Event()

    def refresh(*_args):
        assert release.wait(timeout=2)
        return DashboardMetrics(cpu=10, cpu_history=[]), ContainerInventory(
            is_available=True,
            unavailable_nodes=[],
            containers=[],
        )

    monkeypatch.setattr(dashboard_api, "_build_runtime_snapshot", refresh)
    monkeypatch.setattr(dashboard_api, "_RUNTIME_COLD_WAIT_SECONDS", 0.01)

    started_at = time.monotonic()
    metrics, inventory = _runtime_snapshot(tmp_path, cfg)
    elapsed = time.monotonic() - started_at
    release.set()

    assert elapsed < 0.5
    assert metrics == DashboardMetrics(cpu_history=[])
    assert inventory.is_available is False
    assert inventory.unavailable_nodes == [cfg.control_node]


def test_runtime_snapshot_sanitizes_refresh_failure(monkeypatch, tmp_path: Path) -> None:
    import toolkit.controller.dashboard_api as dashboard_api

    cfg = Config(proxmox={"provision_machines": False})

    def fail(*_args):
        raise RuntimeError("private backend detail")

    monkeypatch.setattr(dashboard_api, "_build_runtime_snapshot", fail)

    metrics, inventory = _runtime_snapshot(tmp_path, cfg)

    assert metrics == DashboardMetrics(cpu_history=[])
    assert inventory.is_available is False
    assert inventory.unavailable_nodes == [cfg.control_node]


def test_uninitialized_dashboard_never_probes_runtime(monkeypatch, tmp_path: Path) -> None:
    def fail(_root):
        raise AssertionError("runtime inventory must not run before configuration")

    monkeypatch.setattr("toolkit.controller.dashboard_api.read_container_inventory", fail)

    view = read_dashboard_view(tmp_path, family=False, groups=[])

    assert view.state == "uninitialized"
    assert view.metrics == DashboardMetrics(cpu_history=[])


def test_config_only_dashboard_never_probes_undeployed_runtime(monkeypatch, tmp_path: Path) -> None:
    save_config(
        Config(
            domain="example.test",
            services=ServicesConfig(
                management=True,
                media=False,
                cloud=False,
                notifications=False,
                email=False,
                security=False,
            ),
        ),
        config_path(tmp_path),
    )

    def fail(*_args):
        raise AssertionError("runtime must not be probed before generated environments exist")

    monkeypatch.setattr("toolkit.controller.dashboard_api.read_container_inventory", fail)
    monkeypatch.setattr("toolkit.controller.dashboard_api.read_dashboard_metrics", fail)

    view = read_dashboard_view(tmp_path, family=False, groups=[])

    assert view.state == "config_only"
    assert view.metrics == DashboardMetrics(cpu_history=[])


def test_operator_dashboard_ignores_non_service_directory_groups(monkeypatch, tmp_path: Path) -> None:
    save_config(
        Config(
            domain="example.test",
            services=ServicesConfig(management=True),
        ),
        config_path(tmp_path),
    )
    monkeypatch.setattr(
        "toolkit.controller.dashboard_api.read_container_inventory",
        lambda *_args: ContainerInventory(is_available=False, containers=[], unavailable_nodes=[]),
    )

    view = read_dashboard_view(tmp_path, family=False, groups=["homelab-users", "lldap_admin"])

    assert view.state == "config_only"


def test_dashboard_combines_typed_runtime_alerts_without_error_details(monkeypatch, tmp_path: Path) -> None:
    cfg = Config(
        domain="example.test",
        owner_password="owner-password-canary",
        services=ServicesConfig(
            management=True,
            media=False,
            cloud=False,
            notifications=False,
            email=False,
            security=False,
        ),
        proxmox={"provision_machines": False},
    )
    save_config(cfg, config_path(tmp_path))
    generated = env_path("infra", tmp_path)
    generated.parent.mkdir(parents=True)
    generated.write_text("generated=true\n")
    verify_path = tmp_path / ".homelab-state" / "last-verify.json"
    verify_path.parent.mkdir(exist_ok=True)
    verify_path.write_text(
        json.dumps(
            {
                "infra": {
                    "ok": True,
                    "healthy": 4,
                    "unhealthy": 0,
                    "pending": 0,
                    "errors": ["internal detail that is not part of the read model"],
                }
            }
        )
    )
    monkeypatch.setattr(
        "toolkit.controller.dashboard_api.read_container_inventory",
        lambda _root: ContainerInventory(
            is_available=True,
            unavailable_nodes=[],
            containers=[
                ContainerStatus(
                    name="grafana",
                    node="infra",
                    status="Up (unhealthy)",
                    state="running",
                    image="grafana/grafana:12",
                    health="unhealthy",
                )
            ],
        ),
    )
    metric_calls = 0

    def metrics(*_args):
        nonlocal metric_calls
        metric_calls += 1
        return DashboardMetrics(memory=91.0, cpu_history=[])

    monkeypatch.setattr("toolkit.controller.dashboard_api.read_dashboard_metrics", metrics)

    view = read_dashboard_view(tmp_path, family=False, groups=[])
    serialized = view.model_dump_json()

    assert view.state == "ready"
    assert view.health == "critical"
    assert view.runtime.total == 1
    assert view.runtime.unhealthy == 1
    assert view.last_verify is not None
    assert view.last_verify["infra"].healthy == 4
    assert "critical" in {alert.severity for alert in view.alerts}
    assert "owner-password-canary" not in serialized
    assert read_dashboard_metrics_view(tmp_path).memory == 91.0
    assert metric_calls == 1


def test_dashboard_projects_jobs_maintenance_backups_and_pending_hosts(monkeypatch, tmp_path: Path) -> None:
    from toolkit.core.config.config import ExternalHost

    cfg = Config(
        domain="example.test",
        services=ServicesConfig(
            management=True,
            media=False,
            cloud=False,
            notifications=False,
            email=False,
            security=False,
        ),
        proxmox={"provision_machines": False},
        external_hosts=[ExternalHost(name="edge-01", ip="192.0.2.20")],
    )
    save_config(cfg, config_path(tmp_path))
    generated = env_path("infra", tmp_path)
    generated.parent.mkdir(parents=True)
    generated.write_text("generated=true\n")
    maintenance = tmp_path / "data" / "maintenance" / "last-run.json"
    maintenance.parent.mkdir(parents=True)
    maintenance.write_text(json.dumps({"timestamp": 1_786_400_000, "ok": True, "actions": [], "errors": []}))
    monkeypatch.setattr(
        "toolkit.controller.dashboard_api.read_container_inventory",
        lambda _root: ContainerInventory(is_available=True, unavailable_nodes=[], containers=[]),
    )
    monkeypatch.setattr(
        "toolkit.controller.dashboard_api.read_dashboard_metrics",
        lambda *_args: DashboardMetrics(cpu=8, memory=20, disk=30, cpu_history=[]),
    )
    now = datetime.now(UTC)
    jobs = [
        JobRecord(
            job_id="job-running-1234",
            request=JobRequest(idempotency_key="dashboard-running-1234", operation=VerifyOperation()),
            state=JobState.RUNNING,
            actor="ui:homelab-ui",
            created_at=now,
            updated_at=now,
        ),
        JobRecord(
            job_id="job-failed-12345",
            request=JobRequest(idempotency_key="dashboard-failed-12345", operation=VerifyOperation()),
            state=JobState.FAILED,
            actor="ui:homelab-ui",
            created_at=now,
            updated_at=now,
            error=ErrorBody(code="OPERATION_FAILED", message="private failure detail"),
        ),
    ]

    view = read_dashboard_view(tmp_path, family=False, groups=[], jobs=jobs)

    assert view.active_jobs == 1
    assert view.attention_jobs == 0
    assert [job.job_id for job in view.recent_jobs] == ["job-running-1234", "job-failed-12345"]
    assert view.operations.maintenance_ok is True
    assert view.operations.backups_enabled is False
    assert view.operations.managed_hosts == 1
    assert view.operations.pending_hosts == 1
    assert any(alert.href == "/operations" for alert in view.alerts)
    assert "private failure detail" not in view.model_dump_json()
