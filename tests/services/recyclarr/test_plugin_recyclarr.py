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
