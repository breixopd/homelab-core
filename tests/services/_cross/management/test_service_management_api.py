from __future__ import annotations

from pathlib import Path

import pytest
from toolkit.controller.desired_state_api import DesiredStateConflictError
from toolkit.controller.read_models import ServiceSettingsUpdate
from toolkit.controller.service_management_api import (
    ServiceManagementNotFoundError,
    ServiceSettingValidationError,
    read_service_management,
    update_service_settings,
)
from toolkit.core.config.config import Config, load_config, save_config
from toolkit.core.config.storage import config_path
from toolkit.services import get_service_plugin


@pytest.fixture(autouse=True)
def _disable_live_service_history(monkeypatch) -> None:
    monkeypatch.setattr(
        "toolkit.controller.service_management_api.read_service_metric_history",
        lambda *_args, **_kwargs: {},
    )


def _root(tmp_path: Path, *, service_settings: dict | None = None) -> Path:
    save_config(Config(domain="example.com", service_settings=service_settings or {}), config_path(tmp_path))
    return tmp_path


def test_service_management_resolves_only_declared_settings_and_metrics(tmp_path: Path, monkeypatch) -> None:
    plugin = get_service_plugin("music-sync")
    assert plugin is not None
    monkeypatch.setattr(
        plugin,
        "status",
        lambda _cfg, _secrets, _root: {
            "tracks": 431,
            "playlists": 7,
            "heartbeat_age_seconds": 32.5,
            "private_token": "must-not-cross-controller-boundary",
        },
    )
    monkeypatch.setattr("toolkit.controller.service_management_api.read_service_metrics", lambda *_args, **_kwargs: {})

    view = read_service_management(_root(tmp_path), "music-sync")

    assert view.service == "music-sync"
    assert view.enabled is True
    assert view.status_available is True
    assert len(view.revision) == 64
    settings = {setting.key: setting.value for setting in view.settings}
    assert settings["enabled"] is True
    assert settings["interval-minutes"] == 60
    custom_metrics = {
        metric.key: metric.value
        for metric in view.metrics
        if metric.key in {"tracks", "playlists", "heartbeat_age_seconds"}
    }
    assert custom_metrics == {
        "tracks": 431,
        "playlists": 7,
        "heartbeat_age_seconds": 32.5,
    }
    assert "private_token" not in view.model_dump_json()
    assert [action.id for action in view.actions] == ["sync-now"]


def test_every_plugin_receives_bounded_container_metrics(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "toolkit.controller.service_management_api.read_service_metrics",
        lambda _root, _cfg, container, **_kwargs: (
            {
                "cpu_percent": 12.3456,
                "memory_megabytes": 384.5,
                "available_percent": 100.0,
                "restart_attempts": 2.0,
                "private": 999,
            }
            if container == "flaresolverr"
            else {}
        ),
    )

    view = read_service_management(_root(tmp_path), "flaresolverr")

    assert view.settings == []
    assert view.actions == []
    assert view.resources == []
    assert {metric.key: metric.value for metric in view.metrics} == {
        "container_cpu_percent": 12.35,
        "container_memory_megabytes": 384.5,
        "container_available_percent": 100.0,
        "container_restart_attempts": 2.0,
        "container_network_receive_mbps": None,
        "container_network_transmit_mbps": None,
        "container_disk_read_mbps": None,
        "container_disk_write_mbps": None,
        "container_uptime_seconds": None,
    }
    assert "private" not in view.model_dump_json()
    assert view.status_available is True


def test_service_management_exposes_bounded_history_summaries(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "toolkit.controller.service_management_api.read_service_metrics",
        lambda *_args, **_kwargs: {"cpu_percent": 10.0, "memory_megabytes": 256.0},
    )
    monkeypatch.setattr(
        "toolkit.controller.service_management_api.read_service_metric_history",
        lambda *_args, **_kwargs: {
            "cpu_percent": [(1_000, 10.0), (2_000, 20.0)],
            "memory_megabytes": [(1_000, 250.0), (2_000, 270.0)],
            "private": [(1_000, 999.0)],
        },
    )

    view = read_service_management(_root(tmp_path), "flaresolverr")

    series = {item.key: item for item in view.metric_series}
    assert series["cpu_percent"].average == 15.0
    assert series["cpu_percent"].peak == 20.0
    assert series["memory_megabytes"].points == [(1_000, 250.0), (2_000, 270.0)]
    assert "private" not in view.model_dump_json()


def test_service_panels_resolve_bounded_public_placeholders(tmp_path: Path, monkeypatch) -> None:
    save_config(Config(domain="example.com", email="brei@example.com"), config_path(tmp_path))
    monkeypatch.setattr("toolkit.controller.service_management_api.read_service_metrics", lambda *_args, **_kwargs: {})

    view = read_service_management(tmp_path, "mailserver", collect_status=False)

    setup = next(panel for panel in view.panels if panel.id == "client-setup")
    values = {item.label: item.value for item in setup.items}
    assert values["Email address"] == "brei@example.com"
    assert values["Incoming server"] == "mail.example.com"
    assert values["IMAP security"] == "SSL/TLS on port 993"


