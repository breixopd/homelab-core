from __future__ import annotations

import re
from contextlib import asynccontextmanager
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from toolkit.controller.client import ControllerClientError
from toolkit.controller.read_models import (
    DnsView,
    SecretInventory,
    SecretMutationResult,
    SecretStatus,
)

pytestmark = pytest.mark.anyio


class SettingsController:
    def __init__(self) -> None:
        self.secret_error = False
        self.inventory = SecretInventory(
            owner_email="owner@example.com",
            storage_mode="encrypted",
            encryption_available=True,
            entries=[
                SecretStatus(
                    name="CLOUDFLARE_API_TOKEN",
                    isConfigured=True,
                    tier="user",
                    description="Cloudflare API token",
                ),
                SecretStatus(
                    name="POSTGRES_PASSWORD",
                    isConfigured=True,
                    tier="gen",
                    description="Postgres password",
                ),
            ],
        )

    def secret_inventory(self) -> SecretInventory:
        if self.secret_error:
            raise ControllerClientError("unavailable")
        return self.inventory

    def update_secrets(self, _request) -> SecretMutationResult:
        return SecretMutationResult(changed_names=["CLOUDFLARE_API_TOKEN"], inventory=self.inventory)

    def generate_secrets(self) -> SecretMutationResult:
        return SecretMutationResult(changed_names=["POSTGRES_PASSWORD"], inventory=self.inventory)

    def rotate_secrets(self) -> SecretMutationResult:
        return SecretMutationResult(changed_names=["POSTGRES_PASSWORD"], inventory=self.inventory)

    def dns_view(self) -> DnsView:
        return DnsView(
            revision="a" * 64,
            public_ip="1.2.3.4",
            ip_source="config",
            records=[],
            has_cloudflare_credentials=True,
        )

    def close(self) -> None:
        return None


def _create_app(tmp_path: Path, monkeypatch, controller: SettingsController) -> FastAPI:
    (tmp_path / "config.yaml").write_text("domain: test.example.com\nemail: owner@example.com\n", encoding="utf-8")
    monkeypatch.setenv("WEBUI_SESSION_SECRET", "network-secrets-router-test-secret")
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


async def test_network_and_secrets_render_redirect_feedback(tmp_path: Path, monkeypatch) -> None:
    app = _create_app(tmp_path, monkeypatch, SettingsController())
    async with _admin_client(app) as client:
        network = await client.get("/dns?flash=Public+IP+saved&error=Example+error")
        secrets = await client.get("/secrets?flash=Credentials+saved&error=Example+error")

    assert network.status_code == 200
    assert "Public IP saved" in network.text
    assert "Example error" in network.text
    assert "<h1>Network</h1>" in network.text
    assert secrets.status_code == 200
    assert "Credentials saved" in secrets.text
    assert "Example error" in secrets.text


async def test_secret_inventory_failure_is_bounded_instead_of_redirecting_to_itself(
    tmp_path: Path,
    monkeypatch,
) -> None:
    controller = SettingsController()
    controller.secret_error = True
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        response = await client.get("/secrets", follow_redirects=False)

    assert response.status_code == 503
    assert response.headers.get("location") is None
    assert "temporarily unavailable" in response.text


async def test_secret_mutations_return_visible_change_counts(tmp_path: Path, monkeypatch) -> None:
    app = _create_app(tmp_path, monkeypatch, SettingsController())
    async with _admin_client(app) as client:
        page = await client.get("/secrets")
        match = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)
        assert match is not None
        csrf = match.group(1)
        no_change = await client.post("/secrets/save", data={"csrf_token": csrf}, follow_redirects=False)
        generated = await client.post(
            "/secrets/generate",
            data={"csrf_token": csrf},
            follow_redirects=False,
        )

    assert no_change.status_code == 303
    assert "flash=No+credential+changes+submitted" in no_change.headers["location"]
    assert generated.status_code == 303
    assert "flash=Generated+1+secret" in generated.headers["location"]
