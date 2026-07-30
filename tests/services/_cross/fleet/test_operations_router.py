from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from toolkit.controller.contracts import JobRecord, JobState
from toolkit.controller.read_models import (
    BackupOperationsView,
    MaintenanceOperationsView,
    ManagedHostIntegrationFieldChoice,
    ManagedHostServiceChoice,
    ManagedHostsView,
    ManagedHostView,
    OperationsView,
    UpdateCandidateView,
    UpdateOperationsView,
)

pytestmark = pytest.mark.anyio


class OperationsController:
    def __init__(self) -> None:
        self.jobs = []
        self.created = []
        self.updated = []

    def operations_view(self) -> OperationsView:
        return OperationsView(
            maintenance=MaintenanceOperationsView(daily_at="03:00"),
            backups=BackupOperationsView(enabled=True, target="remote", storage_host="nas-01"),
            dumps=[],
            hosts=ManagedHostsView(
                revision="a" * 64,
                hosts=[
                    ManagedHostView(
                        fingerprint="b" * 64,
                        name="nas-01",
                        ip="192.0.2.40",
                        kind="fleet",
                        ssh_user="root",
                        ssh_port=22,
                        cluster_group="storage",
                        lldap_email="ops@example.test",
                        headscale_tags=["tag:storage"],
                        reconciled=False,
                        last_reconcile_at=None,
                        services=["monitoring-agent", "media-cache"],
                        applied_services=[],
                        integrations={"media-cache": {"path": "/srv/media"}},
                    )
                ],
                service_choices=[
                    ManagedHostServiceChoice(
                        name="monitoring-agent",
                        label="Monitoring",
                        default_for_plain=True,
                        default_for_fleet=True,
                        fleet_only=False,
                    ),
                    ManagedHostServiceChoice(
                        name="media-cache",
                        label="Media cache",
                        default_for_plain=False,
                        default_for_fleet=False,
                        fleet_only=False,
                        fields=[
                            ManagedHostIntegrationFieldChoice(
                                key="path",
                                label="Storage path",
                                description="Absolute media path",
                                type="path",
                                required=True,
                                placeholder="/srv/media",
                            )
                        ],
                    ),
                    ManagedHostServiceChoice(
                        name="ldap-client",
                        label="LDAP SSH",
                        default_for_plain=False,
                        default_for_fleet=True,
                        fleet_only=True,
                    ),
                ],
            ),
            updates=UpdateOperationsView(
                available=True,
                reason="1 compatible update ready for review",
                revision="c" * 64,
                candidates=[
                    UpdateCandidateView(
                        service="redis",
                        current="8.8.0-alpine",
                        target="8.9.0-alpine",
                        changelog_url="https://example.test/redis",
                    )
                ],
                active_revision="d" * 64,
                rollback_available=True,
            ),
        )

    def create_managed_host(self, request):
        self.created.append(request)
        return self.operations_view().hosts

    def update_managed_host(self, name, request):
        self.updated.append((name, request))
        return self.operations_view().hosts

    def submit(self, request) -> JobRecord:
        self.jobs.append(request)
        now = datetime(2026, 7, 11, tzinfo=UTC)
        return JobRecord(
            job_id=f"job-{request.kind.value.lower()}",
            request=request,
            state=JobState.QUEUED,
            actor="ui:homelab-ui",
            created_at=now,
            updated_at=now,
        )

    def close(self) -> None:
        return None


def _app(tmp_path: Path, monkeypatch, controller: OperationsController) -> FastAPI:
    (tmp_path / "config.yaml").write_text("domain: test.example.com\n", encoding="utf-8")
    monkeypatch.setenv("WEBUI_SESSION_SECRET", "operations-router-test-secret-value")
    monkeypatch.setattr("toolkit.webui.app.controller_client_from_environment", lambda: controller)
    monkeypatch.setattr("toolkit.webui.routers.auth.verify_password", lambda *_args, **_kwargs: (True, "ok"))
    from toolkit.webui.app import create_app

    return create_app(root=tmp_path)


