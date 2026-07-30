"""Unit tests for music-sync plugin post_start() and verify() dispatch."""

from __future__ import annotations

import json
from unittest.mock import patch

from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.ops.hook_verify import VerifyCheck, verify_hooks


def _music_plugin():
    from toolkit.services import get_service_plugin

    plugin = get_service_plugin("music-sync")
    assert plugin is not None
    return plugin


class TestMusicSyncPostStart:
    def test_triggers_initial_sync(self, tmp_path):
        cfg = Config(domain="test.local", services=ServicesConfig(media=True))
        secrets = {"MUSIC_SYNC_WEB_USERNAME": "admin", "MUSIC_SYNC_WEB_PASSWORD": "pw"}

        with patch("toolkit.services.sdk.docker_curl", return_value=(0, "ok")) as curl_mock:
            logs = _music_plugin().post_start(cfg, secrets, root=tmp_path)

        assert "  music-sync: triggered initial sync" in logs
        assert curl_mock.call_count == 2
        args, kwargs = curl_mock.call_args
        assert args[2] == "music-sync"
        assert args[3].endswith("/api/sync")
        assert kwargs["method"] == "POST"

    def test_skipped_when_disabled(self, tmp_path):
        cfg = Config(
            domain="test.local",
            services=ServicesConfig(media=True),
            service_settings={"music-sync": {"enabled": False}},
        )
        with patch("toolkit.services.sdk.docker_curl") as curl_mock:
            logs = _music_plugin().post_start(cfg, {}, root=tmp_path)
        curl_mock.assert_not_called()
        assert logs == []

    def test_warns_when_unhealthy(self, tmp_path):
        cfg = Config(domain="test.local", services=ServicesConfig(media=True))
        with patch("toolkit.services.sdk.docker_curl", return_value=(1, "")):
            logs = _music_plugin().post_start(cfg, {}, root=tmp_path)
        assert any("WARNING: music-sync: not ready yet" in line for line in logs)