def test_service_secret_fields_expose_only_configuration_state(tmp_path: Path, monkeypatch) -> None:
    root = _root(tmp_path)
    from toolkit.core.config.storage import secrets_path

    secrets_path(root).write_text("placeholder", encoding="utf-8")
    canary = "must-never-cross-the-controller-boundary"
    monkeypatch.setattr(
        "toolkit.controller.service_management_api.load_secrets_plaintext",
        lambda _path: {"IGDB_CLIENT_ID": canary},
    )
    monkeypatch.setattr("toolkit.controller.service_management_api.read_service_metrics", lambda *_args, **_kwargs: {})

    view = read_service_management(root, "romm")

    fields = {field.name: field.is_configured for field in view.secrets}
    assert fields["IGDB_CLIENT_ID"] is True
    assert fields["IGDB_CLIENT_SECRET"] is False
    assert canary not in view.model_dump_json()


def test_declared_prometheus_metric_is_selected_by_manifest_id(tmp_path: Path, monkeypatch) -> None:
    from toolkit.core.manifest.schema import ServiceMetric

    plugin = get_service_plugin("flaresolverr")
    assert plugin is not None
    management = plugin.management().model_copy(
        update={
            "metrics": [
                ServiceMetric(
                    key="challenge_rate",
                    label="Challenge rate",
                    source="prometheus",
                    query="sum(rate(flaresolverr_challenges_total[5m]))",
                    unit="count",
                    precision=2,
                )
            ]
        }
    )
    monkeypatch.setattr(plugin, "management", lambda: management)
    monkeypatch.setattr(
        "toolkit.controller.service_management_api.read_service_metrics",
        lambda _root, _cfg, _container, **kwargs: (
            {
                "cpu_percent": 1.0,
                "challenge_rate": 4.25,
                "undeclared": 99,
            }
            if kwargs["manifest_queries"] == {"challenge_rate": "sum(rate(flaresolverr_challenges_total[5m]))"}
            else {}
        ),
    )

    view = read_service_management(_root(tmp_path), "flaresolverr")

    assert next(metric for metric in view.metrics if metric.key == "challenge_rate").value == 4.25
    assert "undeclared" not in view.model_dump_json()


def test_restart_counter_alone_does_not_mark_container_status_available(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "toolkit.controller.service_management_api.read_service_metrics",
        lambda *_args, **_kwargs: {"restart_attempts": 0.0},
    )

    view = read_service_management(_root(tmp_path), "flaresolverr")

    assert view.status_available is False


def test_disabled_service_skips_status_collection_but_remains_configurable(tmp_path: Path, monkeypatch) -> None:
    plugin = get_service_plugin("media-cache")
    assert plugin is not None

    def unexpected_status(*_args):
        raise AssertionError("disabled services must not be queried")

    monkeypatch.setattr(plugin, "status", unexpected_status)
    view = read_service_management(
        _root(tmp_path, service_settings={"media-cache": {"enabled": False}}),
        "media-cache",
    )

    assert view.enabled is False
    assert view.status_available is False
    assert all(metric.value is None for metric in view.metrics)
    assert next(setting for setting in view.settings if setting.key == "enabled").value is False


def test_service_management_rejects_unknown_services(tmp_path: Path) -> None:
    root = _root(tmp_path)

    with pytest.raises(ServiceManagementNotFoundError):
        read_service_management(root, "not-managed")


def test_music_sync_status_extracts_only_bounded_declared_candidates(tmp_path: Path, monkeypatch) -> None:
    plugin = get_service_plugin("music-sync")
    assert plugin is not None
    monkeypatch.setattr(
        "toolkit.services.sdk.docker_curl",
        lambda *_args, **_kwargs: (
            0,
            '{"tracks": 12, "playlists": 2, "heartbeat_age_seconds": 9.5, "token": "private"}',
        ),
    )

    status = plugin.status(Config(domain="example.com"), {"MUSIC_SYNC_WEB_PASSWORD": "secret"}, tmp_path)

    assert status == {"tracks": 12, "playlists": 2, "heartbeat_age_seconds": 9.5}


def test_media_cache_status_flattens_safe_bandwidth_metrics(tmp_path: Path, monkeypatch) -> None:
    plugin = get_service_plugin("media-cache")
    assert plugin is not None
    monkeypatch.setattr(
        "toolkit.services.sdk.docker_curl",
        lambda *_args, **_kwargs: (
            0,
            '{"cache_used_pct": 37.5, "tracked_files": 88, "active_prefetch": 1, '
            '"bandwidth": {"effective_uplink_mbps": 280.5}, "admin_token": "private"}',
        ),
    )

    status = plugin.status(Config(domain="example.com"), {}, tmp_path)

    assert status == {
        "cache_used_pct": 37.5,
        "tracked_files": 88,
        "active_prefetch": 1,
        "effective_uplink_mbps": 280.5,
    }


