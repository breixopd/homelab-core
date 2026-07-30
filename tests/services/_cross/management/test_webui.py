from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from toolkit.controller.read_models import (
    DashboardCategory,
    DashboardMetrics,
    DashboardView,
    ServiceTopology,
)
from toolkit.webui.auth import _linux_default_gateway_cidrs


@pytest.fixture
def webui_app(tmp_path: Path, monkeypatch) -> FastAPI:
    config = tmp_path / "config.yaml"
    config.write_text(
        "domain: test.example.com\n"
        "email: admin@test.example.com\n"
        "timezone: UTC\n"
        "services:\n"
        "  management: true\n"
        "  media: false\n"
        "  cloud: false\n"
        "  notifications: false\n"
        "  email: false\n"
        "  security: false\n"
    )
    monkeypatch.setenv("HOMELAB_ROOT", str(tmp_path))
    monkeypatch.setenv("WEBUI_SESSION_SECRET", "test-secret-for-webui-tests-only")
    monkeypatch.setenv("HOMELAB_UI_WIZARD", "1")

    from toolkit.webui.app import create_app

    monkeypatch.setattr(
        "toolkit.webui.routers.auth.verify_password",
        lambda _password, _client_ip, email="": (True, "Authenticated"),
    )
    app = create_app(root=tmp_path)
    app.state.controller = type(
        "ReadController",
        (),
        {
            "service_topology": lambda _self: ServiceTopology(nodes=[], edges=[], catalog=[]),
            "dashboard_view": lambda _self, **_kwargs: DashboardView(
                state="config_only",
                domain="test.example.com",
                enabled_nodes=["infra"],
                categories=[DashboardCategory(name="Management", node="infra", services=["caddy"])],
                total_services=1,
                metrics=DashboardMetrics(cpu_history=[]),
                alerts=[],
                bookmark_groups=[],
                tier_labels=[],
            ),
            "dashboard_metrics": lambda _self: DashboardMetrics(cpu_history=[]),
            "health": lambda _self: SimpleNamespace(status="ok"),
        },
    )()
    return app


@pytest.fixture
async def webui_client(webui_app: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=webui_app), base_url="https://testserver") as client:
        yield client


def test_create_app_factory(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("WEBUI_SESSION_SECRET", "test-secret")
    from toolkit.webui import create_app, current_root, init_webui

    app = create_app(root=tmp_path)
    assert app.title == "Homelab Toolkit"
    assert app.state.controller is not None
    assert current_root() == tmp_path.resolve()
    assert init_webui(tmp_path) == tmp_path.resolve()


def test_session_secret_file_is_atomic_persistent_and_owner_only(tmp_path: Path, monkeypatch) -> None:
    from toolkit.webui.app import _session_secret

    secret_path = tmp_path / "state" / "webui-secret"
    monkeypatch.delenv("WEBUI_SESSION_SECRET", raising=False)
    monkeypatch.setenv("WEBUI_SESSION_SECRET_FILE", str(secret_path))

    first = _session_secret()
    second = _session_secret()

    assert first == second
    assert len(first) == 64
    assert secret_path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize(
    ("configured", "expected"),
    [(None, True), ("true", True), ("1", True), ("false", False), ("0", False)],
)
def test_secure_cookie_mode_is_explicit_and_defaults_closed(monkeypatch, configured, expected) -> None:
    from toolkit.webui.app import _secure_session_cookies

    if configured is None:
        monkeypatch.delenv("WEBUI_SECURE_COOKIES", raising=False)
    else:
        monkeypatch.setenv("WEBUI_SECURE_COOKIES", configured)

    assert _secure_session_cookies() is expected


def test_secure_cookie_mode_rejects_invalid_configuration(monkeypatch) -> None:
    from toolkit.webui.app import _secure_session_cookies

    monkeypatch.setenv("WEBUI_SECURE_COOKIES", "maybe")

    with pytest.raises(RuntimeError, match="WEBUI_SECURE_COOKIES"):
        _secure_session_cookies()


def test_linux_default_gateway_cidrs_are_exact_and_require_gateway_flag() -> None:
    route_table = (
        "Iface Destination Gateway Flags RefCnt Use Metric Mask MTU Window IRTT\n"
        "eth0 00000000 D1271FAC 0003 0 0 0 00000000 0 0 0\n"
        "eth1 00000000 00000000 0001 0 0 0 00000000 0 0 0\n"
        "eth0 00271FAC 00000000 0001 0 0 0 F0FFFFFF 0 0 0\n"
        "broken route\n"
    )

    assert tuple(map(str, _linux_default_gateway_cidrs(route_table))) == ("172.31.39.209/32",)


@pytest.mark.anyio
async def test_login_page_returns_200(webui_client: AsyncClient):
    response = await webui_client.get("/login")
    assert response.status_code == 200
    assert "Sign in" in response.text


@pytest.mark.anyio
async def test_dashboard_redirects_to_login_when_unauthenticated(webui_client: AsyncClient):
    response = await webui_client.get("/", follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"] == "/login"


@pytest.mark.anyio
async def test_dashboard_accessible_after_login(webui_client: AsyncClient):
    login = await webui_client.post("/login", data={"password": ""}, follow_redirects=False)
    assert login.status_code == 303
    response = await webui_client.get("/")
    assert response.status_code == 200
    assert "<h1>Overview</h1>" in response.text
    assert "Services" in response.text
    assert "1 configured" in response.text


@pytest.mark.anyio
async def test_health_endpoint(webui_client: AsyncClient, webui_app: FastAPI):
    response = await webui_client.get("/health")
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json() == {"status": "ok", "controller": "ok"}
    assert str(webui_app.state.homelab_root) not in response.text


# --- Phase U: graph + catalog endpoints -----------------------------------


async def _login_as_admin(webui_client: AsyncClient) -> None:
    """Wizard-bypass login so the admin-only /api/ routes are reachable."""
    await webui_client.post("/login", data={"password": ""}, follow_redirects=False)


@pytest.mark.anyio
async def test_service_graph_endpoint(webui_client: AsyncClient):
    await _login_as_admin(webui_client)
    response = await webui_client.get("/api/services/graph")
    assert response.status_code == 200
    data = response.json()
    assert "nodes" in data
    assert "edges" in data
    # No compose file in the tmp root → empty graph, but the shape is valid.
    assert isinstance(data["nodes"], list)
    assert isinstance(data["edges"], list)


@pytest.mark.anyio
async def test_service_catalog_endpoint(webui_client: AsyncClient):
    await _login_as_admin(webui_client)
    response = await webui_client.get("/api/services/catalog")
    assert response.status_code == 200
    catalog = response.json()
    assert isinstance(catalog, list)


# NOTE: a 'requires admin' test is intentionally omitted here — the /api/ prefix
# is admin-gated by AuthMiddleware (rbac.py:28 ADMIN_ONLY_PREFIXES), and the
# exact unauthenticated denial path (303/401/500) is pre-existing RBAC behavior
# covered by test_webui_rbac.py, not Phase U. The two positive tests above prove
# the graph + catalog endpoints work for authenticated admins.
