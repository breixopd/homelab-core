"""Controller-owned projections for manifest-driven service management."""

from __future__ import annotations

import logging
import math
from pathlib import Path
from typing import Literal

from pydantic import ValidationError

from toolkit.controller.desired_state_api import DesiredStateConflictError
from toolkit.controller.prometheus_api import read_service_metric_history, read_service_metrics
from toolkit.controller.read_models import (
    ManagedServiceActionView,
    ManagedServiceInfoItemView,
    ManagedServiceInfoPanelView,
    ManagedServiceMetricSeriesView,
    ManagedServiceMetricView,
    ManagedServiceResourceColumnView,
    ManagedServiceResourceView,
    ManagedServiceSecretView,
    ManagedServiceSettingView,
    ServiceManagementView,
    ServiceSettingsUpdate,
)
from toolkit.controller.sanitization import sanitize_message
from toolkit.core.config.config import Config, load_config, save_config, save_local_config
from toolkit.core.config.mutations import config_revision, configuration_lock, configuration_mutation
from toolkit.core.config.storage import config_path, secrets_path
from toolkit.core.manifest.settings import (
    ServiceSettingError,
    service_setting_value,
    validate_service_setting_overrides,
    validate_setting_value,
)
from toolkit.core.secrets.secrets import load_secrets_plaintext
from toolkit.services import get_service_plugin

logger = logging.getLogger(__name__)

_MetricUnit = Literal["none", "count", "percent", "bytes", "megabytes", "seconds", "mbps"]

_CONTAINER_METRICS: tuple[tuple[str, str, _MetricUnit, int, str], ...] = (
    ("container_cpu_percent", "CPU", "percent", 2, "cpu_percent"),
    ("container_memory_megabytes", "Memory", "megabytes", 1, "memory_megabytes"),
    ("container_available_percent", "Available", "percent", 0, "available_percent"),
    ("container_restart_attempts", "Heal restarts", "count", 0, "restart_attempts"),
    ("container_network_receive_mbps", "Network in", "mbps", 3, "network_receive_mbps"),
    ("container_network_transmit_mbps", "Network out", "mbps", 3, "network_transmit_mbps"),
    ("container_disk_read_mbps", "Disk read", "mbps", 3, "disk_read_mbps"),
    ("container_disk_write_mbps", "Disk write", "mbps", 3, "disk_write_mbps"),
    ("container_uptime_seconds", "Uptime", "seconds", 0, "uptime_seconds"),
)

_SERIES: tuple[tuple[str, str, Literal["percent", "megabytes"], int], ...] = (
    ("cpu_percent", "CPU history", "percent", 2),
    ("memory_megabytes", "Memory history", "megabytes", 1),
)


def _panel_text(value: str, cfg: Config, plugin) -> str:
    email = (cfg.email or "").strip()
    username = email.split("@", 1)[0] if "@" in email else email
    route = next(iter(plugin.manifest.routes), None)
    url = f"https://{route.subdomain or plugin.service}.{cfg.domain}" if route is not None else ""
    address = plugin.runtime_address(cfg)
    return (
        value.replace("{domain}", cfg.domain)
        .replace("{email}", email)
        .replace("{username}", username)
        .replace("{url}", url)
        .replace("{address}", address)
        .replace("{node}", plugin.runtime_node(cfg))
    )


class ServiceManagementNotFoundError(RuntimeError):
    pass


class ServiceSettingValidationError(RuntimeError):
    pass


def _status_value(status: dict[str, object], path: str) -> object:
    value: object = status
    for part in path.split("."):
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value


def _numeric(value: object) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


def _metric_value(values: dict[str, float], key: str, precision: int) -> int | float | None:
    value = _numeric(values.get(key))
    if value is None or value < 0:
        return None
    return round(value, precision)


