from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml
from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig


def _plugin():
    module = load_plugin("romm")
    return module.RommPlugin()


def test_romm_verify_uses_unauthenticated_heartbeat(tmp_path, monkeypatch):
    cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
    monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
    calls: list[tuple[str, str]] = []

    def fake_curl(_cfg, _ip, container, url, **_kwargs):
        calls.append((container, url))
        return 0, json.dumps(
            {
                "status": "ok",
                "SYSTEM": {"SHOW_SETUP_WIZARD": False},
                "METADATA_SOURCES": {
                    "HASHEOUS_API_ENABLED": True,
                    "HLTB_API_ENABLED": True,
                    "TGDB_API_ENABLED": True,
                    "PLAYMATCH_API_ENABLED": False,
                    "FLASHPOINT_API_ENABLED": False,
                },
            }
        )

    monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
    monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", lambda *_a, **_k: (0, ""))
    checks = _plugin().verify(cfg, {}, "10.10.10.12", tmp_path)

    assert checks[0].passed
    assert checks[1].passed
    assert checks[2].passed
    assert checks[3].passed
    assert calls == [("romm", "http://localhost:8080/api/heartbeat")]


def test_romm_status_exposes_heartbeat_and_counts(tmp_path, monkeypatch):
    cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
    monkeypatch.setattr(
        "toolkit.services.sdk.docker_curl",
        lambda *_a, **_k: (
            0,
            '{"roms":12,"platforms":4,"users":2,"show_setup_wizard":false,'
            '"METADATA_SOURCES":{"HASHEOUS_API_ENABLED":true,"HLTB_API_ENABLED":true}}',
        ),
    )

    assert _plugin().status(cfg, {}, tmp_path) == {
        "heartbeat": 1,
        "roms": 12,
        "platforms": 4,
        "users": 2,
        "metadata_providers_configured": 3,
        "metadata_providers_enabled": 2,
        "metadata_providers_aligned": 2,
        "metadata_sources_observed": 1,
        "metadata_hasheous_configured": 1,
        "metadata_hltb_configured": 1,
        "metadata_tgdb_configured": 1,
        "metadata_playmatch_configured": 0,
        "metadata_flashpoint_configured": 0,
        "metadata_igdb_configured": 0,
        "metadata_screenscraper_configured": 0,
        "metadata_steamgriddb_configured": 0,
        "metadata_retroachievements_configured": 0,
        "metadata_hasheous_enabled": 1,
        "metadata_hltb_enabled": 1,
    }


def test_romm_status_omits_untrusted_metadata_and_negative_counts(tmp_path, monkeypatch):
    cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
    monkeypatch.setattr(
        "toolkit.services.sdk.docker_curl",
        lambda *_a, **_k: (0, '{"roms":-1,"users":true,"METADATA_SOURCES":[]}'),
    )

    assert _plugin().status(cfg, {}, tmp_path) == {"heartbeat": 1}


def test_romm_status_reports_setting_override_as_provider_drift(tmp_path, monkeypatch):
    cfg = Config(
        domain="example.com",
        services=ServicesConfig(cloud=True),
        service_settings={"romm": {"hasheous-enabled": False}},
    )
    monkeypatch.setattr(
        "toolkit.services.sdk.docker_curl",
        lambda *_a, **_k: (0, '{"METADATA_SOURCES":{"HASHEOUS_API_ENABLED":true}}'),
    )

    status = _plugin().status(cfg, {}, tmp_path)
    assert status["metadata_hasheous_enabled"] == 1
    assert status["metadata_hasheous_configured"] == 0
    assert status["metadata_providers_configured"] == 2
    assert status["metadata_providers_aligned"] == 0


def test_romm_resources_present_provider_readiness_without_secrets(tmp_path, monkeypatch):
    cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
    monkeypatch.setattr(
        "toolkit.services.sdk.docker_curl",
        lambda *_a, **_k: (
            0,
            '{"METADATA_SOURCES":{"HASHEOUS_API_ENABLED":true,"HLTB_API_ENABLED":false}}',
        ),
    )

    rows = _plugin().resources(cfg, {"ROMM_TGDB_CLIENT_SECRET": "must-not-appear"}, tmp_path)["metadata_providers"]

    assert rows[0] == {
        "provider": "Hasheous",
        "configured": "Yes",
        "runtime": "Enabled",
        "config_parity": "Aligned",
    }
    assert rows[1]["runtime"] == "Disabled"
    assert rows[1]["config_parity"] == "Drift"
    assert all("must-not-appear" not in repr(row) for row in rows)


def test_romm_optional_provider_and_missing_source_state(tmp_path, monkeypatch):
    cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
    responses = iter(
        [
            '{"METADATA_SOURCES":{"IGDB_API_ENABLED":true,"MOBY_API_ENABLED":true}}',
            '{"roms":1}',
        ]
    )
    monkeypatch.setattr(
        "toolkit.services.sdk.docker_curl",
        lambda *_a, **_k: (0, next(responses)),
    )

    status = _plugin().status(
        cfg,
        {"IGDB_CLIENT_ID": "client", "IGDB_CLIENT_SECRET": "secret"},
        tmp_path,
    )
    assert status["metadata_igdb_configured"] == 1
    assert status["metadata_igdb_enabled"] == 1
    assert status["metadata_moby_enabled"] == 1
    assert status["metadata_providers_enabled"] == 2
    assert status["metadata_providers_aligned"] == 2

    with pytest.raises(RuntimeError, match="status is unavailable"):
        _plugin().resources(cfg, {}, tmp_path)


def test_romm_library_permissions_are_scoped_to_app_directory() -> None:
    service_root = Path(__file__).parents[3] / "toolkit/services/romm"
    manifest = yaml.safe_load((service_root / "service.yaml").read_text(encoding="utf-8"))
    assert {metric["key"] for metric in manifest["management"]["metrics"]} >= {
        "roms",
        "platforms",
        "users",
        "metadata_providers_enabled",
    }
    library = next(spec for spec in manifest["data_specs"] if spec["name"] == "romm-library")
    assert library["host_subdirs"] == ["roms"]
    assert library["manage_permissions"] is False
    assert library["shared"] is True
    assert not (service_root / "ansible/pre-deploy.yml").exists()
