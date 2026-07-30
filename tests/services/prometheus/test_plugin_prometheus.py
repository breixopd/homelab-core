"""Unit tests for Prometheus service-owned post-start reconciliation."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config


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
