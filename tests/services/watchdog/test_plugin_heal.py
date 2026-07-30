"""Tests for plugin heal dispatch from the watchdog."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.ops.watchdog import HealthIssue, Watchdog, WatchdogReport
from toolkit.services import _reset_cache, heal_routing_map


@pytest.fixture(autouse=True)
def _clear_plugin_cache():
    _reset_cache()
    yield
    _reset_cache()


def test_heal_routing_map_excludes_stateful_services_without_safe_heal_steps():
    routes = heal_routing_map()
    assert "wazuh-indexer" not in routes
    assert "wazuh-dashboard" not in routes
    assert "komodo-core" not in routes
    assert "komodo-mongo" not in routes
    assert routes["qbittorrent-vpn"].service == "qbittorrent"
    assert routes["postgres"].service == "postgres"


def test_watchdog_dispatches_plugin_heal(tmp_path: Path):
    cfg = Config(domain="localhost", services=ServicesConfig())
    wd = Watchdog(tmp_path, cfg)

    report = WatchdogReport()
    report.issues.append(
        HealthIssue(
            service="authelia",
            category="management",
            severity="critical",
            message="down",
            auto_fixable=True,
        )
    )

    with patch("toolkit.services.authelia.bootstrap.heal_authelia", return_value=["healed authelia"]) as heal_fn:
        result = wd.heal(report)

    heal_fn.assert_called_once_with(tmp_path)
    assert result.attempted == 1
    assert result.succeeded == 1
    assert result.failed == 0
    assert any("healed authelia" in line for line in result.logs)


def test_watchdog_skips_generic_restart_for_structured_heal(tmp_path: Path):
    cfg = Config(domain="localhost", services=ServicesConfig())
    wd = Watchdog(tmp_path, cfg)

    report = WatchdogReport()
    report.issues.append(
        HealthIssue(
            service="postgres",
            category="management",
            severity="critical",
            message="down",
            auto_fixable=True,
        )
    )

    with (
        patch(
            "toolkit.services.sdk.ensure_postgres_healthy",
            return_value=["postgres ok"],
        ),
        patch.object(wd, "_docker_action") as docker_action,
    ):
        result = wd.heal(report)

    docker_action.assert_not_called()
    assert result.attempted == 1
    assert result.succeeded == 1


def test_watchdog_wazuh_dashboard_routes_to_indexer_plugin(tmp_path: Path):
    cfg = Config(domain="localhost", services=ServicesConfig())
    wd = Watchdog(tmp_path, cfg)
    plugin = MagicMock()
    plugin.service = "wazuh-indexer"
    plugin.heal.return_value = ["indexer heal"]

    report = WatchdogReport()
    report.issues.append(
        HealthIssue(
            service="wazuh-dashboard",
            category="security",
            severity="critical",
            message="down",
            auto_fixable=True,
        )
    )

    with (
        patch("toolkit.services.heal_routing_map", return_value={"wazuh-dashboard": plugin}),
        patch.object(Watchdog, "structured_heal_services", return_value=frozenset({"wazuh-dashboard"})),
    ):
        result = wd.heal(report)

    plugin.heal.assert_called_once_with(cfg, tmp_path, service="wazuh-dashboard")
    assert result.attempted == 1
    assert result.succeeded == 1
    assert "indexer heal" in result.logs


def test_watchdog_records_structured_heal_failure(tmp_path: Path):
    cfg = Config(domain="localhost", services=ServicesConfig())
    wd = Watchdog(tmp_path, cfg)
    plugin = MagicMock()
    plugin.service = "authelia"
    plugin.heal.side_effect = RuntimeError("failed")
    report = WatchdogReport(issues=[HealthIssue("authelia", "management", "critical", "down", auto_fixable=True)])

    with (
        patch("toolkit.services.heal_routing_map", return_value={"authelia": plugin}),
        patch.object(Watchdog, "structured_heal_services", return_value=frozenset({"authelia"})),
    ):
        result = wd.heal(report)

    assert result.attempted == 1
    assert result.succeeded == 0
    assert result.failed == 1
    assert result.ok is False
