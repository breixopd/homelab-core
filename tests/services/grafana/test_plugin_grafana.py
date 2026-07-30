"""Unit tests for grafana plugin verify()."""

from __future__ import annotations

import json
from unittest.mock import patch

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig


def _plugin():
    module = load_plugin("grafana")
    for name in dir(module):
        if not name.endswith("Plugin") or name == "ServicePlugin":
            continue
        obj = getattr(module, name)
        if isinstance(obj, type):
            return obj()
    raise RuntimeError("no grafana plugin")


def test_post_start_reloads_and_verifies_provisioning(tmp_path):
    cfg = Config(domain="example.com", services=ServicesConfig(management=True))
    secrets = {"GRAFANA_ADMIN_PASSWORD": "secret"}
    with (
        patch(
            "toolkit.services.grafana.bootstrap.reload_dashboard_provisioning",
            return_value=["Grafana: dashboards reloaded"],
        ),
        patch(
            "toolkit.services.grafana.bootstrap.verify_grafana_provisioning",
            return_value=["Grafana: dashboards verified"],
        ),
        patch(
            "toolkit.services.grafana.bootstrap.verify_grafana_datasources",
            return_value=["Grafana: datasources verified"],
        ),
        patch("toolkit.core.ops.automation.resolve_docker_service_url", return_value="http://grafana:3000"),
    ):
        logs = _plugin().post_start(cfg, secrets, root=tmp_path)

    assert logs == [
        "Grafana: dashboards reloaded",
        "Grafana: dashboards verified",
        "Grafana: datasources verified",
    ]


def test_smtp_uses_fully_qualified_ehlo_identity() -> None:
    from toolkit.services import get_service_plugin

    plugin = get_service_plugin("grafana")
    assert plugin is not None
    environment = plugin.compose_application()["services"]["grafana"]["environment"]

    assert environment["GF_SMTP_EHLO_IDENTITY"] == "grafana.${BASE_DOMAIN:-localhost}"


class TestGrafanaVerify:
    def test_skips_localhost(self, tmp_path):
        cfg = Config(domain="localhost", services=ServicesConfig(management=True))
        checks = _plugin().verify(cfg, {"GRAFANA_ADMIN_PASSWORD": "x"}, "10.10.10.10", tmp_path)
        assert checks[0].passed

    def test_health_datasources_dashboards(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))
        datasources = [
            {"name": "Prometheus", "uid": "prometheus"},
            {"name": "Loki", "uid": "loki"},
        ]

        def fake_curl(_cfg, _ip, container, url, **_kw):
            if url.endswith("/api/health"):
                return 0, json.dumps({"database": "ok", "version": "13.0.2"})
            if url.endswith("/api/datasources"):
                return 0, json.dumps(datasources)
            if "/health" in url and "/datasources/" in url:
                return 0, json.dumps({"status": "OK"})
            if "search?type=dash-db" in url:
                return 0, json.dumps([{"title": "Homelab"}])
            return 1, ""

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_exec_on_vm",
            lambda *_a, **_k: (
                0,
                "GF_AUTH_GENERIC_OAUTH_AUTH_URL=https://auth.example.com/api/oidc/authorization\n"
                "GF_AUTH_GENERIC_OAUTH_TOKEN_URL=http://authelia:9091/api/oidc/token",
            ),
        )

        checks = {
            c.check: c for c in _plugin().verify(cfg, {"GRAFANA_ADMIN_PASSWORD": "secret"}, "10.10.10.10", tmp_path)
        }
        assert checks["health"].passed
        assert checks["datasource_health_prometheus"].passed
        assert checks["datasource_health_loki"].passed
        assert checks["dashboards"].passed
        assert checks["oidc_token_url"].passed
