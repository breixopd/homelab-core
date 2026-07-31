"""E2E smoke tests for Homelab Web UI pages through the ASGI boundary."""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from toolkit.controller.desired_state_api import read_machines_view, read_projects_view, read_settings_view
from toolkit.controller.inventory_api import read_services_view
from toolkit.controller.operations_api import read_operations_view
from toolkit.controller.read_models import (
    AccountView,
    BootstrapStatus,
    ContainerInventory,
    DashboardMetrics,
    DashboardView,
    DeploymentView,
    DirectoryGroupView,
    DirectoryUsersView,
    DnsView,
    InvitePreview,
    JobsView,
    PortalStatus,
    SecretInventory,
    ServiceTopology,
    ServiceVerificationCheckView,
    ServiceVerificationView,
)
from toolkit.controller.service_management_api import read_service_management

pytestmark = [pytest.mark.e2e, pytest.mark.anyio]

UI_PAGES = (
    "/",
    "/services",
    "/machines",
    "/account",
    "/deploy",
    "/jobs",
    "/projects",
    "/dns",
    "/secrets",
    "/settings",
    "/operations",
    "/people",
)


@pytest.fixture(autouse=True)
def _patch_slow_ops(monkeypatch):
    """Health report page runs live collectors — stub for e2e."""
    from toolkit.core.ops.health_report import HealthReport

    def _empty_report(root, cfg=None):
        return HealthReport(domain="test.example.com")

    monkeypatch.setattr("toolkit.core.ops.health_report.create_health_report", _empty_report)


@pytest.fixture
def e2e_app(tmp_path: Path, monkeypatch) -> FastAPI:
    (tmp_path / "config.yaml").write_text(
        "domain: test.example.com\n"
        "email: admin@test.example.com\n"
        "timezone: UTC\n"
        "services:\n"
        "  management: true\n"
        "  media: true\n"
        "  cloud: true\n"
        "  notifications: true\n"
        "  email: true\n"
        "  security: true\n"
    )
    (tmp_path / "secrets.enc.yaml").write_text("sops: {}\n")
    monkeypatch.setenv("HOMELAB_ROOT", str(tmp_path))
    monkeypatch.setenv("WEBUI_SESSION_SECRET", "e2e-test-secret")
    monkeypatch.setenv("HOMELAB_UI_WIZARD", "1")
    monkeypatch.setattr("toolkit.core.secrets.secrets.load_secrets_plaintext", lambda _path: {})

    monkeypatch.setattr(
        "toolkit.webui.routers.auth.verify_password",
        lambda _password, _client_ip, email="": (True, "Authenticated"),
    )

    class ReadController:
        verification = ServiceVerificationView(service="grafana", state="never")

        def bootstrap_status(self) -> BootstrapStatus:
            return BootstrapStatus(phase="ready")

        def invite_preview(self, _token: str) -> InvitePreview:
            return InvitePreview(
                valid=False,
                domain="test.example.com",
                secure_cookie=True,
                cookie_max_age_seconds=72 * 3600,
                sections=[],
            )

        def account_view(self, *, groups: list[str] | None = None):
            services = read_services_view(tmp_path, family=True, groups=groups or [])
            return AccountView(
                domain=services.domain,
                auth_url=f"https://auth.{services.domain}",
                sections=services.family_sections,
                tier_labels=services.tier_labels,
            )

        def services_view(self, *, family: bool = False, groups: list[str] | None = None):
            return read_services_view(tmp_path, family=family, groups=groups or [])

        def service_management(self, service: str, *, collect_status: bool = True):
            return read_service_management(tmp_path, service, collect_status=collect_status)

        def service_verification(self, _service: str) -> ServiceVerificationView:
            return self.verification

        def machines_view(self):
            return read_machines_view(tmp_path)

        def container_inventory(self) -> ContainerInventory:
            return ContainerInventory(is_available=True, unavailable_nodes=[], containers=[])

        def service_topology(self) -> ServiceTopology:
            return ServiceTopology(nodes=[], edges=[], catalog=[])

        def secret_inventory(self) -> SecretInventory:
            return SecretInventory(
                owner_email="admin@test.example.com",
                storage_mode="encrypted",
                encryption_available=True,
                entries=[],
            )

        def dashboard_view(self, **_kwargs) -> DashboardView:
            return DashboardView(
                state="config_only",
                domain="test.example.com",
                enabled_nodes=["infra", "apps", "media"],
                categories=[],
                metrics=DashboardMetrics(cpu_history=[]),
                alerts=[],
                bookmark_groups=[],
                tier_labels=[],
            )

        def dashboard_metrics(self) -> DashboardMetrics:
            return DashboardMetrics(cpu_history=[])

        def portal_status(self) -> PortalStatus:
            return PortalStatus(
                checked_at=datetime.now().astimezone(),
                complete=True,
                unavailable_nodes=0,
                services={},
            )

        def jobs(self, *, limit: int = 100) -> JobsView:
            assert limit == 100
            return JobsView(jobs=[], queued=0, running=0, attention=0, succeeded=0)

        def operations_view(self):
            return read_operations_view(tmp_path)

        def directory_users(self) -> DirectoryUsersView:
            return DirectoryUsersView(
                domain="test.example.com",
                users=[],
                group_options=[
                    DirectoryGroupView(
                        name="homelab-media",
                        label="Media",
                        description="Media services",
                        is_default=True,
                    ),
                    DirectoryGroupView(
                        name="homelab-cloud",
                        label="Cloud",
                        description="Cloud services",
                        is_default=True,
                    ),
                    DirectoryGroupView(
                        name="homelab-admin",
                        label="Administration",
                        description="Operator access",
                        is_default=False,
                    ),
                ],
                invites_enabled=False,
                invite_disabled_reason="Invitation delivery is not configured.",
            )

        def deployment_view(self) -> DeploymentView:
            return DeploymentView(
                state="config_only",
                enabled_targets=["infra", "apps", "media"],
                node_count=3,
                total_services=0,
                category_count=0,
                generated_config_count=0,
                step_labels={},
                preflight=[],
                preflight_ok=False,
                active_jobs=[],
            )

        def dns_view(self) -> DnsView:
            return DnsView(
                revision="a" * 64,
                public_ip="1.2.3.4",
                ip_source="config",
                records=[],
                has_cloudflare_credentials=False,
            )

        def settings_view(self):
            return read_settings_view(tmp_path)

        def projects_view(self):
            return read_projects_view(tmp_path)

        def close(self) -> None:
            return None

    controller = ReadController()
    monkeypatch.setattr("toolkit.webui.app.controller_client_from_environment", lambda: controller)
    from toolkit.webui.app import create_app

    return create_app(root=tmp_path)