class TestMusicSyncVerify:
    def test_health_and_status(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(media=True))
        status = {
            "running": True,
            "sync_status": "success",
            "sync_sources": [],
            "spotify_ready": False,
            "ytmusic_ready": False,
            "playlists": 2,
            "tracks": 42,
        }

        def fake_curl(_cfg, _ip, container, url, **_kw):
            if url.endswith("/health"):
                return 0, "ok"
            if url.endswith("/api/status"):
                return 0, json.dumps(status)
            return 0, "ok"

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)

        checks = {
            c.check: c for c in _music_plugin().verify(cfg, {"MUSIC_SYNC_WEB_PASSWORD": "pw"}, "10.10.10.11", tmp_path)
        }
        assert checks["health"].passed is True
        assert checks["api_status"].passed is True
        assert "tracks=42" in checks["api_status"].detail

    def test_verify_hooks_dispatches_music_sync_plugin(self, tmp_path, monkeypatch):
        """Plugin verify() runs via verify_hooks and dedupes generic health sweep."""
        cfg = Config(domain="example.com", services=ServicesConfig(media=True))

        monkeypatch.setattr("toolkit.core.ops.hook_verify._check_forward_auth_routes", lambda *_a, **_k: [])
        monkeypatch.setattr("toolkit.core.ops.hook_verify._check_repo_parity", lambda *_a, **_k: [])
        monkeypatch.setattr(
            "toolkit.core.ops.hook_verify._check_sssd_active",
            lambda *_a, **_k: VerifyCheck("sssd", "active", True, "ok"),
        )
        monkeypatch.setattr(
            "toolkit.core.ops.hook_verify._check_ldap_getent",
            lambda *_a, **_k: VerifyCheck("ldap", "getent", True, "ok"),
        )
        monkeypatch.setattr("toolkit.core.ops.monitoring_verify.verify_monitoring_stack", lambda *_a, **_k: [])
        monkeypatch.setattr(
            "toolkit.services.enabled_service_plugins",
            lambda *_a, **_k: [("music-sync", _music_plugin())],
        )

        curl_calls: list[tuple[str, str]] = []

        def fake_curl(_cfg, _ip, container, url, **_kw):
            curl_calls.append((container, url))
            if container == "music-sync" and url.endswith("/health"):
                return 0, "ok"
            if container == "music-sync" and "/api/status" in url:
                return 0, json.dumps(
                    {
                        "running": True,
                        "sync_status": "success",
                        "sync_sources": [],
                        "spotify_ready": False,
                        "ytmusic_ready": False,
                        "playlists": 0,
                        "tracks": 0,
                    }
                )
            return 0, "ok"

        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        monkeypatch.setattr(
            "toolkit.services.sdk.container_exists_on_vm",
            lambda _cfg, _ip, container, _root: container == "music-sync",
        )

        result = verify_hooks(cfg, {}, tmp_path, vm="media")
        music_checks = [c for c in result.checks if c.service == "music-sync"]
        health_checks = [c for c in music_checks if c.check == "health"]
        assert len(health_checks) == 1
        assert health_checks[0].passed is True
        assert any(c.check == "api_status" and c.passed for c in music_checks)
        assert curl_calls.count(("music-sync", "http://localhost:8845/health")) == 1

    def test_status_without_current_source_contract_fails_closed(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(media=True))

        def fake_curl(_cfg, _ip, container, url, **_kw):
            if url.endswith("/health"):
                return 0, "ok"
            return 0, json.dumps({"running": True, "tracks": 0})

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        checks = _music_plugin().verify(cfg, {}, "10.10.10.11", tmp_path)
        status = next(check for check in checks if check.check == "api_status")
        assert status.passed is False
        assert status.detail == "status has invalid sync_status"

    def test_live_status_reports_pending_oauth_without_blocking_deploy_and_hides_warning_secrets(
        self, tmp_path, monkeypatch
    ):
        cfg = Config(domain="example.com", services=ServicesConfig(media=True))
        status = {
            "running": True,
            "sync_status": "failed",
            "warnings": ["token=super-secret; retrying provider"],
            "spotify_ready": False,
            "ytmusic_ready": True,
            "sync_sources": [
                {"name": "spotify", "configured": True, "success": False},
                {"name": "ytmusic", "configured": False, "success": False},
            ],
            "playlists": 1,
            "tracks": 3,
        }

        def fake_curl(_cfg, _ip, _container, url, **_kw):
            return (0, "ok") if url.endswith("/health") else (0, json.dumps(status))

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        check = next(c for c in _music_plugin().verify(cfg, {}, "10.10.10.11", tmp_path) if c.check == "api_status")

        assert check.passed is True
        assert "spotify" in check.detail
        assert "manual authorization" in check.detail
        assert "latest synchronization failed" not in check.detail
        assert "super-secret" not in check.detail
        assert check.retryable is False

    def test_ready_configured_source_sync_failure_remains_blocking(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(media=True))
        status = {
            "running": False,
            "sync_status": "failed",
            "warnings": [],
            "spotify_ready": True,
            "ytmusic_ready": False,
            "sync_sources": [
                {"name": "spotify", "configured": True, "success": False},
                {"name": "ytmusic", "configured": False, "success": False},
            ],
            "playlists": 1,
            "tracks": 3,
        }

        def fake_curl(_cfg, _ip, _container, url, **_kw):
            return (0, "ok") if url.endswith("/health") else (0, json.dumps(status))

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        check = next(c for c in _music_plugin().verify(cfg, {}, "10.10.10.11", tmp_path) if c.check == "api_status")

        assert check.passed is False
        assert "configured source synchronization failed: spotify" in check.detail
        assert "latest synchronization failed" in check.detail
        assert check.retryable is True

    def test_unconfigured_optional_sources_do_not_fail(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(media=True))
        status = {
            "running": True,
            "sync_status": "success",
            "spotify_ready": False,
            "ytmusic_ready": False,
            "sync_sources": [
                {"name": "spotify", "configured": False, "success": False},
                {"name": "ytmusic", "configured": False, "success": False},
            ],
        }

        def fake_curl(_cfg, _ip, _container, url, **_kw):
            return (0, "ok") if url.endswith("/health") else (0, json.dumps(status))

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        check = next(c for c in _music_plugin().verify(cfg, {}, "10.10.10.11", tmp_path) if c.check == "api_status")
        assert check.passed is True

    def test_malformed_boolean_status_fails_closed(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(media=True))
        status = {
            "running": "false",
            "sync_status": "success",
            "sync_sources": [],
            "spotify_ready": False,
            "ytmusic_ready": False,
        }

        def fake_curl(_cfg, _ip, _container, url, **_kw):
            return (0, "ok") if url.endswith("/health") else (0, json.dumps(status))

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        check = next(c for c in _music_plugin().verify(cfg, {}, "10.10.10.11", tmp_path) if c.check == "api_status")

        assert check.passed is False
        assert check.detail == "status running must be boolean"

    def test_structured_and_bearer_warnings_do_not_leak_secrets(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(media=True))
        status = {
            "running": True,
            "sync_status": "failed",
            "sync_sources": [],
            "spotify_ready": False,
            "ytmusic_ready": False,
            "warnings": [{"token": "structured-secret"}, "Authorization: Bearer bearer-secret"],
        }

        def fake_curl(_cfg, _ip, _container, url, **_kw):
            return (0, "ok") if url.endswith("/health") else (0, json.dumps(status))

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        check = next(c for c in _music_plugin().verify(cfg, {}, "10.10.10.11", tmp_path) if c.check == "api_status")

        assert "structured-secret" not in check.detail
        assert "bearer-secret" not in check.detail
        assert "[non-text warning omitted]" in check.detail
        assert "Bearer [redacted]" in check.detail

    def test_configured_source_failure_overrides_inconsistent_success_status(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(media=True))
        status = {
            "running": True,
            "sync_status": "success",
            "sync_sources": [{"name": "spotify", "configured": True, "success": False}],
            "spotify_ready": True,
            "ytmusic_ready": False,
        }

        def fake_curl(_cfg, _ip, _container, url, **_kw):
            return (0, "ok") if url.endswith("/health") else (0, json.dumps(status))

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        check = next(c for c in _music_plugin().verify(cfg, {}, "10.10.10.11", tmp_path) if c.check == "api_status")

        assert check.passed is False
        assert "configured source synchronization failed: spotify" in check.detail

    def test_manual_authorization_pending_is_nonblocking_and_nonretryable(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(media=True))
        status = {
            "running": True,
            "sync_status": "success",
            "sync_sources": [{"name": "spotify", "configured": True, "success": True}],
            "spotify_ready": False,
            "ytmusic_ready": False,
        }

        def fake_curl(_cfg, _ip, _container, url, **_kw):
            return (0, "ok") if url.endswith("/health") else (0, json.dumps(status))

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        check = next(c for c in _music_plugin().verify(cfg, {}, "10.10.10.11", tmp_path) if c.check == "api_status")

        assert check.passed is True
        assert check.retryable is False
