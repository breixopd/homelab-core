"""Unit tests for Prometheus service-owned post-start reconciliation."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config
from toolkit.core.verify.models import VerifyStatus


def _plugin():
    return load_plugin("prometheus").PrometheusPlugin()


def test_compose_enables_prometheus_lifecycle_reload() -> None:
    compose_path = Path(__file__).parents[3] / "toolkit" / "services" / "prometheus" / "compose.yaml"
    document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))

    assert "--web.enable-lifecycle" in document["services"]["prometheus"]["command"]


def test_post_start_reports_target_health():
    response = MagicMock()
    response.json.return_value = {
        "data": {
            "activeTargets": [
                {"health": "up"},
                {"health": "up"},
                {"health": "down"},
            ]
        }
    }
    response.raise_for_status.return_value = None
    with (
        patch("toolkit.services.sdk.wait_for_http", return_value=True),
        patch("httpx.get", return_value=response),
        patch("toolkit.core.ops.automation.resolve_docker_service_url", return_value="http://prometheus:9090"),
    ):
        logs = _plugin().post_start(Config(), {})

    assert logs == ["Prometheus: 2/3 targets up"]


def test_post_start_warns_when_targets_api_never_becomes_ready():
    with (
        patch("toolkit.services.sdk.wait_for_http", return_value=False),
        patch("toolkit.core.ops.automation.resolve_docker_service_url", return_value="http://prometheus:9090"),
    ):
        logs = _plugin().post_start(Config(), {})

    assert logs == ["WARNING: Prometheus not reachable yet (targets API timeout)"]


def test_management_status_and_target_inventory_are_bounded_and_sanitized(tmp_path, monkeypatch):
    payload = {
        "status": "success",
        "data": {
            "activeTargets": [
                {
                    "labels": {"job": "node-exporter"},
                    "health": "down",
                    "lastScrape": "2026-07-31T12:00:00Z",
                    "lastError": "GET https://user:password@example.test/metrics token=private",
                }
            ]
            * 150
        },
    }
    monkeypatch.setattr(
        "toolkit.services.sdk.docker_curl",
        lambda *_args, **_kwargs: (0, json.dumps(payload)),
    )
    plugin = _plugin()

    assert plugin.status(Config(domain="example.com"), {}, tmp_path) == {
        "target_count": 150,
        "healthy_targets": 0,
        "unhealthy_targets": 150,
    }
    rows = plugin.resources(Config(domain="example.com"), {}, tmp_path)["targets"]
    assert len(rows) == 100
    assert rows[0] == {
        "job": "node-exporter",
        "health": "down",
        "last_scrape": "2026-07-31T12:00:00Z",
        "last_error": "GET https://[REDACTED]@example.test/metrics token=[REDACTED]",
    }
    assert plugin._safe_target_text("GET https://token-only@example.test/metrics") == (
        "GET https://[REDACTED]@example.test/metrics"
    )


def test_management_status_fails_closed_on_unavailable_targets_api(tmp_path, monkeypatch):
    monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_args, **_kwargs: (1, ""))

    assert _plugin().status(Config(), {}, tmp_path) == {}


def test_management_status_rejects_oversized_targets_response(tmp_path, monkeypatch):
    body = json.dumps({"status": "success", "data": {"activeTargets": [], "padding": "x" * (512 * 1024)}})
    monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_args, **_kwargs: (0, body))

    assert _plugin().status(Config(), {}, tmp_path) == {}


def test_management_status_rejects_prometheus_error_envelope(tmp_path, monkeypatch):
    body = json.dumps({"status": "error", "errorType": "unavailable", "data": {"activeTargets": []}})
    monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_args, **_kwargs: (0, body))

    assert _plugin().status(Config(), {}, tmp_path) == {}


def test_verify_marks_zero_targets_not_ready(tmp_path, monkeypatch):
    monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)

    def fake_curl(_cfg, _ip, _container, url, **_kwargs):
        if url.endswith("/-/healthy") or url.endswith("/-/ready"):
            return 0, "Prometheus is Ready."
        return 0, json.dumps({"status": "success", "data": {"activeTargets": []}})

    monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
    checks = {c.check: c for c in _plugin().verify(Config(domain="example.com"), {}, "10.10.10.10", tmp_path)}
    assert checks["targets"].status is VerifyStatus.NOT_READY
