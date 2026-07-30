"""Controller-owned dashboard snapshot and bounded metric collection."""

from __future__ import annotations

import json
import logging
import math
import os
import threading
import time
import urllib.parse
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from toolkit.controller.contracts import JobKind, JobRecord, JobState, job_can_cancel
from toolkit.controller.inventory_api import (
    InventoryRequestError,
    as_node_name,
    read_container_inventory,
    serialize_bookmarks,
)
from toolkit.controller.operations_api import read_operations_view
from toolkit.controller.prometheus_api import RECORD_SEPARATOR, run_prometheus_urls
from toolkit.controller.read_models import (
    ContainerInventory,
    DashboardAction,
    DashboardAlert,
    DashboardCategory,
    DashboardJobView,
    DashboardMetrics,
    DashboardOperationsSummary,
    DashboardRuntimeSummary,
    DashboardView,
    MetricPoint,
    PortalStatus,
    VerifyNodeSummary,
)
from toolkit.core.compose.registry import enabled_categories, load_all
from toolkit.core.config.config import Config, ToolkitState, get_state, load_config
from toolkit.core.config.storage import config_path, env_path
from toolkit.core.identity.service_groups import validate_service_groups
from toolkit.core.ops.family_portal import family_portal_groups, tier_labels_for_groups
from toolkit.core.ops.manual_steps import get_all_manual_guidance
from toolkit.core.ops.portal_bookmarks import portal_bookmark_groups

_MAX_VERIFY_OUTPUT = 2 * 1024 * 1024
_MANUAL_STEP_PRIORITY = {"Prerequisite": 0, "Required": 1, "Verify": 2, "Optional": 3}
_RUNTIME_CACHE_TTL_SECONDS = 10.0
_RUNTIME_ERROR_TTL_SECONDS = 2.0
_RUNTIME_COLD_WAIT_SECONDS = 2.0
_RUNTIME_CACHE_LIMIT = 16
_RUNTIME_INFLIGHT_LIMIT = 8
_RuntimeKey = tuple[Path, str]
logger = logging.getLogger(__name__)
_runtime_refresh_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="dashboard-runtime")
_runtime_cache: dict[_RuntimeKey, tuple[float, DashboardMetrics, ContainerInventory, bool]] = {}
_runtime_inflight: dict[_RuntimeKey, Future[tuple[DashboardMetrics, ContainerInventory]]] = {}
_runtime_cache_lock = threading.Lock()


def _state_value(state: ToolkitState) -> Literal["uninitialized", "config_only", "ready"]:
    if state is ToolkitState.UNINITIALIZED:
        return "uninitialized"
    if state is ToolkitState.CONFIG_ONLY:
        return "config_only"
    return "ready"


def _empty_metrics() -> DashboardMetrics:
    return DashboardMetrics()


def _prometheus_urls() -> list[str]:
    cpu = '100 - (avg(rate(node_cpu_seconds_total{mode="idle"}[5m])) * 100)'
    memory = "(1 - node_memory_MemAvailable_bytes / node_memory_MemTotal_bytes) * 100"
    disk = '(1 - node_filesystem_avail_bytes{mountpoint="/"} / node_filesystem_size_bytes{mountpoint="/"}) * 100'
    queries = [
        cpu,
        memory,
        disk,
        'count(container_last_seen{name!=""})',
        "sum(up) or vector(0)",
        "count(up == 0) or vector(0)",
    ]
    urls = [f"http://127.0.0.1:9090/api/v1/query?query={urllib.parse.quote(query, safe='')}" for query in queries]
    end = int(time.time())
    urls.extend(
        "http://127.0.0.1:9090/api/v1/query_range"
        f"?query={urllib.parse.quote(query, safe='')}&start={end - 3600}&end={end}&step=60"
        for query in (cpu, memory, disk)
    )
    return urls


def _result_values(payload: Any, *, ranged: bool = False) -> list:
    if not isinstance(payload, dict) or payload.get("status") != "success":
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    result = data.get("result")
    if not isinstance(result, list) or not result or not isinstance(result[0], dict):
        return []
    values = result[0].get("values" if ranged else "value")
    return values if isinstance(values, list) else []


