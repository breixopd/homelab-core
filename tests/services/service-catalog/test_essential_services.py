"""Tests for essential service flag loading and deploy guard."""

from __future__ import annotations

import pytest
from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.deploy.essential_guard import (
    EssentialServicesDisabledError,
    assert_essential_services_enabled,
    disabled_essential_services,
    essential_removal_allowed,
)
from toolkit.services import _reset_cache, essential_service_plugins, get_service_plugin


@pytest.fixture(autouse=True)
def _load_categories():
    from toolkit.core.compose.registry import load_all

    load_all()
    yield


@pytest.fixture(autouse=True)
def _clear_plugin_cache():
    _reset_cache()
    yield
    _reset_cache()


def test_essential_flag_loaded_from_yaml():
    postgres = get_service_plugin("postgres")
    assert postgres is not None
    assert postgres.essential is True

    komodo = get_service_plugin("komodo-core")
    assert komodo is not None
    assert komodo.essential is False


def test_essential_service_plugins_returns_marked_plugins():
    names = {p.service for p in essential_service_plugins()}
    assert {
        "authelia",
        "postgres",
        "redis",
        "lldap",
        "caddy",
        "adguard",
        "prometheus",
        "loki",
        "vaultwarden",
        "registry-mirror",
        "wazuh-indexer",
        "wazuh-dashboard",
        "crowdsec",
    }.issubset(names)


def test_essential_safe_restart_upgraded_to_careful():
    loki = get_service_plugin("loki")
    assert loki is not None
    assert loki._yaml_data.get("restart_policy") == "careful"


def test_disabled_essential_services_when_cloud_off():
    cfg = Config(
        domain="test.example.com",
        services=ServicesConfig(cloud=False, security=True),
    )
    disabled = disabled_essential_services(cfg)
    assert "vaultwarden" in disabled
    assert "postgres" not in disabled


def test_assert_essential_services_enabled_raises():
    cfg = Config(
        domain="test.example.com",
        services=ServicesConfig(cloud=False, security=False),
    )
    with pytest.raises(EssentialServicesDisabledError, match="vaultwarden|crowdsec"):
        assert_essential_services_enabled(cfg)


def test_assert_essential_services_override_env(monkeypatch):
    cfg = Config(domain="test.example.com", services=ServicesConfig())
    monkeypatch.setenv("HOMELAB_ALLOW_ESSENTIAL_REMOVAL", "1")
    assert essential_removal_allowed()
    assert_essential_services_enabled(cfg)