def _resource_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    if isinstance(value, int | float):
        if isinstance(value, float) and not math.isfinite(value):
            return ""
        return str(value)[:200]
    if not isinstance(value, str):
        return ""
    printable = "".join(character for character in value.strip() if ord(character) >= 32 and ord(character) != 127)
    return sanitize_message(printable)[:200]


def _resource_views(capabilities, candidates: object, *, available: bool) -> list[ManagedServiceResourceView]:
    candidate_map = candidates if isinstance(candidates, dict) else {}
    views: list[ManagedServiceResourceView] = []
    for resource in capabilities.resources:
        raw_rows = candidate_map.get(resource.key, [])
        rows: list[dict[str, str]] = []
        if isinstance(raw_rows, list):
            for raw_row in raw_rows[:100]:
                if not isinstance(raw_row, dict):
                    continue
                rows.append({column.key: _resource_text(raw_row.get(column.key)) for column in resource.columns})
        views.append(
            ManagedServiceResourceView(
                key=resource.key,
                label=resource.label,
                description=resource.description,
                available=available,
                columns=[
                    ManagedServiceResourceColumnView(key=column.key, label=column.label) for column in resource.columns
                ],
                rows=rows,
            )
        )
    return views


def read_service_management(
    root: Path,
    service: str,
    *,
    collect_status: bool = True,
) -> ServiceManagementView:
    root = root.resolve()
    plugin = get_service_plugin(service)
    if plugin is None:
        raise ServiceManagementNotFoundError("service is not managed")
    capabilities = plugin.management()

    with configuration_lock(root):
        cfg = load_config(config_path(root))
        revision = config_revision(root)

    enabled = plugin.is_enabled(cfg)
    manifest_queries = {metric.key: metric.query for metric in capabilities.metrics if metric.source == "prometheus"}
    container_metrics = (
        read_service_metrics(root, cfg, plugin.service, manifest_queries=manifest_queries)
        if collect_status and enabled
        else {}
    )
    container_history = read_service_metric_history(root, cfg, plugin.service) if collect_status and enabled else {}
    status_metrics = [metric for metric in capabilities.metrics if metric.source == "status"]
    status: dict[str, object] = {}
    status_available = False
    resource_candidates: dict[str, list[dict[str, object]]] = {}
    resources_available = False
    secrets: dict[str, str] = {}
    secrets_available = True
    needs_secrets = bool(capabilities.secrets) or (
        enabled and collect_status and bool(status_metrics or capabilities.resources)
    )
    if needs_secrets:
        try:
            secret_file = secrets_path(root)
            secrets = load_secrets_plaintext(secret_file) if secret_file.exists() else {}
        except Exception:
            logger.warning("Service secret loading failed for %s", plugin.service, exc_info=True)
            secrets_available = False
    if collect_status and enabled and status_metrics and secrets_available:
        try:
            status = plugin.status(cfg, secrets, root)
            status_available = bool(status)
        except Exception:
            logger.warning("Service status collection failed for %s", plugin.service, exc_info=True)
            status = {}
    if collect_status and enabled and capabilities.resources and secrets_available:
        try:
            resource_candidates = plugin.resources(cfg, secrets, root)
            resources_available = isinstance(resource_candidates, dict)
        except Exception:
            logger.warning("Service resource collection failed for %s", plugin.service, exc_info=True)
            resource_candidates = {}

    metadata = plugin._yaml_data
    supported_actions = plugin.supported_actions()
    return ServiceManagementView(
        revision=revision,
        service=plugin.service,
        label=str(metadata.get("label") or plugin.service.replace("-", " ").title()),
        description=str(metadata.get("description") or ""),
        category=plugin.category,
        node=plugin.runtime_node(cfg),
        enabled=enabled,
        status_available=status_available or container_metrics.get("available_percent") is not None,
        panels=[
            ManagedServiceInfoPanelView(
                id=panel.id,
                title=panel.title,
                description=panel.description,
                items=[
                    ManagedServiceInfoItemView(
                        label=item.label,
                        value=_panel_text(item.value, cfg, plugin),
                        copyable=item.copyable,
                        href=_panel_text(item.href, cfg, plugin),
                    )
                    for item in panel.items
                ],
            )
            for panel in capabilities.panels
        ],
        secrets=[
            ManagedServiceSecretView(
                name=field.name,
                label=field.label,
                description=field.description,
                is_configured=bool(secrets.get(field.name)),
            )
            for field in capabilities.secrets
        ],
        settings=[
            ManagedServiceSettingView(
                key=setting.key,
                label=setting.label,
                description=setting.description,
                type=setting.type,
                value=service_setting_value(cfg, plugin.manifest, setting.key),
                default=setting.default,
                minimum=setting.minimum,
                maximum=setting.maximum,
                step=setting.step,
                choices=list(setting.choices),
                requires_redeploy=setting.requires_redeploy,
            )
            for setting in capabilities.settings
        ],
        actions=[
            ManagedServiceActionView(
                id=action.id,
                label=action.label,
                description=action.description,
                confirmation=action.confirmation,
                is_dangerous=action.is_dangerous,
                can_run=enabled and action.id in supported_actions,
            )
            for action in capabilities.actions
        ],
        metrics=(
            [
                ManagedServiceMetricView(
                    key=key,
                    label=label,
                    unit=unit,
                    precision=precision,
                    value=_metric_value(container_metrics, source, precision),
                )
                for key, label, unit, precision, source in _CONTAINER_METRICS
            ]
            + [
                ManagedServiceMetricView(
                    key=metric.key,
                    label=metric.label,
                    unit=metric.unit,
                    precision=metric.precision,
                    value=(
                        _numeric(_status_value(status, metric.field))
                        if metric.source == "status"
                        else _metric_value(container_metrics, metric.key, metric.precision)
                    ),
                )
                for metric in capabilities.metrics
            ]
        ),
        metric_series=[
            ManagedServiceMetricSeriesView(
                key=key,
                label=label,
                unit=unit,
                average=round(sum(value for _, value in points) / len(points), precision) if points else None,
                peak=round(max(value for _, value in points), precision) if points else None,
                points=points,
            )
            for key, label, unit, precision in _SERIES
            if (points := container_history.get(key, []))
        ],
        resources=_resource_views(
            capabilities,
            resource_candidates,
            available=resources_available,
        ),
    )


