"""Controller service-management actions use manifest-owned capabilities."""

from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from toolkit.controller.app import create_controller_app
from toolkit.controller.store import ControllerStore
from toolkit.core.config.config import Config, load_config, save_config
from toolkit.core.config.storage import config_path

_LOCAL_TOKEN = "local-controller-token-for-tests-000000000000"


@pytest.fixture
async def controller_client(tmp_path: Path):
    store = ControllerStore(tmp_path / "controller.db")
    app = create_controller_app(
        root=tmp_path,
        store=store,
        local_transport_token=_LOCAL_TOKEN,
        ui_transport_token="ui-controller-token-for-tests-000000000000",
    )
    async with AsyncClient(
        transport=ASGITransport(app=app, client=None, raise_app_exceptions=False),
        base_url="http://controller",
        headers={"x-controller-transport": "local", "x-controller-token": _LOCAL_TOKEN},
    ) as client:
        yield client


@pytest.mark.anyio
async def test_service_settings_patch_is_revisioned_and_manifest_scoped(
    controller_client: AsyncClient,
    tmp_path: Path,
) -> None:
    save_config(Config(domain="example.com"), config_path(tmp_path))
    current = await controller_client.get("/v1/services/media-cache/management")
    response = await controller_client.patch(
        "/v1/services/media-cache/settings",
        json={
            "expected_revision": current.json()["revision"],
            "values": {"cold-after-days": 30},
        },
    )
    rejected = await controller_client.patch(
        "/v1/services/media-cache/settings",
        json={
            "expected_revision": response.json()["revision"],
            "values": {"music-sync": False},
        },
    )

    assert response.status_code == 200
    assert load_config(config_path(tmp_path)).service_settings["media-cache"]["cold-after-days"] == 30
    assert rejected.status_code == 422
    assert rejected.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.anyio
async def test_service_action_submission_is_declared_and_single_flight(
    controller_client: AsyncClient,
    tmp_path: Path,
) -> None:
    save_config(Config(domain="example.com"), config_path(tmp_path))
    first = await controller_client.post(
        "/v1/jobs",
        json={
            "idempotency_key": "service-action-first",
            "operation": {"kind": "SERVICE_ACTION", "service": "music-sync", "action": "sync-now"},
        },
    )
    duplicate = await controller_client.post(
        "/v1/jobs",
        json={
            "idempotency_key": "service-action-second",
            "operation": {"kind": "SERVICE_ACTION", "service": "music-sync", "action": "sync-now"},
        },
    )
    undeclared = await controller_client.post(
        "/v1/jobs",
        json={
            "idempotency_key": "service-action-third",
            "operation": {"kind": "SERVICE_ACTION", "service": "music-sync", "action": "wipe-library"},
        },
    )

    assert first.status_code == 201
    assert duplicate.status_code == 429
    assert undeclared.status_code == 422
    assert undeclared.json()["error"]["code"] == "VALIDATION_ERROR"
