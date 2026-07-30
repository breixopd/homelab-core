"""Unit tests for the embedded Servarr integration plugin."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig


def _plugin():
    return load_plugin("servarr").ServarrPlugin()


def _patch_core(monkeypatch, *, wait: bool = True, notifications: bool = True):
    monkeypatch.setattr("toolkit.services.seerr.bootstrap.extract_seerr_api_key", lambda _root: "")
    monkeypatch.setattr(
        "toolkit.services._arr.reconcile_servarr_api_key",
        lambda _root, _service, secrets, environment: secrets.get(environment, ""),
    )
    monkeypatch.setattr("toolkit.services._arr.wait_for_arr_api", lambda *_a, **_k: wait)
    monkeypatch.setattr("toolkit.services._arr.configure_arr_root_folder", lambda *_a, **_k: True)
    monkeypatch.setattr("toolkit.services._arr.configure_arr_download_client", lambda *_a, **_k: True)
    monkeypatch.setattr("toolkit.services._arr.extract_bazarr_api_key", lambda *_a, **_k: "bazarr")
    monkeypatch.setattr("toolkit.services._arr.wire_prowlarr_apps", lambda **_k: [])
    monkeypatch.setattr("toolkit.services._arr.wire_bazarr_arr", lambda *_a, **_k: [])
    monkeypatch.setattr("toolkit.services._arr.wire_bazarr_providers", lambda *_a, **_k: [])
    monkeypatch.setattr("toolkit.services._arr.wire_arr_notifications", lambda *_a, **_k: notifications)


def test_post_start_wires_notifications_and_reports_failures(tmp_path, monkeypatch):
    _patch_core(monkeypatch, notifications=False)
    cfg = Config(
        domain="test.local",
        services=ServicesConfig(management=True, media=True, notifications=True),
    )
    secrets = {
        "PROWLARR_API_KEY": "prowlarr",
        "SONARR_API_KEY": "sonarr",
        "RADARR_API_KEY": "radarr",
    }
    with (
        patch(
            "toolkit.core.ops.automation.resolve_docker_service_url",
            side_effect=lambda service, port: f"http://{service}:{port}",
        ),
        patch("toolkit.services.ntfy.client.resolve_infra_ntfy_url", return_value="http://ntfy:80"),
    ):
        logs = _plugin().post_start(cfg, secrets, root=tmp_path)

    assert "WARNING: Sonarr ntfy wiring failed" in logs
    assert "WARNING: Radarr ntfy wiring failed" in logs


def test_post_start_skips_notifications_when_category_is_disabled(tmp_path, monkeypatch):
    _patch_core(monkeypatch)
    cfg = Config(
        domain="test.local",
        services=ServicesConfig(management=True, media=True, notifications=False),
    )
    secrets = {
        "PROWLARR_API_KEY": "prowlarr",
        "SONARR_API_KEY": "sonarr",
        "RADARR_API_KEY": "radarr",
    }
    with (
        patch(
            "toolkit.core.ops.automation.resolve_docker_service_url",
            side_effect=lambda service, port: f"http://{service}:{port}",
        ),
        patch("toolkit.services._arr.wire_arr_notifications") as notify,
    ):
        logs = _plugin().post_start(cfg, secrets, root=tmp_path)

    notify.assert_not_called()
    assert not any("ntfy" in line for line in logs)


def test_post_start_fails_when_arr_api_never_becomes_ready(tmp_path, monkeypatch):
    _patch_core(monkeypatch, wait=False)
    cfg = Config(domain="test.local", services=ServicesConfig(management=True, media=True))
    secrets = {
        "PROWLARR_API_KEY": "prowlarr",
        "SONARR_API_KEY": "sonarr",
        "RADARR_API_KEY": "radarr",
    }
    with patch(
        "toolkit.core.ops.automation.resolve_docker_service_url",
        side_effect=lambda service, port: f"http://{service}:{port}",
    ):
        with pytest.raises(RuntimeError, match="API not ready after wait"):
            _plugin().post_start(cfg, secrets, root=tmp_path)


def test_post_start_fails_when_bazarr_api_key_is_unavailable(tmp_path, monkeypatch):
    _patch_core(monkeypatch)
    monkeypatch.setattr("toolkit.services._arr.extract_bazarr_api_key", lambda *_a, **_k: None)
    cfg = Config(domain="test.local", services=ServicesConfig(management=True, media=True))
    secrets = {
        "PROWLARR_API_KEY": "prowlarr",
        "SONARR_API_KEY": "sonarr",
        "RADARR_API_KEY": "radarr",
    }
    with (
        patch(
            "toolkit.core.ops.automation.resolve_docker_service_url",
            side_effect=lambda service, port: f"http://{service}:{port}",
        ),
        pytest.raises(RuntimeError, match="Bazarr API key is unavailable"),
    ):
        _plugin().post_start(cfg, secrets, root=tmp_path)


def test_post_start_propagates_cross_service_wiring_failure(tmp_path, monkeypatch):
    _patch_core(monkeypatch)
    monkeypatch.setattr(
        "toolkit.services._arr.wire_prowlarr_apps",
        lambda **_k: ["Prowlarr: failed to register Sonarr"],
    )
    cfg = Config(domain="test.local", services=ServicesConfig(management=True, media=True))
    secrets = {
        "PROWLARR_API_KEY": "prowlarr",
        "SONARR_API_KEY": "sonarr",
        "RADARR_API_KEY": "radarr",
    }
    with patch(
        "toolkit.core.ops.automation.resolve_docker_service_url",
        side_effect=lambda service, port: f"http://{service}:{port}",
    ):
        with pytest.raises(RuntimeError, match="Prowlarr auto-wire"):
            _plugin().post_start(cfg, secrets, root=tmp_path)
