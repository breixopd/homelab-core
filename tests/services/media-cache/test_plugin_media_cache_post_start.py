"""Unit tests for media-cache webhook reconciliation."""

from __future__ import annotations

from importlib import import_module
from unittest.mock import MagicMock, patch

from toolkit.core.config.config import Config, ServicesConfig

cache_client_module = import_module("toolkit.services.media-cache.client")


def _plugin():
    from toolkit.services import get_service_plugin

    plugin = get_service_plugin("media-cache")
    assert plugin is not None
    return plugin


def _cfg() -> Config:
    cfg = Config(
        domain="test.local",
        services=ServicesConfig(management=True, media=True),
        service_settings={
            "media-library": {"server": "plex"},
            "media-cache": {"enabled": True},
        },
    )
    return cfg


def test_post_start_registers_tautulli_webhook(tmp_path):
    cache = MagicMock(health=MagicMock(return_value=True))
    with (
        patch.object(cache_client_module, "MediaCacheClient", return_value=cache),
        patch("toolkit.core.ops.automation.resolve_docker_service_url", return_value="http://127.0.0.1:8686"),
        patch.object(
            cache_client_module,
            "register_tautulli_webhook",
            return_value=(True, "tautulli webhook registered"),
        ) as register,
    ):
        logs = _plugin().post_start(_cfg(), {"MEDIA_CACHE_TOKEN": "cache", "TAUTULLI_API_KEY": "tok"}, root=tmp_path)

    assert register.call_args.kwargs["webhook_url"] == "http://media-cache:8686/webhook/tautulli"
    assert any("registered tautulli webhook" in line for line in logs)


def test_post_start_webhook_failure_is_best_effort(tmp_path):
    cache = MagicMock(health=MagicMock(return_value=True))
    with (
        patch.object(cache_client_module, "MediaCacheClient", return_value=cache),
        patch("toolkit.core.ops.automation.resolve_docker_service_url", return_value="http://127.0.0.1:8686"),
        patch.object(cache_client_module, "register_tautulli_webhook", side_effect=RuntimeError("failed")),
    ):
        logs = _plugin().post_start(_cfg(), {"TAUTULLI_API_KEY": "tok"}, root=tmp_path)

    assert any("WARNING:" in line and "tautulli webhook registration failed" in line for line in logs)


def test_post_start_warns_when_tautulli_key_is_missing(tmp_path):
    cache = MagicMock(health=MagicMock(return_value=True))
    with (
        patch.object(cache_client_module, "MediaCacheClient", return_value=cache),
        patch("toolkit.core.ops.automation.resolve_docker_service_url", return_value="http://127.0.0.1:8686"),
        patch.object(cache_client_module, "register_tautulli_webhook") as register,
    ):
        logs = _plugin().post_start(_cfg(), {}, root=tmp_path)

    register.assert_not_called()
    assert any("TAUTULLI_API_KEY missing" in line for line in logs)