def test_service_resources_are_allowlisted_bounded_and_typed(tmp_path: Path, monkeypatch) -> None:
    plugin = get_service_plugin("media-cache")
    assert plugin is not None
    monkeypatch.setattr(
        plugin,
        "resources",
        lambda _cfg, _secrets, _root: {
            "storage_backends": [
                {"name": "media-union", "kind": "Union pool", "credential": "must-not-cross"},
                {"name": "ext-nas", "kind": "Fleet storage"},
                {"name": "bad\x00name", "kind": "Storage"},
            ],
            "undeclared": [{"name": "private"}],
        },
    )
    monkeypatch.setattr(plugin, "status", lambda *_args: {})
    monkeypatch.setattr("toolkit.controller.service_management_api.read_service_metrics", lambda *_a, **_k: {})

    view = read_service_management(
        _root(tmp_path, service_settings={"media-cache": {"enabled": True}}),
        "media-cache",
    )

    assert len(view.resources) == 1
    resource = view.resources[0]
    assert resource.key == "storage_backends"
    assert resource.available is True
    assert resource.rows == [
        {"name": "media-union", "kind": "Union pool"},
        {"name": "ext-nas", "kind": "Fleet storage"},
        {"name": "badname", "kind": "Storage"},
    ]
    assert "credential" not in view.model_dump_json()
    assert "must-not-cross" not in view.model_dump_json()


def test_service_management_degrades_when_secret_loading_fails(tmp_path: Path, monkeypatch) -> None:
    root = _root(tmp_path)
    from toolkit.core.config.storage import secrets_path

    secrets_path(root).write_text("unreadable", encoding="utf-8")
    monkeypatch.setattr(
        "toolkit.controller.service_management_api.load_secrets_plaintext",
        lambda _path: (_ for _ in ()).throw(ValueError("secret canary")),
    )
    monkeypatch.setattr("toolkit.controller.service_management_api.read_service_metrics", lambda *_a, **_k: {})

    view = read_service_management(root, "media-cache")

    assert view.status_available is False
    assert view.resources[0].available is False


def test_media_cache_resources_extract_backend_names(tmp_path: Path, monkeypatch) -> None:
    plugin = get_service_plugin("media-cache")
    assert plugin is not None
    monkeypatch.setattr(
        "toolkit.services.sdk.docker_curl",
        lambda *_args, **_kwargs: (0, '{"backends": ["media-union", "ext-nas", 5]}'),
    )

    resources = plugin.resources(Config(domain="example.com"), {}, tmp_path)

    assert resources == {
        "storage_backends": [
            {"name": "media-union", "kind": "Union pool"},
            {"name": "ext-nas", "kind": "Fleet storage"},
        ]
    }


def test_media_cache_resources_reject_an_unavailable_backend_api(tmp_path: Path, monkeypatch) -> None:
    plugin = get_service_plugin("media-cache")
    assert plugin is not None
    monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_args, **_kwargs: (1, ""))

    with pytest.raises(RuntimeError, match="unavailable"):
        plugin.resources(Config(domain="example.com"), {}, tmp_path)


def test_service_settings_update_is_revisioned_validated_and_partial(tmp_path: Path) -> None:
    root = _root(tmp_path)
    current = read_service_management(root, "media-cache", collect_status=False)

    updated = update_service_settings(
        root,
        "media-cache",
        ServiceSettingsUpdate(
            expected_revision=current.revision,
            values={"cold-after-days": 45, "uplink-mbps": 500},
        ),
    )

    cfg = load_config(config_path(root))
    assert cfg.service_settings["media-cache"] == {"cold-after-days": 45, "uplink-mbps": 500}
    assert updated.revision != current.revision


@pytest.mark.parametrize(
    "values",
    [
        {"not-declared": 1},
        {"cold-after-days": 0},
        {"cold-after-days": True},
        {"enabled": "yes"},
    ],
)
def test_service_settings_update_rejects_unknown_or_invalid_values(tmp_path: Path, values: dict) -> None:
    root = _root(tmp_path)
    current = read_service_management(root, "media-cache", collect_status=False)

    with pytest.raises(ServiceSettingValidationError):
        update_service_settings(
            root,
            "media-cache",
            ServiceSettingsUpdate(expected_revision=current.revision, values=values),
        )


def test_service_settings_update_rejects_stale_revision(tmp_path: Path) -> None:
    root = _root(tmp_path)

    with pytest.raises(DesiredStateConflictError):
        update_service_settings(
            root,
            "music-sync",
            ServiceSettingsUpdate(expected_revision="0" * 64, values={"interval-minutes": 30}),
        )