def _number(payload: Any) -> float | None:
    value = _result_values(payload)
    if len(value) != 2:
        return None
    try:
        return float(value[1])
    except (TypeError, ValueError):
        return None


def read_dashboard_metrics(root: Path, cfg: Config) -> DashboardMetrics:
    output = run_prometheus_urls(root, cfg, _prometheus_urls())
    parts = output.split(RECORD_SEPARATOR)
    if len(parts) < 9:
        return _empty_metrics()
    payloads: list[Any] = []
    for part in parts[:9]:
        try:
            payloads.append(json.loads(part))
        except json.JSONDecodeError:
            payloads.append({})

    def percentage(payload: Any) -> float | None:
        value = _number(payload)
        return round(min(100.0, max(0.0, value)), 1) if value is not None and math.isfinite(value) else None

    def history(payload: Any) -> list[MetricPoint]:
        points: list[MetricPoint] = []
        for pair in _result_values(payload, ranged=True)[:1_440]:
            if not isinstance(pair, list) or len(pair) != 2:
                continue
            try:
                timestamp = float(pair[0])
                value = float(pair[1])
                if not math.isfinite(timestamp) or not math.isfinite(value):
                    continue
                points.append(
                    MetricPoint(
                        timestamp_ms=int(timestamp * 1000),
                        value=round(min(100.0, max(0.0, value)), 1),
                    )
                )
            except (TypeError, ValueError, ValidationError):
                continue
        return points

    counts = [_number(payloads[index]) for index in range(3, 6)]
    return DashboardMetrics(
        cpu=percentage(payloads[0]),
        memory=percentage(payloads[1]),
        disk=percentage(payloads[2]),
        containers=int(counts[0]) if counts[0] is not None and math.isfinite(counts[0]) and counts[0] >= 0 else None,
        targets_up=int(counts[1]) if counts[1] is not None and math.isfinite(counts[1]) and counts[1] >= 0 else None,
        targets_down=int(counts[2]) if counts[2] is not None and math.isfinite(counts[2]) and counts[2] >= 0 else None,
        cpu_history=history(payloads[6]),
        memory_history=history(payloads[7]),
        disk_history=history(payloads[8]),
    )


def read_dashboard_metrics_view(root: Path) -> DashboardMetrics:
    root = root.resolve()
    if get_state(root) is not ToolkitState.READY:
        return _empty_metrics()
    cfg = load_config(config_path(root))
    metrics, _inventory = _runtime_snapshot(root, cfg)
    return metrics


def _runtime_key(root: Path, cfg: Config) -> _RuntimeKey:
    from hashlib import sha256

    return root.resolve(), sha256(cfg.model_dump_json().encode()).hexdigest()


def _unavailable_inventory(cfg: Config) -> ContainerInventory:
    configured = cfg.enabled_nodes if cfg.proxmox.provision_machines else [cfg.control_node]
    return ContainerInventory(
        is_available=False,
        unavailable_nodes=[as_node_name(node) for node in configured],
        containers=[],
    )


def _build_runtime_snapshot(root: Path, cfg: Config) -> tuple[DashboardMetrics, ContainerInventory]:
    return read_dashboard_metrics(root, cfg), read_container_inventory(root)


def _complete_runtime_refresh(
    key: _RuntimeKey,
    cfg: Config,
    future: Future[tuple[DashboardMetrics, ContainerInventory]],
) -> None:
    failed = False
    try:
        metrics, inventory = future.result()
    except Exception:
        failed = True
        metrics, inventory = _empty_metrics(), _unavailable_inventory(cfg)
        logger.warning("Dashboard runtime refresh failed", exc_info=True)
    with _runtime_cache_lock:
        if _runtime_inflight.get(key) is not future:
            return
        _runtime_inflight.pop(key, None)
        _runtime_cache[key] = (time.monotonic(), metrics, inventory, failed)
        while len(_runtime_cache) > _RUNTIME_CACHE_LIMIT:
            oldest = min(_runtime_cache, key=lambda item: _runtime_cache[item][0])
            del _runtime_cache[oldest]