def update_service_settings(
    root: Path,
    service: str,
    update: ServiceSettingsUpdate,
) -> ServiceManagementView:
    root = root.resolve()
    plugin = get_service_plugin(service)
    if plugin is None:
        raise ServiceManagementNotFoundError("service is not managed")
    capabilities = plugin.management()
    settings = {setting.key: setting for setting in capabilities.settings}
    if not settings:
        raise ServiceManagementNotFoundError("service has no configurable settings")
    unknown = set(update.values) - set(settings)
    if unknown:
        raise ServiceSettingValidationError("setting is not declared by this service")

    with configuration_mutation(root, f"service-settings:{service}"):
        if config_revision(root) != update.expected_revision:
            raise DesiredStateConflictError("configuration changed")
        current = load_config(config_path(root))
        overrides = {owner: dict(values) for owner, values in current.service_settings.items()}
        service_overrides = overrides.setdefault(service, {})
        try:
            for key, raw_value in update.values.items():
                service_overrides[key] = validate_setting_value(settings[key], raw_value)
            validated = Config.model_validate(
                current.model_copy(update={"service_settings": overrides}).model_dump(mode="python")
            )
            validate_service_setting_overrides(validated)
        except (ServiceSettingError, ValidationError) as exc:
            raise ServiceSettingValidationError("service setting violates configuration constraints") from exc
        save_config(validated, config_path(root))
        save_local_config(validated, root)
    return read_service_management(root, service, collect_status=False)
