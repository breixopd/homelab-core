from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from toolkit.core.config.config import Config
from toolkit.core.ops.family_portal import family_portal_groups
from toolkit.webui.rbac import family_route_allowed, homelab_tier_groups, is_toolkit_admin


@pytest.fixture
def portal_app(tmp_path: Path, monkeypatch) -> FastAPI:
    (tmp_path / "config.yaml").write_text(
        "domain: test.example.com\n"
        "email: admin@test.example.com\n"
        "timezone: UTC\n"
        "services:\n"
        "  management: true\n"
        "  media: true\n"
        "  cloud: true\n"
    )
    monkeypatch.setenv("HOMELAB_ROOT", str(tmp_path))
    monkeypatch.setenv("WEBUI_SESSION_SECRET", "test")
    from toolkit.webui.app import create_app

    return create_app(root=tmp_path)


@pytest.fixture
async def portal_client(portal_app: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=portal_app), base_url="https://testserver") as client:
        yield client


def test_family_portal_groups_media_only():
    cfg = Config(domain="example.com", services={"media": True, "cloud": False})
    groups = family_portal_groups(cfg, ["homelab-media"])
    names = [g.name for g in groups]
    assert "Media" in names
    assert "Cloud" not in names
    assert all("sonarr" not in item.href for g in groups for item in g.items)


def test_family_users_can_read_the_minimal_portal_status_feed() -> None:
    assert family_route_allowed("GET", "/api/portal/status") is True
    assert family_route_allowed("POST", "/api/portal/status") is False


@pytest.mark.anyio
@pytest.mark.parametrize("path", ["/deploy", "/jobs", "/machines"])
async def test_family_user_blocked_from_operator_routes(portal_client: AsyncClient, path: str):
    portal_client.cookies.set("homelab_webui", "x")
    with (
        patch(
            "toolkit.webui.auth.authelia_user",
            return_value="family@example.com",
        ),
        patch(
            "toolkit.webui.rbac.authelia_user",
            return_value="family@example.com",
        ),
        patch(
            "toolkit.webui.rbac.authelia_groups",
            return_value=["homelab-media", "homelab-cloud"],
        ),
        patch(
            "toolkit.webui.auth.is_authenticated",
            return_value=True,
        ),
    ):
        response = await portal_client.get(path, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/"


def test_admin_groups_recognized():
    from starlette.requests import Request

    scope = {"type": "http", "headers": [], "client": ("127.0.0.1", 1234)}
    request = Request(scope)
    with (
        patch("toolkit.webui.rbac.authelia_user", return_value="owner"),
        patch(
            "toolkit.webui.rbac.authelia_groups",
            return_value=["homelab-admin", "lldap_password_manager"],
        ),
    ):
        assert is_toolkit_admin(request) is True


def test_tier_groups_include_only_manifest_defined_access_groups() -> None:
    from starlette.requests import Request

    request = Request({"type": "http", "headers": [], "client": ("127.0.0.1", 1234)})
    with patch(
        "toolkit.webui.rbac.authelia_groups",
        return_value=["homelab-users", "homelab-admin", "homelab-media", "lldap_admin"],
    ):
        assert homelab_tier_groups(request) == ["homelab-admin", "homelab-media"]