def _runtime_snapshot(root: Path, cfg: Config) -> tuple[DashboardMetrics, ContainerInventory]:
    root = root.resolve()
    key = _runtime_key(root, cfg)
    now = time.monotonic()
    new_future: Future[tuple[DashboardMetrics, ContainerInventory]] | None = None
    with _runtime_cache_lock:
        cached = _runtime_cache.get(key)
        future = _runtime_inflight.get(key)
        cache_ttl = _RUNTIME_ERROR_TTL_SECONDS if cached and cached[3] else _RUNTIME_CACHE_TTL_SECONDS
        if cached is not None and now - cached[0] <= cache_ttl and future is None:
            return cached[1], cached[2]
        if future is None and len(_runtime_inflight) < _RUNTIME_INFLIGHT_LIMIT:
            new_future = _runtime_refresh_executor.submit(_build_runtime_snapshot, root, cfg)
            _runtime_inflight[key] = new_future
            future = new_future
    if new_future is not None:
        new_future.add_done_callback(lambda completed: _complete_runtime_refresh(key, cfg, completed))
    if cached is not None:
        return cached[1], cached[2]
    if future is not None:
        try:
            return future.result(timeout=_RUNTIME_COLD_WAIT_SECONDS)
        except Exception:
            if future.done():
                return _empty_metrics(), _unavailable_inventory(cfg)
    return _empty_metrics(), _unavailable_inventory(cfg)


def read_last_verify_summary(root: Path) -> dict[str, VerifyNodeSummary] | None:
    path = root / ".homelab-state" / "last-verify.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        content = os.read(descriptor, _MAX_VERIFY_OUTPUT + 1)
    finally:
        os.close(descriptor)
    if len(content) > _MAX_VERIFY_OUTPUT:
        return None
    try:
        raw = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    result: dict[str, VerifyNodeSummary] = {}
    for node, value in raw.items():
        if not isinstance(node, str) or not isinstance(value, dict):
            continue
        try:
            from toolkit.core.machines.models import validate_machine_id

            validate_machine_id(node)
        except ValueError:
            continue
        try:
            result[node] = VerifyNodeSummary.model_validate(
                {
                    "ok": value.get("ok"),
                    "healthy": value.get("healthy"),
                    "unhealthy": value.get("unhealthy"),
                    "pending": value.get("pending"),
                }
            )
        except ValidationError:
            continue
    return result or None


def _next_action(cfg) -> DashboardAction | None:
    steps = get_all_manual_guidance(cfg)
    if not steps:
        return None
    step = min(steps, key=lambda item: _MANUAL_STEP_PRIORITY.get(item.category, 99))
    return DashboardAction(title=step.title, category=step.category, service=step.service)


