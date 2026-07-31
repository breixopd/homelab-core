"""Unit tests for Recyclarr post-start synchronization."""

from __future__ import annotations

import hashlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from toolkit.core.config.config import Config, ServicesConfig
from toolkit.services import get_service_plugin


def _plugin():
    plugin = get_service_plugin("recyclarr")
    assert plugin is not None
    return plugin


def test_recyclarr_uses_current_runtime_with_explicit_tini_reaping_contract() -> None:
    compose = yaml.safe_load((Path(__file__).parents[3] / "toolkit/services/recyclarr/compose.yaml").read_text())
    service = compose["services"]["recyclarr"]

    assert service["image"].startswith("ghcr.io/recyclarr/recyclarr:8.6.0@sha256:")
    assert service["healthcheck"]["timeout"] == "15s"


def test_post_start_generates_config_and_runs_sync(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "toolkit.services._arr.reconcile_servarr_api_key",
        lambda _root, _service, secrets, environment: secrets.get(environment, ""),
    )
    cfg = Config(domain="test.local", services=ServicesConfig(management=True, media=True))
    with patch("subprocess.run", return_value=MagicMock(returncode=0, stdout="", stderr="")) as run:
        logs = _plugin().post_start(
            cfg,
            {"SONARR_API_KEY": "sonarr", "RADARR_API_KEY": "radarr"},
            root=tmp_path,
        )

    config = tmp_path / "generated/recyclarr/recyclarr.yml"
    assert "sonarr" in config.read_text()
    assert (tmp_path / "generated/recyclarr/.last-sync.sha256").read_text().strip() == hashlib.sha256(
        config.read_bytes()
    ).hexdigest()
    run.assert_called_once()
    assert logs == ["Recyclarr: sync triggered"]


def test_post_start_fails_when_sync_command_fails(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "toolkit.services._arr.reconcile_servarr_api_key",
        lambda _root, _service, secrets, environment: secrets.get(environment, ""),
    )
    cfg = Config(domain="test.local", services=ServicesConfig(management=True, media=True))
    with (
        patch(
            "subprocess.run",
            return_value=MagicMock(returncode=2, stdout="", stderr="config invalid"),
        ),
        pytest.raises(RuntimeError, match="Recyclarr sync failed"),
    ):
        _plugin().post_start(
            cfg,
            {"SONARR_API_KEY": "sonarr", "RADARR_API_KEY": "radarr"},
            root=tmp_path,
        )
    assert not (tmp_path / "generated/recyclarr/.last-sync.sha256").exists()


def test_verify_does_not_sync_when_profiles_need_configuration(tmp_path, monkeypatch):
    cfg = Config(domain="example.com", services=ServicesConfig(media=True))
    sync_calls = []

    def ssh(_cfg, _vm, command, **_kwargs):
        if command.startswith("test -s"):
            return 0, "", ""
        if "--version" in command:
            return 0, "recyclarr 8.6.0", ""
        return 1, "", ""

    monkeypatch.setattr("toolkit.services.sdk.ssh_on_vm", ssh)
    monkeypatch.setattr(
        "toolkit.services.sdk.docker_curl",
        lambda *_a, **_k: (0, '[{"name":"Default"}]'),
    )
    monkeypatch.setattr(
        "toolkit.services.sdk.docker_exec_on_vm",
        lambda *_a, **_k: sync_calls.append(_a) or (1, "unexpected sync"),
    )

    checks = {check.check: check for check in _plugin().verify(cfg, {}, "10.0.0.2", tmp_path)}

    assert not checks["profiles"].passed
    assert checks["profiles"].status.value == "not_ready"
    assert sync_calls == []
