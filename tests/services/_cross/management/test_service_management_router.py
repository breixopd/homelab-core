from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from toolkit.controller.client import ControllerClientError
from toolkit.controller.contracts import JobRecord, JobState
from toolkit.controller.read_models import (
    ManagedServiceActionView,
    ManagedServiceMetricView,
    ManagedServiceResourceColumnView,
    ManagedServiceResourceView,
    ManagedServiceSecretView,
    ManagedServiceSettingView,
    SecretInventory,
    SecretMutationResult,
    ServiceManagementView,
    ServiceSettingsUpdate,
    ServiceVerificationView,
)

pytestmark = pytest.mark.anyio


class ServiceController:
    def __init__(self) -> None:
        self.unavailable = False
        self.updates: list[tuple[str, ServiceSettingsUpdate]] = []
        self.secret_updates = []
        self.jobs = []
        self.management_calls: list[bool] = []
        self.verification = ServiceVerificationView(service="music-sync", state="never")
        self.view = ServiceManagementView(
            revision="a" * 64,
            service="music-sync",
            label="Music Sync",
            description="Import configured music libraries",
            category="media",
            node="media",
            enabled=True,
            status_available=True,
            secrets=[
                ManagedServiceSecretView(
                    name="SPOTIFY_CLIENT_SECRET",
                    label="Spotify client secret",
                    description="OAuth application secret.",
                    is_configured=False,
                )
            ],
            settings=[
                ManagedServiceSettingView(
                    key="enabled",
                    label="Enabled",
                    description="Import music libraries.",
                    type="boolean",
                    value=True,
                    default=True,
                    choices=[],
                    requires_redeploy=True,
                ),
                ManagedServiceSettingView(
                    key="interval-minutes",
                    label="Sync interval",
                    description="Minutes between runs.",
                    type="number",
                    value=60,
                    default=60,
                    minimum=5,
                    maximum=1440,
                    step=5,
                    choices=[],
                    requires_redeploy=True,
                ),
                ManagedServiceSettingView(
                    key="headroom-percent",
                    label="Resource headroom",
                    description="Capacity retained above observed demand.",
                    type="number",
                    value=130,
                    default=130,
                    minimum=110,
                    maximum=300,
                    step=5,
                    choices=[],
                    requires_redeploy=False,
                ),
            ],
            actions=[
                ManagedServiceActionView(
                    id="sync-now",
                    label="Sync now",
                    description="Start a music library sync.",
                    confirmation="Start a music library sync now?",
                    is_dangerous=False,
                    can_run=True,
                )
            ],
            metrics=[
                ManagedServiceMetricView(
                    key="tracks",
                    label="Imported tracks",
                    unit="count",
                    precision=0,
                    value=431,
                ),
                ManagedServiceMetricView(
                    key="heartbeat_age_seconds",
                    label="Last heartbeat age",
                    unit="seconds",
                    precision=0,
                    value=32,
                ),
            ],
            resources=[
                ManagedServiceResourceView(
                    key="storage_backends",
                    label="Storage backends",
                    description="Configured storage pool members.",
                    available=True,
                    columns=[
                        ManagedServiceResourceColumnView(key="name", label="Name"),
                        ManagedServiceResourceColumnView(key="kind", label="Kind"),
                    ],
                    rows=[{"name": "ext-nas", "kind": "Fleet storage"}],
                )
            ],
        )

    def service_management(self, service: str, *, collect_status: bool = True) -> ServiceManagementView:
        assert service == "music-sync"
        self.management_calls.append(collect_status)
        if self.unavailable:
            raise ControllerClientError("unavailable")
        return self.view

    def service_verification(self, service: str) -> ServiceVerificationView:
        assert service == "music-sync"
        return self.verification

    def update_service_settings(self, service: str, update: ServiceSettingsUpdate) -> ServiceManagementView:
        self.updates.append((service, update))
        return self.view.model_copy(update={"revision": "b" * 64})

    def update_secrets(self, update):
        self.secret_updates.append(update)
        return SecretMutationResult(
            changed_names=sorted(update.values),
            inventory=SecretInventory(
                owner_email="owner@example.com",
                storage_mode="encrypted",
                encryption_available=True,
                entries=[],
            ),
        )

    def submit(self, request) -> JobRecord:
        self.jobs.append(request)
        from datetime import UTC, datetime

        now = datetime(2026, 7, 11, tzinfo=UTC)
        return JobRecord(
            job_id="job-service-action-1234",
            request=request,
            state=JobState.QUEUED,
            actor="ui:homelab-ui",
            created_at=now,
            updated_at=now,
        )

    def close(self) -> None:
        return None


def _create_app(tmp_path: Path, monkeypatch, controller: ServiceController) -> FastAPI:
    (tmp_path / "config.yaml").write_text(
        "domain: test.example.com\nemail: owner@example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WEBUI_SESSION_SECRET", "service-management-router-secret")
    monkeypatch.setattr("toolkit.webui.app.controller_client_from_environment", lambda: controller)
    monkeypatch.setattr(
        "toolkit.webui.routers.auth.verify_password",
        lambda _password, _client_ip, email="": (True, "Authenticated"),
    )
    from toolkit.webui.app import create_app

    return create_app(root=tmp_path)


