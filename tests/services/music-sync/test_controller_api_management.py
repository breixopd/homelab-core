"""Music Sync controller projections are owned by the service suite."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from toolkit.controller.app import create_controller_app
from toolkit.controller.store import ControllerStore
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path
from toolkit.services import get_service_plugin

_LOCAL_TOKEN = "local-controller-token-for-tests-000000000000"
_UI_TOKEN = "ui-controller-token-for-tests-000000000000000"


@pytest.fixture
async def controller_client(tmp_path: Path):
    store = ControllerStore(tmp_path / "controller.db")
    app = create_controller_app(
        root=tmp_path,
        store=store,
        local_transport_token=_LOCAL_TOKEN,
        ui_transport_token=_UI_TOKEN,
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, client=None, raise_app_exceptions=False),
        base_url="http://controller",
        headers={"x-controller-transport": "local", "x-controller-token": _LOCAL_TOKEN},
    ) as client:
        yield client


@pytest.mark.anyio
async def test_service_management_returns_declared_capabilities_without_status_extras(
    controller_client: AsyncClient,
    monkeypatch,
    tmp_path: Path,
) -> None:
    save_config(Config(domain="example.com"), config_path(tmp_path))
    plugin = get_service_plugin("music-sync")
    assert plugin is not None
    monkeypatch.setattr(
        plugin,
        "status",
        lambda _cfg, _secrets, _root: {
            "tracks": 19,
            "playlists": 3,
            "heartbeat_age_seconds": 12,
            "access_token": "must-not-be-returned",
        },
    )

    response = await controller_client.get("/v1/services/music-sync/management")

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["service"] == "music-sync"
    custom_metrics = {
        metric["key"]: metric["value"]
        for metric in payload["metrics"]
        if metric["key"] in {"tracks", "playlists", "heartbeat_age_seconds"}
    }
    assert custom_metrics == {
        "tracks": 19,
        "playlists": 3,
        "heartbeat_age_seconds": 12,
    }
    assert "access_token" not in response.text
    assert "must-not-be-returned" not in response.text