@asynccontextmanager
async def _client(app: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as client:
        login = await client.post("/login", data={"password": ""}, follow_redirects=False)
        assert login.status_code == 303
        yield client


def _csrf(html: str) -> str:
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    assert match
    return match.group(1)


async def test_operations_page_lists_managed_hosts_and_update_controls(tmp_path: Path, monkeypatch) -> None:
    app = _app(tmp_path, monkeypatch, OperationsController())
    async with _client(app) as client:
        response = await client.get("/operations")

    assert response.status_code == 200
    assert "nas-01" in response.text
    assert "8.9.0-alpine" in response.text
    assert 'action="/operations/updates/apply"' in response.text
    assert 'action="/operations/updates/rollback"' in response.text
    assert 'action="/operations/hosts/nas-01/reconcile"' in response.text
    assert 'action="/operations/hosts/nas-01/edit"' in response.text
    assert 'action="/operations/backups/drill"' in response.text


@pytest.mark.parametrize(
    ("path", "kind", "extra"),
    [
        ("/operations/maintenance", "MAINTENANCE", {}),
        ("/operations/backups/drill", "BACKUP_DRILL", {}),
        ("/operations/hosts/nas-01/reconcile", "HOST_RECONCILE", {}),
        ("/operations/hosts/nas-01/remove", "HOST_REMOVE", {"fingerprint": "b" * 64}),
        ("/operations/dumps/dmp_aaaaaaaaaaaaaaaaaaaa/drill", "RESTORE_DRILL", {}),
        ("/operations/updates/refresh", "UPDATE", {}),
        ("/operations/updates/apply", "UPDATE", {"revision": "c" * 64, "services": "redis"}),
        ("/operations/updates/rollback", "UPDATE", {"revision": "d" * 64}),
        ("/operations/updates/recover", "UPDATE", {}),
    ],
)
async def test_operations_actions_submit_durable_jobs(
    tmp_path: Path,
    monkeypatch,
    path: str,
    kind: str,
    extra: dict[str, str],
) -> None:
    controller = OperationsController()
    app = _app(tmp_path, monkeypatch, controller)
    async with _client(app) as client:
        page = await client.get("/operations")
        response = await client.post(
            path,
            data={"csrf_token": _csrf(page.text), **extra},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == f"/jobs/job-{kind.lower()}"
    assert controller.jobs[0].kind.value == kind


async def test_managed_host_create_uses_typed_resource_then_queues_reconcile(tmp_path: Path, monkeypatch) -> None:
    controller = OperationsController()
    app = _app(tmp_path, monkeypatch, controller)
    async with _client(app) as client:
        page = await client.get("/operations")
        assert 'name="integration.media-cache.path"' in page.text
        response = await client.post(
            "/operations/hosts",
            data={
                "csrf_token": _csrf(page.text),
                "revision": "a" * 64,
                "name": "edge-02",
                "ip": "192.0.2.41",
                "kind": "fleet",
                "ssh_user": "root",
                "ssh_port": "22",
                "cluster_group": "edge",
                "lldap_email": "ops@example.test",
                "headscale_tags": "tag:edge",
                "services": ["monitoring-agent", "ldap-client", "media-cache"],
                "integration.media-cache.path": "/srv/edge-media",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert controller.created[0].host.name == "edge-02"
    assert controller.created[0].host.services == ["monitoring-agent", "ldap-client", "media-cache"]
    assert controller.created[0].host.integrations == {"media-cache": {"path": "/srv/edge-media"}}
    assert controller.jobs[0].operation.host_name == "edge-02"


async def test_managed_host_create_reports_saved_state_when_job_queue_fails(tmp_path: Path, monkeypatch) -> None:
    from toolkit.controller.client import ControllerClientError

    controller = OperationsController()

    def reject(_request):
        raise ControllerClientError("queue unavailable")

    controller.submit = reject
    app = _app(tmp_path, monkeypatch, controller)
    async with _client(app) as client:
        page = await client.get("/operations")
        response = await client.post(
            "/operations/hosts",
            data={
                "csrf_token": _csrf(page.text),
                "revision": "a" * 64,
                "name": "edge-02",
                "ip": "192.0.2.41",
                "kind": "plain",
                "ssh_user": "root",
                "ssh_port": "22",
                "services": ["monitoring-agent"],
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "was+saved+but+reconciliation" in response.headers["location"]
    assert controller.created[0].host.name == "edge-02"
