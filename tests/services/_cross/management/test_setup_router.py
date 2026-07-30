from __future__ import annotations

import re
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from toolkit.controller.client import ControllerUnavailableError
from toolkit.controller.read_models import (
    BootstrapInitializeRequest,
    BootstrapInitializeResult,
    BootstrapServiceSecretView,
    BootstrapServiceSettingView,
    BootstrapSessionGrant,
    BootstrapStatus,
    BootstrapView,
    DeploymentView,
)

pytestmark = pytest.mark.anyio


class BootstrapController:
    def __init__(self, phase: str = "uninitialized") -> None:
        self.phase = phase
        self.exchanged_capability = ""
        self.initialize_request: BootstrapInitializeRequest | None = None

    def bootstrap_status(self) -> BootstrapStatus:
        return BootstrapStatus(phase=self.phase)

    def exchange_bootstrap_capability(self, token: str) -> BootstrapSessionGrant:
        self.exchanged_capability = token
        return BootstrapSessionGrant(
            session_token="00000000-0000-4000-8000-000000000000.session-secret-value",
            expires_at="2026-07-10T12:15:00Z",
        )

    def bootstrap_view(self, _session_token: str) -> BootstrapView:
        return BootstrapView(
            status=BootstrapStatus(phase="uninitialized", has_active_session=True),
            categories=[],
            service_settings=[
                BootstrapServiceSettingView(
                    service="media-library",
                    service_label="Media library",
                    key="server",
                    label="Media servers",
                    description="Select the media server implementation.",
                    type="select",
                    default="jellyfin",
                    choices=["jellyfin", "plex", "both"],
                ),
                BootstrapServiceSettingView(
                    service="music-sync",
                    service_label="Music sync",
                    key="enabled",
                    label="Enabled",
                    description="Import configured music sources.",
                    type="boolean",
                    default=True,
                ),
            ],
            service_secrets=[
                BootstrapServiceSecretView(
                    service="music-sync",
                    name="SPOTIFY_CLIENT_ID",
                    label="Spotify client ID",
                    description="Spotify application client ID.",
                    input="text",
                    required=False,
                )
            ],
        )

    def initialize_bootstrap(self, request: BootstrapInitializeRequest) -> BootstrapInitializeResult:
        self.initialize_request = request
        return BootstrapInitializeResult(
            config_revision="a" * 64,
            configured_secret_names=sorted(request.credential_values),
        )

    def deployment_view(self) -> DeploymentView:
        return DeploymentView(
            state="ready",
            enabled_targets=["infra", "apps", "media"],
            node_count=3,
            total_services=0,
            category_count=0,
            generated_config_count=3,
            step_labels={},
            preflight=[],
            preflight_ok=True,
            active_jobs=[],
        )

    def close(self) -> None:
        return None


def _csrf(response_text: str) -> str:
    match = re.search(r'name="csrf_token" value="([^"]+)"', response_text)
    assert match is not None
    return match.group(1)


def _create_app(tmp_path: Path, monkeypatch, controller: BootstrapController) -> FastAPI:
    monkeypatch.setenv("WEBUI_SESSION_SECRET", "setup-router-test-secret")
    monkeypatch.setattr("toolkit.webui.app.controller_client_from_environment", lambda: controller)
    from toolkit.webui.app import create_app

    return create_app(root=tmp_path)


def _client(app: FastAPI) -> AsyncClient:
    return AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver", follow_redirects=False)


async def test_setup_session_cookie_works_over_loopback_http(tmp_path: Path, monkeypatch) -> None:
    controller = BootstrapController()
    monkeypatch.setenv("WEBUI_SECURE_COOKIES", "false")
    app = _create_app(tmp_path, monkeypatch, controller)

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://127.0.0.1:8080",
        follow_redirects=False,
    ) as client:
        gate = await client.get("/setup")
        assert "secure" not in gate.headers["set-cookie"].lower()

        exchanged = await client.post(
            "/setup/session",
            data={
                "csrf_token": _csrf(gate.text),
                "capability": "10000000-0000-4000-8000-000000000000.capability-secret-value",
            },
        )
        wizard = await client.get("/setup")

    assert exchanged.status_code == 303
    assert "Owner password" in wizard.text


