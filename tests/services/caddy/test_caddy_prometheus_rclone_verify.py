"""Unit tests for caddy, prometheus, and rclone plugin verify()."""

from __future__ import annotations

import json
from types import SimpleNamespace

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig


def _plugin(service: str):
    module = load_plugin(service)
    for name in dir(module):
        obj = getattr(module, name)
        if isinstance(obj, type) and name.endswith("Plugin"):
            return obj()
    raise RuntimeError(f"no plugin class in {service}")


class TestCaddyVerify:
    def test_skips_on_localhost(self, tmp_path):
        cfg = Config(domain="localhost", services=ServicesConfig(management=True))
        checks = {c.check: c for c in _plugin("caddy").verify(cfg, {}, "10.10.10.10", tmp_path)}
        assert all(c.passed for c in checks.values())
        assert "localhost" in checks["route_parity"].detail

    def test_route_parity_and_forward_auth(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True, media=True))
        caddy_config = (
            "grafana.example.com {\n"
            "    reverse_proxy grafana:3000\n"
            "}\n"
            "sonarr.example.com {\n"
            "    forward_auth http://authelia:9091 {}\n"
            "    reverse_proxy 10.10.10.11:8989\n"
            "}\n"
        )
        monkeypatch.setattr(
            "toolkit.core.manifest.routes.compile_routes",
            lambda _cfg: (
                SimpleNamespace(
                    service="grafana",
                    host="grafana.example.com",
                    match=None,
                    auth=SimpleNamespace(mode="oidc"),
                ),
                SimpleNamespace(
                    service="sonarr",
                    host="sonarr.example.com",
                    match=None,
                    auth=SimpleNamespace(mode="forward_auth"),
                ),
            ),
        )

        def fake_ssh(_cfg, _ip, cmd, root=None, timeout=30, **_kw):
            if "Caddyfile" in cmd:
                return 0, caddy_config, ""
            return 0, "200", ""

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_exec_on_vm",
            lambda *_a, **_k: (0, caddy_config),
        )
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_curl",
            lambda *_a, **_k: (0, caddy_config),
        )
        monkeypatch.setattr("toolkit.services.sdk.ssh_on_vm", fake_ssh)

        checks = {c.check: c for c in _plugin("caddy").verify(cfg, {}, "10.10.10.10", tmp_path)}
        assert checks["route_parity"].passed is True
        assert checks["forward_auth"].passed is True
        assert checks["live_probe"].passed is True

    def test_missing_routes_fail(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True, media=True))
        monkeypatch.setattr(
            "toolkit.core.manifest.routes.compile_routes",
            lambda _cfg: (
                SimpleNamespace(
                    service="grafana",
                    host="grafana.example.com",
                    match=None,
                    auth=SimpleNamespace(mode="oidc"),
                ),
            ),
        )
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_curl",
            lambda *_a, **_k: (0, "# empty caddy config"),
        )
        monkeypatch.setattr(
            "toolkit.services.sdk.ssh_on_vm",
            lambda *_a, **_k: (0, "502", ""),
        )

        checks = {c.check: c for c in _plugin("caddy").verify(cfg, {}, "10.10.10.10", tmp_path)}
        assert checks["route_parity"].passed is False
        assert "missing" in checks["route_parity"].detail


class TestPrometheusVerify:
    def test_healthy_ready_and_targets(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))
        targets = {
            "data": {
                "activeTargets": [
                    {"health": "up", "labels": {"job": "prometheus"}},
                    {"health": "down", "labels": {"job": "node"}},
                ]
            }
        }

        def fake_curl(_cfg, _ip, container, url, **_kw):
            if "targets" in url:
                return 0, json.dumps(targets)
            return 0, "ok"

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)

        checks = {c.check: c for c in _plugin("prometheus").verify(cfg, {}, "10.10.10.10", tmp_path)}
        assert checks["healthy"].passed is True
        assert checks["ready"].passed is True
        assert checks["targets"].passed is False
        assert "node" in checks["targets"].detail

    def test_fails_when_container_missing(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: False)
        checks = _plugin("prometheus").verify(cfg, {}, "10.10.10.10", tmp_path)
        assert checks and all(not c.passed for c in checks)
        assert all("missing" in c.detail for c in checks)


class TestRcloneVerify:
    def test_mount_health(self, tmp_path, monkeypatch):
        cfg = Config(
            domain="example.com",
            services=ServicesConfig(media=True),
            service_settings={"media-cache": {"enabled": True}},
        )

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_health_status_on_vm",
            lambda *_a, **_k: ("running", "healthy"),
        )
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_exec_on_vm",
            lambda *_a, **_k: (0, ""),
        )

        checks = {c.check: c for c in _plugin("rclone").verify(cfg, {}, "10.10.10.11", tmp_path)}
        assert checks["container"].passed is True
        assert checks["mount"].passed is True
        assert checks["rc_api"].passed is True

    def test_skips_when_cache_disabled(self, tmp_path):
        cfg = Config(
            domain="example.com",
            services=ServicesConfig(media=True),
            service_settings={"media-cache": {"enabled": False}},
        )
        checks = _plugin("rclone").verify(cfg, {}, "10.10.10.11", tmp_path)
        assert len(checks) == 1
        assert checks[0].passed is True
        assert "disabled" in checks[0].detail