@asynccontextmanager
async def _admin_client(app: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as client:
        response = await client.post("/login", data={"password": ""}, follow_redirects=False)
        assert response.status_code == 303
        yield client


def _csrf(page: str) -> str:
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', page)
    assert match is not None
    return match.group(1)


async def test_service_management_page_is_generated_from_typed_capabilities(tmp_path: Path, monkeypatch) -> None:
    controller = ServiceController()
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        response = await client.get("/services/music-sync")

    assert response.status_code == 200
    assert controller.management_calls == [False]
    assert 'hx-get="/partials/services/music-sync/observability"' in response.text
    assert "Loading live data" in response.text
    assert 'name="setting_enabled"' in response.text
    assert 'name="setting_interval-minutes"' in response.text
    assert 'name="setting_headroom-percent"' in response.text
    assert 'value="60"' in response.text
    assert 'action="/services/music-sync/actions/sync-now"' in response.text
    assert 'action="/services/music-sync/secrets"' in response.text
    assert 'name="secret_SPOTIFY_CLIENT_SECRET"' in response.text
    assert "Start a music library sync now?" in response.text
    assert "config_path" not in response.text


async def test_service_observability_partial_collects_live_data_and_accessible_tables(
    tmp_path: Path, monkeypatch
) -> None:
    controller = ServiceController()
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        response = await client.get("/partials/services/music-sync/observability")

    assert response.status_code == 200
    assert controller.management_calls == [True]
    assert "431" in response.text
    assert "Imported tracks" in response.text
    assert "Storage backends" in response.text
    assert "ext-nas" in response.text
    assert 'scope="col"' in response.text
    assert 'data-label="Name"' in response.text


async def test_service_observability_partial_keeps_bounded_error_state(tmp_path: Path, monkeypatch) -> None:
    controller = ServiceController()
    controller.unavailable = True
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        response = await client.get("/partials/services/music-sync/observability")

    assert response.status_code == 200
    assert 'role="alert"' in response.text
    assert "temporarily unavailable" in response.text


async def test_service_settings_save_is_typed_and_queues_generation(tmp_path: Path, monkeypatch) -> None:
    controller = ServiceController()
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        page = await client.get("/services/music-sync")
        response = await client.post(
            "/services/music-sync/settings",
            data={
                "csrf_token": _csrf(page.text),
                "revision": "a" * 64,
                "setting_enabled": "on",
                "setting_interval-minutes": "30",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"].startswith("/services/music-sync?flash=")
    assert "job=job-service-action-1234" in response.headers["location"]
    assert controller.updates[0][1].values == {
        "enabled": True,
        "interval-minutes": 30,
        "headroom-percent": 130,
    }
    assert controller.jobs[0].kind.value == "CONFIG_APPLY"
    assert controller.jobs[0].operation.service == "music-sync"
    assert controller.jobs[0].operation.revision_hash == "b" * 64


async def test_service_settings_save_does_not_mutate_or_queue_when_unchanged(tmp_path: Path, monkeypatch) -> None:
    controller = ServiceController()
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        page = await client.get("/services/music-sync")
        response = await client.post(
            "/services/music-sync/settings",
            data={
                "csrf_token": _csrf(page.text),
                "revision": "a" * 64,
                "setting_enabled": "on",
                "setting_interval-minutes": "60",
                "setting_headroom-percent": "130",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "flash=No+settings+changed" in response.headers["location"]
    assert controller.updates == []
    assert controller.jobs == []


async def test_service_settings_save_updates_live_policy_without_reconciliation(tmp_path: Path, monkeypatch) -> None:
    controller = ServiceController()
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        page = await client.get("/services/music-sync")
        response = await client.post(
            "/services/music-sync/settings",
            data={
                "csrf_token": _csrf(page.text),
                "revision": "a" * 64,
                "setting_enabled": "on",
                "setting_interval-minutes": "60",
                "setting_headroom-percent": "150",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "flash=Settings+saved" in response.headers["location"]
    assert "job=" not in response.headers["location"]
    assert controller.updates[0][1].values["headroom-percent"] == 150
    assert controller.jobs == []


async def test_service_action_queues_a_durable_job(tmp_path: Path, monkeypatch) -> None:
    controller = ServiceController()
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        page = await client.get("/services/music-sync")
        response = await client.post(
            "/services/music-sync/actions/sync-now",
            data={"csrf_token": _csrf(page.text)},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "job-service-action-1234" in response.headers["location"]
    assert controller.jobs[0].operation.kind.value == "SERVICE_ACTION"
    assert controller.jobs[0].operation.action == "sync-now"


async def test_service_secret_save_is_allowlisted_and_queues_reconciliation(tmp_path: Path, monkeypatch) -> None:
    controller = ServiceController()
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        page = await client.get("/services/music-sync")
        response = await client.post(
            "/services/music-sync/secrets",
            data={
                "csrf_token": _csrf(page.text),
                "secret_SPOTIFY_CLIENT_SECRET": "new-secret",
                "secret_UNDECLARED_SECRET": "must-be-ignored",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "job=job-service-action-1234" in response.headers["location"]
    assert controller.secret_updates[0].values == {"SPOTIFY_CLIENT_SECRET": "new-secret"}
    assert controller.jobs[0].operation.kind.value == "CONFIG_APPLY"
    assert controller.jobs[0].operation.service == "music-sync"


async def test_service_management_failure_is_bounded(tmp_path: Path, monkeypatch) -> None:
    controller = ServiceController()
    controller.unavailable = True
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        response = await client.get("/services/music-sync", follow_redirects=False)

    assert response.status_code == 503
    assert response.headers.get("location") is None
    assert "temporarily unavailable" in response.text


def test_service_redirect_url_cannot_escape_same_origin() -> None:
    from toolkit.webui.routers.services import _service_url

    location = _service_url("https://attacker.invalid/path?next=//attacker.invalid", error="rejected")

    assert location.startswith("/services/")
    assert not location.startswith("//")
    assert "https://" not in location
    assert "attacker.invalid" in location