async def test_setup_exchanges_capability_then_initializes_through_controller(tmp_path: Path, monkeypatch) -> None:
    controller = BootstrapController()
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _client(app) as client:
        gate = await client.get("/setup")
        assert gate.status_code == 200
        assert "Setup capability" in gate.text
        assert str(tmp_path) not in gate.text
        assert "secure" in gate.headers["set-cookie"].lower()
        assert gate.headers["cache-control"] == "no-store"
        assert gate.headers["referrer-policy"] == "no-referrer"
        assert "frame-ancestors 'none'" in gate.headers["content-security-policy"]
        assert "unsafe-inline" not in gate.headers["content-security-policy"]
        assert "fonts.googleapis.com" not in gate.text
        csrf = _csrf(gate.text)

        capability = "10000000-0000-4000-8000-000000000000.capability-secret-value"
        exchanged = await client.post(
            "/setup/session",
            data={"csrf_token": csrf, "capability": capability},
        )
        assert exchanged.status_code == 303
        assert exchanged.headers["location"] == "/setup"
        assert exchanged.headers["cache-control"] == "no-store"
        assert capability not in exchanged.headers["location"]
        assert capability not in exchanged.text

        wizard = await client.get("/setup")
        assert wizard.status_code == 200
        assert "Owner password" in wizard.text
        assert '<script src="/static/js/setup.js?v=' in wizard.text
        assert "(function ()" not in wizard.text
        assert "ssh_public_key" not in wizard.text
        csrf = _csrf(wizard.text)

        initialized = await client.post(
            "/setup",
            data={
                "csrf_token": csrf,
                "domain": "home.example.com",
                "email": "operator@example.com",
                "cloudflare_api_token": "cloudflare-token-value-0123456789",
                "cloudflare_zone_id": "0123456789abcdef0123456789abcdef",
                "proxmox_api_token_id": "root@pam!homelab",
                "proxmox_api_token_secret": "proxmox-token-value-0123456789",
                "owner_password": "correct horse battery staple",
                "owner_password_confirm": "correct horse battery staple",
                "proxmox_api_url": "https://192.0.2.10:8006",
                "proxmox_node": "pve",
                "proxmox_storage": "local-zfs",
                "timezone": "Europe/Madrid",
                "service_setting__media-library__server": "plex",
                "bootstrap_secret__SPOTIFY_CLIENT_ID": "spotify-client-id",
            },
        )
        deploy_status = await client.get("/deploy/status")
        settings = await client.get("/settings")
        unknown_job_id = "00000000-0000-4000-8000-000000000099"
        unknown_stream = await client.get(f"/deploy/stream/{unknown_job_id}")
        deploy_page = await client.get("/deploy")
        page_csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', deploy_page.text)
        assert page_csrf is not None
        unknown_cancel = await client.post(
            f"/deploy/jobs/{unknown_job_id}/cancel",
            headers={"x-csrf-token": page_csrf.group(1)},
        )

    assert initialized.status_code == 303
    assert initialized.headers["location"] == "/deploy?setup=1"
    assert controller.exchanged_capability == capability
    assert controller.initialize_request is not None
    assert controller.initialize_request.desired_state.service_settings == {
        "media-library": {"server": "plex"},
        "music-sync": {"enabled": False},
    }
    assert controller.initialize_request.credential_values["SSO_USER_PASSWORD"] == "correct horse battery staple"
    assert controller.initialize_request.credential_values["SPOTIFY_CLIENT_ID"] == "spotify-client-id"
    assert "correct horse battery staple" not in initialized.text

    assert deploy_status.status_code == 200
    assert settings.status_code == 303
    assert settings.headers["location"] == "/login"
    assert unknown_stream.status_code == 404
    assert unknown_cancel.status_code == 404

    async with _client(app) as scoped_client:
        scoped_client.cookies.update(client.cookies)
        future_deploy_route = await scoped_client.get("/deploy/secrets")
    assert future_deploy_route.status_code == 303
    assert future_deploy_route.headers["location"] == "/login"

    monkeypatch.setattr("toolkit.webui.auth.time.time", lambda: 9_000_000_000)
    async with _client(app) as expired_client:
        expired_client.cookies.update(client.cookies)
        expired_deploy = await expired_client.get("/deploy/status")
    assert expired_deploy.status_code == 303
    assert expired_deploy.headers["location"] == "/login"


async def test_bootstrap_deploy_session_slides_but_has_an_absolute_limit(tmp_path: Path, monkeypatch) -> None:
    real_now = int(__import__("time").time())
    controller = BootstrapController()
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _client(app) as client:
        gate = await client.get("/setup")
        await client.post(
            "/setup/session",
            data={"csrf_token": _csrf(gate.text), "capability": "a" * 40},
        )
        wizard = await client.get("/setup")
        initialized = await client.post(
            "/setup",
            data={
                "csrf_token": _csrf(wizard.text),
                "domain": "home.example.com",
                "email": "operator@example.com",
                "cloudflare_api_token": "cloudflare-token-value-0123456789",
                "cloudflare_zone_id": "0123456789abcdef0123456789abcdef",
                "proxmox_api_token_id": "root@pam!homelab",
                "proxmox_api_token_secret": "proxmox-token-value-0123456789",
                "owner_password": "correct horse battery staple",
                "owner_password_confirm": "correct horse battery staple",
                "proxmox_api_url": "https://192.0.2.10:8006",
            },
        )
        assert initialized.status_code == 303

        monkeypatch.setattr("toolkit.webui.auth.time.time", lambda: real_now + 20 * 60)
        first_activity = await client.get("/deploy/status")
        monkeypatch.setattr("toolkit.webui.auth.time.time", lambda: real_now + 40 * 60)
        second_activity = await client.get("/deploy/status")
        monkeypatch.setattr("toolkit.webui.auth.time.time", lambda: real_now + 4 * 60 * 60 + 1)
        over_absolute_limit = await client.get("/deploy/status")

    assert first_activity.status_code == 200
    assert second_activity.status_code == 200
    assert over_absolute_limit.status_code == 303