def _alerts(
    cfg,
    root: Path,
    metrics: DashboardMetrics,
    inventory,
    *,
    metrics_service_href: str,
    metrics_dashboard_href: str,
) -> list[DashboardAlert]:
    alerts: list[DashboardAlert] = []
    completed_oneshots = _completed_oneshots(cfg, inventory)
    for container in inventory.containers:
        if container.health == "unhealthy":
            alerts.append(
                DashboardAlert(
                    severity="critical",
                    message=f"Container unhealthy on {container.node}: {container.name}",
                    href="/services",
                )
            )
        elif container.state == "exited" and container.name not in completed_oneshots:
            alerts.append(
                DashboardAlert(
                    severity="warning",
                    message=f"Container exited on {container.node}: {container.name}",
                    href="/services",
                )
            )
    for node in inventory.unavailable_nodes:
        alerts.append(
            DashboardAlert(
                severity="warning",
                message=f"Container status unavailable for node: {node}",
                href="/deploy",
            )
        )
    # Generated env files are role-scoped artifacts.  The controller can
    # authoritatively inspect only its own control-node artifact; remote role
    # files are generated and consumed on their respective guests.
    node = as_node_name(cfg.control_node)
    if not env_path(node, root).exists():
        alerts.append(
            DashboardAlert(
                severity="warning",
                message=f"No generated environment for node: {node}",
                href="/deploy",
            )
        )
    for name, value, warning, critical in (
        ("Disk", metrics.disk, 80, 90),
        ("Memory", metrics.memory, 85, 90),
    ):
        if value is not None and value > critical:
            alerts.append(
                DashboardAlert(
                    severity="critical",
                    message=f"{name} usage critical: {value:.0f}%",
                    href=metrics_dashboard_href,
                )
            )
        elif value is not None and value > warning:
            alerts.append(
                DashboardAlert(
                    severity="warning",
                    message=f"{name} usage high: {value:.0f}%",
                    href=metrics_dashboard_href,
                )
            )
    if metrics.cpu is None and metrics.memory is None and metrics.disk is None:
        alerts.append(DashboardAlert(severity="info", message="Metrics are unavailable", href=metrics_service_href))
    return alerts[:512]


def _metrics_hrefs() -> tuple[str, str]:
    from toolkit.core.manifest.catalog import load_service_catalog

    catalog = load_service_catalog()
    collector = catalog.provider("metrics")
    dashboard = catalog.provider("metrics-dashboard")
    collector_href = f"/services/{collector.name}" if collector is not None else "/services"
    dashboard_href = f"/services/{dashboard.name}" if dashboard is not None else collector_href
    return collector_href, dashboard_href


def _completed_oneshots(_cfg: Config, inventory: ContainerInventory) -> set[str]:
    """Return successful manifest-declared one-shots classified by inventory."""
    return {container.name for container in inventory.containers if container.completed}


def _runtime_summary(cfg: Config, inventory: ContainerInventory) -> DashboardRuntimeSummary:
    completed_oneshots = _completed_oneshots(cfg, inventory)
    operational = [container for container in inventory.containers if container.name not in completed_oneshots]
    return DashboardRuntimeSummary(
        total=len(operational),
        running=sum(container.state == "running" for container in operational),
        healthy=sum(container.health == "healthy" for container in operational),
        unhealthy=sum(container.health == "unhealthy" for container in operational),
        exited=sum(container.state == "exited" for container in operational),
        unavailable_nodes=len(inventory.unavailable_nodes),
    )


def _portal_container_status(state: str, health: str) -> Literal["online", "degraded", "offline", "unknown"]:
    if state == "running":
        if health in {"healthy", "none"}:
            return "online"
        if health in {"starting", "unhealthy"}:
            return "degraded"
    if state in {"exited", "dead"}:
        return "offline"
    if state in {"created", "paused", "restarting", "removing"}:
        return "degraded"
    return "unknown"


def read_portal_status(root: Path) -> PortalStatus:
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import compile_routes

    root = root.resolve()
    cfg = load_config(config_path(root))
    portal_services = {
        route.compose_service or route.service
        for route in compile_routes(cfg, load_service_catalog())
        if route.match is None and not route.file_server_root and not route.service.startswith("project-")
    }
    _metrics, inventory = _runtime_snapshot(root, cfg)
    return PortalStatus(
        checked_at=datetime.now(UTC),
        complete=inventory.is_available and not inventory.unavailable_nodes,
        unavailable_nodes=len(inventory.unavailable_nodes),
        services={
            container.name: _portal_container_status(container.state, container.health)
            for container in inventory.containers
            if not container.completed and container.name in portal_services
        },
    )


