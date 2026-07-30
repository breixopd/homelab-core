from __future__ import annotations

import json
from pathlib import Path

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
        "metadata_providers_enabled": 2,
    }


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