async def test_setup_rejects_password_confirmation_without_sending_credentials(tmp_path: Path, monkeypatch) -> None:
    controller = BootstrapController()
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _client(app) as client:
        gate = await client.get("/setup")
        await client.post(
            "/setup/session",
            data={"csrf_token": _csrf(gate.text), "capability": "a" * 40},
        )
        wizard = await client.get("/setup")
        response = await client.post(
            "/setup",
            data={
                "csrf_token": _csrf(wizard.text),
                "owner_password": "correct horse battery staple",
                "owner_password_confirm": "different password value",
            },
        )

    assert response.status_code == 400
    assert "Passwords do not match" in response.text
    assert "correct horse battery staple" not in response.text
    assert controller.initialize_request is None


async def test_setup_reports_terminal_controller_phases_without_leaking_paths(tmp_path: Path, monkeypatch) -> None:
    for phase, expected_status, expected_text in (
        ("ready", 403, "Setup complete"),
        ("recovery_required", 409, "Recovery required"),
    ):
        controller = BootstrapController(phase)
        app = _create_app(tmp_path, monkeypatch, controller)
        async with _client(app) as client:
            response = await client.get("/setup")
        assert response.status_code == expected_status
        assert expected_text in response.text
        assert str(tmp_path) not in response.text


async def test_setup_reconciles_ready_state_after_initialization_response_loss(tmp_path: Path, monkeypatch) -> None:
    class ResponseLossController(BootstrapController):
        def initialize_bootstrap(self, request: BootstrapInitializeRequest) -> BootstrapInitializeResult:
            self.initialize_request = request
            self.phase = "ready"
            raise ControllerUnavailableError

    controller = ResponseLossController()
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _client(app) as client:
        gate = await client.get("/setup")
        await client.post(
            "/setup/session",
            data={"csrf_token": _csrf(gate.text), "capability": "a" * 40},
        )
        wizard = await client.get("/setup")
        response = await client.post(
            "/setup",
            data={
                "csrf_token": _csrf(wizard.text),
                "domain": "home.example.com",
                "email": "operator@example.com",
                "cloudflare_api_token": "cloudflare-token-value-0123456789",
                "cloudflare_zone_id": "0123456789abcdef0123456789abcdef",
                "proxmox_api_token_id": "root@pam!homelab",
                "proxmox_api_token_secret": "proxmox-token-value-0123456789",
                "owner_password": "correct horse battery staple",
                "owner_password_confirm": "correct horse battery staple",
                "proxmox_api_url": "https://192.0.2.10:8006",
                "proxmox_node": "pve",
                "proxmox_storage": "local-zfs",
                "timezone": "Europe/Madrid",
                "service_setting__media-library__server": "jellyfin",
            },
        )

    assert response.status_code == 303
    assert response.headers["location"] == "/deploy?setup=1"


async def test_setup_reconciles_delayed_commit_on_next_status_read(tmp_path: Path, monkeypatch) -> None:
    class DelayedCommitController(BootstrapController):
        def initialize_bootstrap(self, request: BootstrapInitializeRequest) -> BootstrapInitializeResult:
            self.initialize_request = request
            raise ControllerUnavailableError

    controller = DelayedCommitController()
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _client(app) as client:
        gate = await client.get("/setup")
        await client.post(
            "/setup/session",
            data={"csrf_token": _csrf(gate.text), "capability": "a" * 40},
        )
        wizard = await client.get("/setup")
        lost_response = await client.post(
            "/setup",
            data={
                "csrf_token": _csrf(wizard.text),
                "domain": "home.example.com",
                "email": "operator@example.com",
                "cloudflare_api_token": "cloudflare-token-value-0123456789",
                "cloudflare_zone_id": "0123456789abcdef0123456789abcdef",
                "proxmox_api_token_id": "root@pam!homelab",
                "proxmox_api_token_secret": "proxmox-token-value-0123456789",
                "owner_password": "correct horse battery staple",
                "owner_password_confirm": "correct horse battery staple",
                "proxmox_api_url": "https://192.0.2.10:8006",
                "proxmox_node": "pve",
                "proxmox_storage": "local-zfs",
                "timezone": "Europe/Madrid",
                "service_setting__media-library__server": "jellyfin",
            },
        )
        assert lost_response.status_code == 400

        controller.phase = "ready"
        reconciled = await client.get("/setup")

    assert reconciled.status_code == 303
    assert reconciled.headers["location"] == "/deploy?setup=1"