def _job_summary(jobs: list[JobRecord]) -> tuple[list[DashboardJobView], int, int]:
    recent = [
        DashboardJobView(
            job_id=job.job_id,
            kind=job.request.kind,
            state=job.state,
            created_at=job.created_at,
            updated_at=job.updated_at,
            can_cancel=job_can_cancel(job.request.kind, job.state),
            error_code=job.error.code if job.error else "",
        )
        for job in jobs[:10]
    ]
    active_states = {JobState.QUEUED, JobState.RUNNING, JobState.CANCEL_REQUESTED}
    attention_states = {JobState.PARTIAL_FAILURE, JobState.FAILED, JobState.CANCELLED}
    attention_cutoff = datetime.now(UTC) - timedelta(hours=24)
    latest_by_kind: dict[JobKind, JobRecord] = {}
    for job in jobs:
        latest_by_kind.setdefault(job.request.kind, job)
    return (
        recent,
        sum(job.state in active_states for job in jobs),
        sum(
            job.state in attention_states and job.updated_at >= attention_cutoff
            for job in latest_by_kind.values()
        ),
    )


def _operations_summary(root: Path) -> DashboardOperationsSummary:
    view = read_operations_view(root)
    return DashboardOperationsSummary(
        maintenance_enabled=view.maintenance.enabled,
        maintenance_ok=view.maintenance.ok,
        maintenance_last_run_at=view.maintenance.last_run_at,
        backups_enabled=view.backups.enabled,
        backup_target=view.backups.target,
        backup_drill_ok=view.backups.drill.ok,
        backup_drill_last_run_at=view.backups.drill.last_run_at,
        restore_points=len(view.dumps),
        managed_hosts=len(view.hosts.hosts),
        pending_hosts=sum(not host.reconciled for host in view.hosts.hosts),
        updates_available=view.updates.available,
    )


def _operations_alerts(
    operations: DashboardOperationsSummary,
    jobs: list[DashboardJobView],
) -> list[DashboardAlert]:
    alerts: list[DashboardAlert] = []
    if operations.maintenance_enabled:
        if operations.maintenance_ok is False:
            alerts.append(
                DashboardAlert(severity="critical", message="The last maintenance run failed", href="/operations")
            )
        elif operations.maintenance_last_run_at is None:
            alerts.append(DashboardAlert(severity="info", message="Maintenance has not run yet", href="/operations"))
        elif datetime.now(UTC) - operations.maintenance_last_run_at > timedelta(hours=36):
            alerts.append(DashboardAlert(severity="warning", message="Maintenance is overdue", href="/operations"))
    if not operations.backups_enabled:
        alerts.append(DashboardAlert(severity="warning", message="Backups are disabled", href="/operations"))
    elif operations.restore_points == 0:
        alerts.append(
            DashboardAlert(severity="warning", message="No database restore point is available", href="/operations")
        )
    if operations.backups_enabled:
        if operations.backup_drill_ok is False:
            alerts.append(
                DashboardAlert(
                    severity="critical",
                    message="The latest backup content drill failed",
                    href="/operations",
                )
            )
        elif operations.backup_drill_last_run_at is None:
            alerts.append(
                DashboardAlert(
                    severity="info",
                    message="Backup content has not been restore-verified",
                    href="/operations",
                )
            )
        elif datetime.now(UTC) - operations.backup_drill_last_run_at > timedelta(days=8):
            alerts.append(
                DashboardAlert(
                    severity="critical",
                    message="Backup content verification is overdue",
                    href="/operations",
                )
            )
    if operations.pending_hosts:
        alerts.append(
            DashboardAlert(
                severity="warning",
                message=f"{operations.pending_hosts} managed host(s) are pending reconciliation",
                href="/operations",
            )
        )
    failure_cutoff = datetime.now(UTC) - timedelta(hours=24)
    latest_by_kind: dict[JobKind, DashboardJobView] = {}
    for job in jobs:
        latest_by_kind.setdefault(job.kind, job)
    failed = next(
        (
            job
            for job in latest_by_kind.values()
            if job.state in {JobState.FAILED, JobState.PARTIAL_FAILURE} and job.updated_at >= failure_cutoff
        ),
        None,
    )
    if failed is not None:
        alerts.append(
            DashboardAlert(
                severity="critical" if failed.state is JobState.FAILED else "warning",
                message=f"Recent {failed.kind.value.lower().replace('_', ' ')} job needs attention",
                href=f"/jobs/{failed.job_id}",
            )
        )
    return alerts