@pytest.fixture
async def e2e_client(e2e_app: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=e2e_app), base_url="https://testserver") as client:
        yield client


async def _login(client: AsyncClient) -> None:
    response = await client.post("/login", data={"password": ""}, follow_redirects=False)
    assert response.status_code == 303


@pytest.mark.parametrize("path", UI_PAGES)
async def test_ui_page_loads_after_login(e2e_client: AsyncClient, path: str) -> None:
    await _login(e2e_client)
    response = await e2e_client.get(path)
    assert response.status_code == 200, f"{path} returned {response.status_code}"


async def test_overview_renders_all_resource_histories(e2e_client: AsyncClient) -> None:
    await _login(e2e_client)
    response = await e2e_client.get("/")

    assert response.status_code == 200
    for metric in ("cpu", "memory", "disk"):
        assert f'id="{metric}-history"' in response.text
        assert f"data-{metric}-history=" in response.text
    assert "/static/css/main.css?v=" in response.text
    assert "/static/js/dashboard.js?v=" in response.text


async def test_managed_host_form_explains_bootstrap_and_ssh_contract(e2e_client: AsyncClient) -> None:
    await _login(e2e_client)
    response = await e2e_client.get("/operations")

    assert response.status_code == 200
    assert "Reachable management IPv4" in response.text
    assert "no Headscale address is needed" in response.text
    assert "ssh.key_file" in response.text
    assert "Add and bootstrap" in response.text


@pytest.mark.parametrize("status", ["pass", "fail", "not_applicable", "degraded", "not_ready"])
async def test_service_page_renders_each_typed_verification_status(
    e2e_app: FastAPI,
    e2e_client: AsyncClient,
    status: str,
) -> None:
    e2e_app.state.controller.verification = ServiceVerificationView(
        service="grafana",
        state="complete",
        overall_status=status,
        checks=[
            ServiceVerificationCheckView(
                service="grafana",
                check="health",
                status=status,
                detail="bounded evidence",
            )
        ],
        observed_at=datetime.now().astimezone(),
    )
    await _login(e2e_client)

    response = await e2e_client.get("/services/grafana")

    assert response.status_code == 200
    assert f'data-status="{status}"' in response.text
    assert f'aria-label="Verification result: {status.replace("_", " ")}"' in response.text
    assert "bounded evidence" in response.text


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ("never", "No verification has been run yet."),
        ("queued", "Waiting for the first result."),
        ("running", "Waiting for the first result."),
    ],
)
async def test_service_page_renders_nonterminal_verification_states(
    e2e_app: FastAPI,
    e2e_client: AsyncClient,
    state: str,
    expected: str,
) -> None:
    e2e_app.state.controller.verification = ServiceVerificationView(service="grafana", state=state)
    await _login(e2e_client)

    response = await e2e_client.get("/services/grafana")

    assert response.status_code == 200
    assert expected in response.text


async def test_invite_activate_public_without_token(e2e_client: AsyncClient) -> None:
    response = await e2e_client.get("/invite/activate")
    assert response.status_code == 200
    assert "Invalid invite" in response.text or "Invite link problem" in response.text


async def test_setup_is_closed_after_initialization(e2e_client: AsyncClient) -> None:
    await _login(e2e_client)
    response = await e2e_client.get("/setup")
    assert response.status_code == 403


async def test_portal_status_feed_returns_bounded_runtime_state(
    e2e_app: FastAPI,
    e2e_client: AsyncClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        e2e_app.state.controller,
        "portal_status",
        lambda: PortalStatus(
            checked_at=datetime.now().astimezone(),
            complete=True,
            unavailable_nodes=0,
            services={"prometheus": "online"},
        ),
    )
    await _login(e2e_client)
    response = await e2e_client.get("/api/portal/status")

    assert response.status_code == 200
    payload = response.json()
    datetime.fromisoformat(payload.pop("checked_at").replace("Z", "+00:00"))
    assert payload == {
        "complete": True,
        "unavailable_nodes": 0,
        "services": {"prometheus": "online"},
    }