def _health(alerts: list[DashboardAlert]) -> Literal["healthy", "attention", "critical", "unknown"]:
    severities = {alert.severity for alert in alerts}
    if "critical" in severities:
        return "critical"
    if "warning" in severities:
        return "attention"
    if "info" in severities:
        return "unknown"
    return "healthy"


def read_dashboard_view(
    root: Path,
    *,
    family: bool,
    groups: list[str],
    jobs: list[JobRecord] | None = None,
) -> DashboardView:
    root = root.resolve()
    state = get_state(root)
    if state is ToolkitState.UNINITIALIZED:
        return DashboardView(
            state="uninitialized",
            enabled_nodes=[],
            categories=[],
            metrics=_empty_metrics(),
            alerts=[],
            bookmark_groups=[],
            tier_labels=[],
            health="setup",
        )

    cfg = load_config(config_path(root))
    if family:
        try:
            selected_groups = validate_service_groups(groups)
        except ValueError as exc:
            raise InventoryRequestError("invalid service access group") from exc
        return DashboardView(
            state=_state_value(state),
            domain=cfg.domain,
            enabled_nodes=[as_node_name(node) for node in cfg.enabled_nodes],
            categories=[],
            metrics=_empty_metrics(),
            alerts=[],
            bookmark_groups=serialize_bookmarks(family_portal_groups(cfg, selected_groups)),
            tier_labels=tier_labels_for_groups(selected_groups),
        )

    load_all()
    metrics_service_href, metrics_dashboard_href = _metrics_hrefs()
    categories = [
        DashboardCategory(
            name=category.label,
            node=as_node_name(category.runtime_node(cfg)),
            services=[service.name for service in category.services(cfg)],
        )
        for category in enabled_categories(cfg)
    ]
    recent_jobs, active_jobs, attention_jobs = _job_summary(jobs or [])
    operations = _operations_summary(root)
    if state is ToolkitState.CONFIG_ONLY:
        return DashboardView(
            state="config_only",
            domain=cfg.domain,
            enabled_nodes=[as_node_name(node) for node in cfg.enabled_nodes],
            categories=categories,
            total_services=sum(len(category.services) for category in categories),
            metrics=_empty_metrics(),
            alerts=[],
            last_verify=read_last_verify_summary(root),
            next_action=_next_action(cfg),
            bookmark_groups=serialize_bookmarks(portal_bookmark_groups(cfg)),
            tier_labels=[],
            health="setup",
            operations=operations,
            recent_jobs=recent_jobs,
            active_jobs=active_jobs,
            attention_jobs=attention_jobs,
            metrics_service_href=metrics_service_href,
            metrics_dashboard_href=metrics_dashboard_href,
        )
    metrics, inventory = _runtime_snapshot(root, cfg)
    alerts = _alerts(
        cfg,
        root,
        metrics,
        inventory,
        metrics_service_href=metrics_service_href,
        metrics_dashboard_href=metrics_dashboard_href,
    )
    alerts.extend(_operations_alerts(operations, recent_jobs))
    alerts = alerts[:512]
    return DashboardView(
        state=_state_value(state),
        domain=cfg.domain,
        enabled_nodes=[as_node_name(node) for node in cfg.enabled_nodes],
        categories=categories,
        total_services=sum(len(category.services) for category in categories),
        metrics=metrics,
        alerts=alerts,
        last_verify=read_last_verify_summary(root),
        next_action=_next_action(cfg),
        bookmark_groups=serialize_bookmarks(portal_bookmark_groups(cfg)),
        tier_labels=[],
        health=_health(alerts),
        runtime=_runtime_summary(cfg, inventory),
        operations=operations,
        recent_jobs=recent_jobs,
        active_jobs=active_jobs,
        attention_jobs=attention_jobs,
        metrics_service_href=metrics_service_href,
        metrics_dashboard_href=metrics_dashboard_href,
    )
