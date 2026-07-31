from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from toolkit.controller.app import create_controller_app
from toolkit.controller.contracts import (
    IdentityOperation,
    InviteUserCommand,
    JobRequest,
    JobState,
    ServiceActionOperation,
    VerifyOperation,
)
from toolkit.controller.desired_state_api import SMTPSettingsValidationError
from toolkit.controller.read_models import (
    AccountView,
    BootstrapInitializeResult,
    ContainerInventory,
    DashboardMetrics,
    DashboardView,
    DirectoryGroupView,
    DirectoryUsersView,
    DirectoryUserView,
    GrafanaWebhookReceipt,
    InviteActivationResult,
    PortalStatus,
    SecretInventory,
    SecretMutationResult,
    SecretStatus,
    ServicesView,
    WebhookJobReceipt,
)
from toolkit.controller.store import ControllerStore
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.mutations import config_revision
from toolkit.core.config.storage import config_path
from toolkit.core.deploy.destructive_guard import record_verified_checkpoint
from toolkit.core.machines import MachineSpec

_LOCAL_TOKEN = "local-controller-token-for-tests-000000000000"
_UI_TOKEN = "ui-controller-token-for-tests-000000000000000"


@pytest.fixture
def controller_store(tmp_path: Path) -> ControllerStore:
    return ControllerStore(tmp_path / "controller.db")


@pytest.fixture
def controller_app(tmp_path: Path, controller_store: ControllerStore) -> FastAPI:
    return create_controller_app(
        root=tmp_path,
        store=controller_store,
        local_transport_token=_LOCAL_TOKEN,
        ui_transport_token=_UI_TOKEN,
    )


@pytest.fixture
async def controller_client(controller_app: FastAPI):
    async with AsyncClient(
        transport=ASGITransport(app=controller_app, client=None, raise_app_exceptions=False),
        base_url="http://controller",
        headers={"x-controller-transport": "local", "x-controller-token": _LOCAL_TOKEN},
    ) as client:
        yield client


async def _create_verify_job(client: AsyncClient, key: str = "request-12345678"):
    return await client.post(
        "/v1/jobs",
        json={"idempotency_key": key, "operation": {"kind": "VERIFY"}},
    )


async def _create_maintenance_job(client: AsyncClient, key: str):
    return await client.post(
        "/v1/jobs",
        json={"idempotency_key": key, "operation": {"kind": "MAINTENANCE"}},
    )


@pytest.mark.anyio
async def test_smtp_settings_rejection_returns_only_safe_structured_stage(
    tmp_path: Path,
    controller_client: AsyncClient,
    monkeypatch,
) -> None:
    save_config(Config(domain="example.test"), config_path(tmp_path))
    current = (await controller_client.get("/v1/settings")).json()

    def reject_smtp(*_args, **_kwargs):
        raise SMTPSettingsValidationError(
            "auth",
            "provider said password=should-never-reach-the-response",
        )

    monkeypatch.setattr("toolkit.controller.app.update_settings", reject_smtp)
    response = await controller_client.put(
        "/v1/settings",
        json={
            "expected_revision": current["revision"],
            "values": current["values"],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"] == {
        "code": "VALIDATION_ERROR",
        "message": "SMTP settings could not be verified",
        "details": {"field": "smtp", "stage": "auth"},
    }
    assert "should-never-reach-the-response" not in response.text


@pytest.mark.anyio
async def test_job_history_is_typed_bounded_and_ui_principal_scoped(
    controller_app: FastAPI,
    controller_client: AsyncClient,
) -> None:
    local_job = await _create_verify_job(controller_client, "local-history-1234")
    assert local_job.status_code == 201
    transport = ASGITransport(app=controller_app, client=None, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://controller",
        headers={"x-controller-transport": "ui", "x-controller-token": _UI_TOKEN},
    ) as ui:
        ui_job = await _create_maintenance_job(ui, "ui-history-request")
        ui_history = await ui.get("/v1/jobs", params={"limit": 50})

    local_history = await controller_client.get("/v1/jobs", params={"limit": 50})

    assert ui_job.status_code == 201
    assert ui_history.status_code == 200
    assert [job["job_id"] for job in ui_history.json()["jobs"]] == [ui_job.json()["job_id"]]
    assert len(local_history.json()["jobs"]) == 2
    assert ui_history.json()["queued"] == 1
    assert ui_history.json()["running"] == 0
    assert ui_history.json()["attention"] == 0
    assert ui_history.json()["succeeded"] == 0
    summary = ui_history.json()["jobs"][0]
    assert set(summary) == {"job_id", "kind", "state", "created_at", "updated_at", "can_cancel", "error_code"}
    assert "request" not in ui_history.text
    assert "idempotency" not in ui_history.text
    assert "principal" not in ui_history.text


@pytest.mark.anyio
async def test_job_history_rejects_an_unbounded_limit(controller_client: AsyncClient) -> None:
    response = await controller_client.get("/v1/jobs", params={"limit": 201})

    assert response.status_code == 422


@pytest.mark.anyio
async def test_job_history_only_offers_cancellation_when_the_store_accepts_it(
    controller_client: AsyncClient,
    controller_store: ControllerStore,
) -> None:
    queued = controller_store.create_job(
        JobRequest(
            idempotency_key="queued-service-action-1234",
            operation=ServiceActionOperation(service="music-sync", action="sync-now"),
        ),
        principal="local:operator",
    )
    running_action = controller_store.create_job(
        JobRequest(
            idempotency_key="running-service-action-1234",
            operation=ServiceActionOperation(service="music-sync", action="sync-now"),
        ),
        principal="local:operator",
    )
    controller_store.claim_job(running_action.job_id, worker_id="worker-a", lease_seconds=30)
    running_verify = controller_store.create_job(
        JobRequest(idempotency_key="running-verify-1234", operation=VerifyOperation()),
        principal="local:operator",
    )
    controller_store.claim_job(running_verify.job_id, worker_id="worker-b", lease_seconds=30)
    controller_store.transition(
        running_verify.job_id,
        expected=JobState.RUNNING,
        target=JobState.CANCEL_REQUESTED,
        worker_id="worker-b",
        lease_generation=controller_store.get_job(running_verify.job_id).lease_generation,
    )

    response = await controller_client.get("/v1/jobs", params={"limit": 50})

    assert response.status_code == 200, response.text
    by_id = {job["job_id"]: job for job in response.json()["jobs"]}
    assert by_id[queued.job_id]["can_cancel"] is True
    assert by_id[running_action.job_id]["can_cancel"] is False
    assert by_id[running_verify.job_id]["can_cancel"] is False


@pytest.mark.anyio
async def test_service_management_includes_builtin_metrics_for_services_without_custom_capabilities(
    controller_client: AsyncClient,
    monkeypatch,
    tmp_path: Path,
) -> None:
    from toolkit.core.config.config import Config, save_config
    from toolkit.core.config.storage import config_path

    save_config(Config(domain="example.com"), config_path(tmp_path))
    monkeypatch.setattr("toolkit.controller.service_management_api.read_service_metrics", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        "toolkit.controller.service_management_api.read_service_metric_history",
        lambda *_args, **_kwargs: {},
    )
    response = await controller_client.get("/v1/services/flaresolverr/management")

    assert response.status_code == 200, response.text
    assert response.json()["service"] == "flaresolverr"
    assert [metric["key"] for metric in response.json()["metrics"]] == [
        "container_cpu_percent",
        "container_memory_megabytes",
        "container_available_percent",
        "container_restart_attempts",
        "container_network_receive_mbps",
        "container_network_transmit_mbps",
        "container_disk_read_mbps",
        "container_disk_write_mbps",
        "container_uptime_seconds",
    ]


@pytest.mark.anyio
async def test_operations_view_and_restore_drill_are_controller_managed(
    controller_client: AsyncClient,
    tmp_path: Path,
) -> None:
    from toolkit.core.config.config import Config, save_config
    from toolkit.core.config.storage import config_path

    save_config(Config(domain="example.com", proxmox={"provision_machines": False}), config_path(tmp_path))
    view = await controller_client.get("/v1/operations")
    job = await controller_client.post(
        "/v1/jobs",
        json={
            "idempotency_key": "restore-drill-operation-1234",
            "operation": {"kind": "RESTORE_DRILL", "dump_id": "dmp_" + "a" * 20},
        },
    )

    assert view.status_code == 200, view.text
    assert set(view.json()) == {"maintenance", "backups", "dumps", "hosts", "updates"}
    assert job.status_code == 201, job.text
    assert job.json()["request"]["operation"]["kind"] == "RESTORE_DRILL"

    backup_job = await controller_client.post(
        "/v1/jobs",
        json={
            "idempotency_key": "backup-drill-operation-1234",
            "operation": {"kind": "BACKUP_DRILL"},
        },
    )
    assert backup_job.status_code == 201, backup_job.text

    config_job = await controller_client.post(
        "/v1/jobs",
        json={
            "idempotency_key": "config-apply-operation-1234",
            "operation": {"kind": "CONFIG_APPLY", "revision_hash": "a" * 64, "service": "music-sync"},
        },
    )
    assert config_job.status_code == 201, config_job.text


@pytest.mark.anyio
async def test_managed_host_resources_are_revisioned_and_typed(
    controller_client: AsyncClient,
    tmp_path: Path,
) -> None:
    from toolkit.core.config.config import Config, save_config
    from toolkit.core.config.storage import config_path

    save_config(Config(domain="example.com"), config_path(tmp_path))
    initial = await controller_client.get("/v1/hosts")
    response = await controller_client.post(
        "/v1/hosts",
        json={
            "expected_revision": initial.json()["revision"],
            "host": {
                "name": "edge-01",
                "ip": "192.0.2.20",
                "kind": "fleet",
                "ssh_user": "root",
                "ssh_port": 22,
                "cluster_group": "edge",
                "lldap_email": "ops@example.com",
                "headscale_tags": ["tag:edge"],
                "services": ["monitoring-agent", "vpn-client", "ldap-client"],
                "integrations": {},
            },
        },
    )

    assert initial.status_code == 200, initial.text
    assert response.status_code == 201, response.text
    assert response.json()["hosts"][0]["name"] == "edge-01"
    assert response.json()["hosts"][0]["reconciled"] is False
    assert "ldap-client" in {choice["name"] for choice in response.json()["service_choices"]}


@pytest.mark.anyio
async def test_machine_resources_are_revisioned_and_typed(
    controller_client: AsyncClient,
    tmp_path: Path,
) -> None:
    from toolkit.core.config.config import Config, save_config
    from toolkit.core.config.storage import config_path

    save_config(Config(domain="example.com"), config_path(tmp_path))
    initial = await controller_client.get("/v1/machines")
    worker = {
        "kind": "lxc",
        "provider": "proxmox",
        "enabled": True,
        "managed": False,
        "hostname": "worker-01",
        "address": "10.10.10.20",
        "vmid": 820,
        "description": "Compute worker",
        "labels": ["compute"],
        "cores": 2,
        "memory_mb": 2048,
        "root_disk_gb": 32,
        "root_datastore": "",
        "data_disks": [],
        "private_bridge": "vmbr1",
        "public_bridge": "",
        "gateway": "10.10.10.1",
        "cidr": 24,
        "startup_order": 40,
        "nesting": True,
        "keyctl": True,
        "fuse": False,
        "template_file_id": "",
        "admin_user": "",
        "ssh_user": "",
        "ssh_port": 22,
        "cloud_image_datastore": "",
        "cloud_image_format": "",
        "cloud_image_url": "",
        "cloud_image_sha256": "",
        "resource_limits": {},
    }
    created = await controller_client.post(
        "/v1/machines",
        json={
            "expected_revision": initial.json()["revision"],
            "machine_id": "worker-east",
            "spec": worker,
        },
    )

    assert initial.status_code == 200, initial.text
    assert created.status_code == 201, created.text
    machine = next(item for item in created.json()["machines"] if item["machine_id"] == "worker-east")
    assert machine["spec"]["hostname"] == "worker-01"
    assert machine["can_remove"] is False
    assert "machine is enabled" in machine["removal_blockers"]


@pytest.mark.anyio
async def test_health_reports_database_status_and_correlation_id(controller_client: AsyncClient) -> None:
    response = await controller_client.get("/v1/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert response.json()["database_ok"] is True
    assert response.json()["worker_ok"] is True
    assert response.json()["secret_store_ok"] is True
    assert response.json()["queued_jobs"] == 0
    assert response.headers["x-correlation-id"]


@pytest.mark.anyio
async def test_health_fails_readiness_when_encrypted_secret_store_cannot_be_read(
    controller_client: AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / "secrets.enc.yaml").write_text("sops: {}\n")

    def fail_decryption(_path: Path) -> dict[str, str]:
        raise RuntimeError("secret detail must not cross the health boundary")

    monkeypatch.setattr("toolkit.controller.app.load_secrets_plaintext", fail_decryption)

    response = await controller_client.get("/v1/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"
    assert response.json()["secret_store_ok"] is False
    assert "secret detail" not in response.text


@pytest.mark.anyio
async def test_ui_socket_token_cannot_impersonate_local_operator(controller_app: FastAPI) -> None:
    transport = ASGITransport(app=controller_app, client=None, raise_app_exceptions=False)
    async with AsyncClient(
        transport=transport,
        base_url="http://controller",
        headers={"x-controller-transport": "ui", "x-controller-token": _UI_TOKEN},
    ) as ui:
        permitted = await ui.get("/v1/deployment")
        destructive = await ui.post(
            "/v1/plans/destruction",
            json={"action": "destroy_all", "scopes": ["infra", "apps", "media"]},
        )

    async with AsyncClient(
        transport=transport,
        base_url="http://controller",
        headers={"x-controller-transport": "local", "x-controller-token": _UI_TOKEN},
    ) as forged_local:
        forged = await forged_local.get("/v1/deployment")

    assert permitted.status_code == 200
    assert destructive.status_code == 403
    assert forged.status_code == 403
    assert forged.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.anyio
async def test_forged_mtls_headers_are_not_an_authentication_transport(controller_app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=controller_app, client=None, raise_app_exceptions=False),
        base_url="http://controller",
        headers={
            "x-controller-transport": "mtls",
            "x-controller-principal": "homelab-ui",
            "x-client-cert-fingerprint": "a" * 64,
        },
    ) as client:
        deployment = await client.get("/v1/deployment")
        secrets = await client.get("/v1/settings/secrets")

    assert deployment.status_code == 403
    assert secrets.status_code == 403


@pytest.mark.anyio
async def test_directory_users_is_a_typed_ui_resource(controller_app: FastAPI, monkeypatch) -> None:
    view = DirectoryUsersView(
        domain="example.com",
        users=[
            DirectoryUserView(
                id="family",
                email="family@example.com",
                display_name="Family",
                groups=["homelab-media"],
                is_protected=False,
            )
        ],
        group_options=[
            DirectoryGroupView(name="homelab-media", label="Media", description="Media", is_default=True),
            DirectoryGroupView(name="homelab-cloud", label="Cloud", description="Cloud", is_default=False),
            DirectoryGroupView(name="homelab-admin", label="Admin", description="Admin", is_default=False),
        ],
        invites_enabled=True,
    )
    monkeypatch.setattr("toolkit.controller.app.read_directory_users", lambda _root: view)
    async with AsyncClient(
        transport=ASGITransport(app=controller_app, client=None, raise_app_exceptions=False),
        base_url="http://controller",
        headers={"x-controller-transport": "ui", "x-controller-token": _UI_TOKEN},
    ) as client:
        response = await client.get("/v1/identity/users")

    assert response.status_code == 200
    assert response.json() == view.model_dump(mode="json")


@pytest.mark.anyio
async def test_directory_failure_is_sanitized_for_ui(controller_app: FastAPI, monkeypatch) -> None:
    from toolkit.controller.identity_api import DirectoryUnavailableError

    def unavailable(_root):
        raise DirectoryUnavailableError("ldap-bind-password-canary")

    monkeypatch.setattr("toolkit.controller.app.read_directory_users", unavailable)
    async with AsyncClient(
        transport=ASGITransport(app=controller_app, client=None, raise_app_exceptions=False),
        base_url="http://controller",
        headers={"x-controller-transport": "ui", "x-controller-token": _UI_TOKEN},
    ) as client:
        response = await client.get("/v1/identity/users")

    assert response.status_code == 503
    assert response.json()["error"] == {
        "code": "OPERATION_REJECTED",
        "message": "The identity directory is unavailable",
        "details": {},
    }
    assert "ldap-bind-password-canary" not in response.text


@pytest.mark.anyio
async def test_directory_users_rejects_unauthenticated_transport(controller_app: FastAPI, monkeypatch) -> None:
    read = MagicMock()
    monkeypatch.setattr("toolkit.controller.app.read_directory_users", read)
    async with AsyncClient(
        transport=ASGITransport(app=controller_app, client=None, raise_app_exceptions=False),
        base_url="http://controller",
    ) as client:
        response = await client.get("/v1/identity/users")

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"
    read.assert_not_called()


@pytest.mark.anyio
async def test_identity_jobs_are_serialized_and_never_accept_passwords(controller_app: FastAPI) -> None:
    transport = ASGITransport(app=controller_app, client=None, raise_app_exceptions=False)
    headers = {"x-controller-transport": "ui", "x-controller-token": _UI_TOKEN}
    operation = {
        "kind": "IDENTITY",
        "command": {
            "action": "invite",
            "email": "family@example.com",
            "groups": ["homelab-media"],
        },
    }
    async with AsyncClient(transport=transport, base_url="http://controller", headers=headers) as client:
        created = await client.post(
            "/v1/jobs",
            json={"idempotency_key": "identity-request-1234", "operation": operation},
        )
        limited = await client.post(
            "/v1/jobs",
            json={"idempotency_key": "identity-request-5678", "operation": operation},
        )
        password = await client.post(
            "/v1/jobs",
            json={
                "idempotency_key": "identity-request-9012",
                "operation": {
                    **operation,
                    "command": {**operation["command"], "password": "must-not-persist"},
                },
            },
        )

    assert created.status_code == 201
    assert limited.status_code == 429
    assert password.status_code == 422


@pytest.mark.anyio
async def test_internal_sealed_identity_payload_cannot_be_submitted(controller_client: AsyncClient) -> None:
    response = await controller_client.post(
        "/v1/jobs",
        json={
            "idempotency_key": "identity-sealed-1234",
            "operation": {
                "kind": "IDENTITY",
                "command": {"action": "invite_sealed", "ciphertext": "A" * 64},
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.anyio
async def test_deployment_view_is_a_typed_controller_resource(controller_client: AsyncClient) -> None:
    response = await controller_client.get("/v1/deployment")

    assert response.status_code == 200
    assert response.json() == {
        "state": "uninitialized",
        "enabled_targets": [],
        "node_count": 0,
        "total_services": 0,
        "category_count": 0,
        "generated_config_count": 0,
        "step_labels": {},
        "preflight": [],
        "preflight_ok": False,
        "last_verify": None,
        "active_jobs": [],
    }


@pytest.mark.anyio
async def test_bootstrap_capability_is_local_only_and_exchanged_without_echo(
    controller_app: FastAPI,
    controller_client: AsyncClient,
) -> None:
    issued = await controller_client.post("/v1/bootstrap/capabilities")
    assert issued.status_code == 201
    capability = issued.json()["token"]

    async with AsyncClient(
        transport=ASGITransport(app=controller_app, client=None),
        base_url="http://controller",
        headers={"x-controller-transport": "ui", "x-controller-token": _UI_TOKEN},
    ) as ui_client:
        denied = await ui_client.post("/v1/bootstrap/capabilities")
        exchanged = await ui_client.post(
            "/v1/bootstrap/sessions",
            json={"capability_token": capability},
        )

    assert denied.status_code == 403
    assert exchanged.status_code == 201
    assert capability not in exchanged.text
    assert exchanged.json()["session_token"]


@pytest.mark.anyio
async def test_bootstrap_status_and_view_are_typed_ui_resources(
    controller_app: FastAPI,
    controller_client: AsyncClient,
) -> None:
    capability = (await controller_client.post("/v1/bootstrap/capabilities")).json()["token"]

    async with AsyncClient(
        transport=ASGITransport(app=controller_app, client=None),
        base_url="http://controller",
        headers={"x-controller-transport": "ui", "x-controller-token": _UI_TOKEN},
    ) as ui_client:
        status_response = await ui_client.get("/v1/bootstrap/status")
        session = (
            await ui_client.post(
                "/v1/bootstrap/sessions",
                json={"capability_token": capability},
            )
        ).json()["session_token"]
        view_response = await ui_client.get(
            "/v1/bootstrap",
            headers={"x-bootstrap-session": session},
        )

    assert status_response.status_code == 200
    assert status_response.json()["phase"] == "uninitialized"
    assert view_response.status_code == 200
    assert view_response.json()["status"]["has_active_session"] is True
    assert any(category["name"] == "management" for category in view_response.json()["categories"])
    assert session not in view_response.text


@pytest.mark.anyio
async def test_bootstrap_initialization_does_not_echo_credentials(
    controller_app: FastAPI,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = "00000000-0000-4000-8000-000000000000.session-secret-value"
    credential = "credential-canary-that-must-not-return"

    def initialize(_root: Path, _store: ControllerStore, request, *, principal: str):
        assert principal == "ui:homelab-ui"
        assert request.session_token == session
        assert request.credential_values["CLOUDFLARE_API_TOKEN"] == credential
        return BootstrapInitializeResult(
            config_revision="a" * 64,
            configured_secret_names=["CLOUDFLARE_API_TOKEN"],
        )

    monkeypatch.setattr("toolkit.controller.app.initialize_bootstrap", initialize)
    async with AsyncClient(
        transport=ASGITransport(app=controller_app, client=None),
        base_url="http://controller",
        headers={"x-controller-transport": "ui", "x-controller-token": _UI_TOKEN},
    ) as ui_client:
        response = await ui_client.post(
            "/v1/bootstrap/initializations",
            json={
                "session_token": session,
                "desired_state": {
                    "domain": "home.example.com",
                    "email": "operator@example.com",
                    "timezone": "Europe/Madrid",
                    "proxmox_api_url": "https://192.0.2.10:8006",
                    "proxmox_node": "pve",
                    "proxmox_storage": "local-zfs",
                    "service_settings": {
                        "media-library": {"server": "jellyfin"},
                        "gluetun": {"enabled": True, "provider": "nordvpn"},
                        "media-cache": {"enabled": True},
                        "tdarr": {"enabled": True},
                        "music-sync": {"enabled": True},
                    },
                },
                "credential_values": {"CLOUDFLARE_API_TOKEN": credential},
            },
        )

    assert response.status_code == 201
    assert response.json()["phase"] == "ready"
    assert session not in response.text
    assert credential not in response.text


@pytest.mark.anyio
async def test_bootstrap_capability_is_refused_for_partial_install(
    tmp_path: Path,
    controller_client: AsyncClient,
) -> None:
    (tmp_path / "config.yaml").write_text("domain: partial.example.com\n")

    response = await controller_client.post("/v1/bootstrap/capabilities")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"
    assert "token" not in response.text.lower()


@pytest.mark.anyio
async def test_job_submission_is_idempotent_with_truthful_status(controller_client: AsyncClient) -> None:
    created = await _create_verify_job(controller_client)
    replayed = await _create_verify_job(controller_client)

    assert created.status_code == 201
    assert replayed.status_code == 200
    assert replayed.json()["job_id"] == created.json()["job_id"]


@pytest.mark.anyio
async def test_controller_rejects_duplicate_active_deployment_family_jobs(controller_client: AsyncClient) -> None:
    first = await controller_client.post(
        "/v1/jobs",
        json={"idempotency_key": "deploy-first-12345", "operation": {"kind": "DEPLOY"}},
    )
    duplicate = await controller_client.post(
        "/v1/jobs",
        json={"idempotency_key": "recover-second-123", "operation": {"kind": "RECOVER"}},
    )

    assert first.status_code == 201
    assert duplicate.status_code == 429
    assert duplicate.json()["error"]["code"] == "OPERATION_REJECTED"


@pytest.mark.anyio
async def test_job_submission_without_verified_transport_fails_closed(controller_app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=controller_app, client=None),
        base_url="http://controller",
    ) as client:
        response = await _create_verify_job(client)

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.anyio
@pytest.mark.parametrize("transport", ["", "uds", "proxy", "localish"])
async def test_unknown_transport_marker_fails_closed(controller_app: FastAPI, transport: str) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=controller_app, client=None),
        base_url="http://controller",
        headers={"x-controller-transport": transport},
    ) as client:
        response = await _create_verify_job(client)

    assert response.status_code == 403


@pytest.mark.anyio
async def test_idempotency_conflict_uses_stable_error_shape(controller_client: AsyncClient) -> None:
    await _create_verify_job(controller_client)
    response = await controller_client.post(
        "/v1/jobs",
        json={
            "idempotency_key": "request-12345678",
            "operation": {"kind": "DEPLOY", "target": "infra"},
        },
    )

    assert response.status_code == 409
    assert response.json() == {
        "error": {
            "code": "CONFLICT",
            "message": "Idempotency key conflicts with an existing request",
            "details": {},
        }
    }


@pytest.mark.anyio
async def test_revision_locked_update_operation_is_queued(controller_client: AsyncClient) -> None:
    response = await controller_client.post(
        "/v1/jobs",
        json={
            "idempotency_key": "update-request-1234",
            "operation": {
                "kind": "UPDATE",
                "action": "apply",
                "services": ["grafana"],
                "revision": "a" * 64,
            },
        },
    )

    assert response.status_code == 201
    assert response.json()["request"]["operation"]["revision"] == "a" * 64


@pytest.mark.anyio
async def test_webhook_heal_cannot_be_submitted_through_generic_job_api(controller_client: AsyncClient) -> None:
    response = await controller_client.post(
        "/v1/jobs",
        json={
            "idempotency_key": "forged-webhook-heal-1234",
            "operation": {
                "kind": "WEBHOOK_HEAL",
                "service": "sonarr",
                "source": "grafana",
                "alert_fingerprint": "a" * 64,
            },
        },
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.anyio
async def test_unknown_mtls_principal_cannot_submit_enabled_job(controller_app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=controller_app, client=None),
        base_url="http://controller",
        headers={
            "x-controller-transport": "mtls",
            "x-controller-principal": "fleet-node-1",
            "x-client-cert-fingerprint": "a" * 64,
        },
    ) as client:
        response = await _create_verify_job(client)

    assert response.status_code == 403


@pytest.mark.anyio
async def test_oversized_request_body_is_rejected(controller_client: AsyncClient) -> None:
    response = await controller_client.post(
        "/v1/jobs",
        content=b"x" * (64 * 1024 + 1),
        headers={"content-type": "application/json"},
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


@pytest.mark.anyio
async def test_unknown_job_uses_stable_not_found_error(controller_client: AsyncClient) -> None:
    response = await controller_client.get("/v1/jobs/missing-job")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "NOT_FOUND"


@pytest.mark.anyio
async def test_validation_error_does_not_echo_approval_token(controller_client: AsyncClient) -> None:
    secret = "approval-token-that-must-not-leak"
    response = await controller_client.post(
        "/v1/jobs",
        json={
            "idempotency_key": "destroy-request-1234",
            "operation": {
                "kind": "DESTROY_INFRA",
                "action": "destroy_all",
                "scopes": ["Invalid_Node"],
                "config_revision": "b" * 64,
                "plan_id": "plan-identifier-1234",
                "plan_hash": "a" * 64,
                "approval_token": secret,
            },
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"
    assert secret not in response.text


@pytest.mark.anyio
async def test_secret_update_never_echoes_submitted_value(
    controller_client: AsyncClient,
    monkeypatch,
) -> None:
    canary = "secret-canary-that-must-never-cross-the-read-boundary"
    inventory = SecretInventory(
        owner_email="owner@example.test",
        storage_mode="encrypted",
        encryption_available=True,
        entries=[
            SecretStatus(
                name="CF_API_TOKEN",
                isConfigured=True,
                tier="user",
                description="Cloudflare API token",
            )
        ],
    )

    def update(_root: Path, request):
        assert request.values == {"CF_API_TOKEN": canary}
        return SecretMutationResult(changed_names=["CF_API_TOKEN"], inventory=inventory)

    monkeypatch.setattr("toolkit.controller.app.update_secret_values", update)
    response = await controller_client.put(
        "/v1/settings/secrets",
        json={"values": {"CF_API_TOKEN": canary}},
    )

    assert response.status_code == 200
    assert response.json()["inventory"]["entries"][0]["isConfigured"] is True
    assert canary not in response.text


@pytest.mark.anyio
async def test_secret_settings_reject_unknown_mtls_principal(controller_app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=controller_app, client=None),
        base_url="http://controller",
        headers={
            "x-controller-transport": "mtls",
            "x-controller-principal": "untrusted-workload",
            "x-client-cert-fingerprint": "a" * 64,
        },
    ) as client:
        response = await client.get("/v1/settings/secrets")

    assert response.status_code == 403


@pytest.mark.anyio
async def test_services_endpoint_passes_only_validated_audience_context(
    controller_client: AsyncClient,
    monkeypatch,
) -> None:
    captured = {}

    def read(_root: Path, *, family: bool, groups: list[str]):
        captured.update(family=family, groups=groups)
        return ServicesView(
            domain="example.test",
            categories=[],
            bookmark_groups=[],
            family_sections=[],
            tier_labels=["Media"],
        )

    monkeypatch.setattr("toolkit.controller.app.read_services_view", read)
    response = await controller_client.get(
        "/v1/services",
        params=[("family", "true"), ("group", "homelab-media")],
    )

    assert response.status_code == 200
    assert captured == {"family": True, "groups": ["homelab-media"]}


@pytest.mark.anyio
async def test_account_endpoint_returns_display_safe_identity_view(
    controller_client: AsyncClient,
    monkeypatch,
) -> None:
    captured = {}

    def read(_root: Path, *, groups: list[str]):
        captured["groups"] = groups
        return AccountView(
            domain="example.test",
            auth_url="https://auth.example.test",
            sections=[],
            tier_labels=["Media"],
        )

    monkeypatch.setattr("toolkit.controller.app.read_account_view", read)
    response = await controller_client.get(
        "/v1/identity/account",
        params=[("group", "homelab-media")],
    )

    assert response.status_code == 200
    assert response.json()["auth_url"] == "https://auth.example.test"
    assert captured == {"groups": ["homelab-media"]}


@pytest.mark.anyio
async def test_invite_activation_endpoint_never_echoes_token_or_password(
    controller_client: AsyncClient,
    controller_store: ControllerStore,
    monkeypatch,
) -> None:
    token = "opaque-token-canary"
    password = "password-canary-123"

    def activate(_root: Path, request):
        assert request.token == token
        assert request.password == password
        return InviteActivationResult(outcome="activated", secure_cookie=True)

    monkeypatch.setattr("toolkit.controller.app.activate_invite", activate)
    response = await controller_client.post(
        "/v1/identity/invite-activation",
        json={
            "token": token,
            "activation_csrf": "a" * 64,
            "origin": "https://homelab.example.test",
            "password": password,
        },
    )

    assert response.status_code == 200
    assert response.json() == {"outcome": "activated", "secure_cookie": True}
    assert token not in response.text
    assert password not in response.text
    audit = controller_store.audit_after(0)[0]
    assert audit.action == "INVITE_ACTIVATION"
    assert audit.outcome == "ALLOWED"
    assert token not in audit.model_dump_json()
    assert password not in audit.model_dump_json()


@pytest.mark.anyio
async def test_invite_resources_reject_unknown_mtls_principal(controller_app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=controller_app, client=None),
        base_url="http://controller",
        headers={
            "x-controller-transport": "mtls",
            "x-controller-principal": "untrusted-workload",
            "x-client-cert-fingerprint": "a" * 64,
        },
    ) as client:
        preview = await client.post("/v1/identity/invite-preview", json={"token": "opaque"})
        activation = await client.post(
            "/v1/identity/invite-activation",
            json={
                "token": "opaque",
                "activation_csrf": "a" * 64,
                "origin": "https://homelab.example.test",
                "password": "password-123",
            },
        )

    assert preview.status_code == 403
    assert activation.status_code == 403


@pytest.mark.anyio
async def test_grafana_endpoint_forwards_raw_signed_body_to_narrow_ingestor(
    controller_client: AsyncClient,
    monkeypatch,
) -> None:
    body = b'{"status":"firing","alerts":[]}'
    captured = {}

    def accept(_root, _store, raw_body, **kwargs):
        captured.update(raw_body=raw_body, **kwargs)
        return GrafanaWebhookReceipt(
            outcome="queued",
            jobs=[WebhookJobReceipt(service="sonarr", job_id="job-1", replayed=False)],
        )

    monkeypatch.setattr("toolkit.controller.app.accept_grafana_alert", accept)
    response = await controller_client.post(
        "/v1/integrations/grafana/alerts",
        content=body,
        headers={
            "content-type": "application/json",
            "x-grafana-alerting-signature": "a" * 64,
            "x-grafana-alerting-signature-timestamp": "1800000000",
        },
    )

    assert response.status_code == 202
    assert captured == {
        "raw_body": body,
        "signature": "a" * 64,
        "timestamp": "1800000000",
        "content_type": "application/json",
    }


@pytest.mark.anyio
async def test_grafana_endpoint_rejects_unknown_mtls_principal(controller_app: FastAPI) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=controller_app, client=None),
        base_url="http://controller",
        headers={
            "x-controller-transport": "mtls",
            "x-controller-principal": "untrusted-workload",
            "x-client-cert-fingerprint": "a" * 64,
        },
    ) as client:
        response = await client.post(
            "/v1/integrations/grafana/alerts",
            content=b"{}",
            headers={"content-type": "application/json"},
        )

    assert response.status_code == 403


@pytest.mark.anyio
async def test_container_inventory_response_is_typed_and_bounded(
    controller_client: AsyncClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "toolkit.controller.app.read_container_inventory",
        lambda _root: ContainerInventory(
            is_available=False,
            unavailable_nodes=["infra"],
            containers=[],
        ),
    )

    response = await controller_client.get("/v1/containers")

    assert response.status_code == 200
    assert response.json() == {
        "is_available": False,
        "unavailable_nodes": ["infra"],
        "containers": [],
    }


@pytest.mark.anyio
async def test_portal_status_response_is_typed_and_bounded(
    controller_client: AsyncClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "toolkit.controller.app.read_portal_status",
        lambda _root: PortalStatus(
            checked_at=datetime(2026, 7, 28, 12, tzinfo=UTC),
            complete=False,
            unavailable_nodes=1,
            services={"prometheus": "online"},
        ),
    )

    response = await controller_client.get("/v1/dashboard/portal-status")

    assert response.status_code == 200
    assert response.json() == {
        "checked_at": "2026-07-28T12:00:00Z",
        "complete": False,
        "unavailable_nodes": 1,
        "services": {"prometheus": "online"},
    }


@pytest.mark.anyio
async def test_dashboard_endpoint_returns_only_typed_snapshot(
    controller_client: AsyncClient,
    monkeypatch,
) -> None:
    monkeypatch.setattr(
        "toolkit.controller.app.read_dashboard_view",
        lambda _root, **_kwargs: DashboardView(
            state="ready",
            domain="example.test",
            enabled_nodes=["infra"],
            categories=[],
            metrics=DashboardMetrics(cpu=12.5, cpu_history=[]),
            alerts=[],
            bookmark_groups=[],
            tier_labels=[],
        ),
    )

    response = await controller_client.get("/v1/dashboard")

    assert response.status_code == 200
    assert response.json()["metrics"]["cpu"] == 12.5
    assert "root" not in response.json()


@pytest.mark.anyio
async def test_destruction_plan_approval_and_submission_are_checkpoint_bound_and_one_time(
    controller_client: AsyncClient,
    tmp_path: Path,
) -> None:
    save_config(Config(), config_path(tmp_path))
    evidence = tmp_path / "restore-drill.json"
    evidence.write_text('{"ok": true}\n')
    checkpoint = record_verified_checkpoint(tmp_path, ["infra", "apps", "media"], [evidence])

    planned = await controller_client.post(
        "/v1/plans/destruction",
        json={"action": "destroy_all", "scopes": ["infra", "apps", "media"]},
    )
    assert planned.status_code == 201
    plan = planned.json()
    assert plan["actor"] == "local:operator"
    assert plan["spec"]["action"] == "destroy_all"
    assert plan["spec"]["config_revision"] == config_revision(tmp_path)
    assert plan["spec"]["checkpoint_id"] == checkpoint.checkpoint_id
    assert len(plan["spec"]["evidence_digest"]) == 64

    fetched = await controller_client.get(f"/v1/plans/{plan['plan_id']}")
    assert fetched.status_code == 200
    assert fetched.json() == plan

    approved = await controller_client.post(
        f"/v1/plans/{plan['plan_id']}/approval",
        json={
            "ttl_seconds": 300,
            "plan_hash": plan["plan_hash"],
            "confirmation": "DESTROY ALL MANAGED INFRASTRUCTURE",
        },
    )
    assert approved.status_code == 201
    approval = approved.json()

    request = {
        "idempotency_key": "destroy-request-1234",
        "operation": {
            "kind": "DESTROY_INFRA",
            "action": "destroy_all",
            "scopes": ["infra", "apps", "media"],
            "config_revision": plan["spec"]["config_revision"],
            "plan_id": plan["plan_id"],
            "plan_hash": plan["plan_hash"],
            "approval_token": approval["token"],
        },
    }
    created = await controller_client.post("/v1/jobs", json=request)
    replayed = await controller_client.post("/v1/jobs", json=request)
    reused = await controller_client.post(
        "/v1/jobs",
        json={**request, "idempotency_key": "destroy-request-5678"},
    )

    assert created.status_code == 201
    assert replayed.status_code == 200
    assert reused.status_code == 403


@pytest.mark.anyio
async def test_destruction_plan_requires_fresh_verified_checkpoint(
    controller_client: AsyncClient,
    tmp_path: Path,
) -> None:
    save_config(Config(), config_path(tmp_path))
    response = await controller_client.post(
        "/v1/plans/destruction",
        json={"action": "destroy_all", "scopes": ["infra", "apps", "media"]},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CHECKPOINT_REQUIRED"


@pytest.mark.anyio
async def test_ui_principal_cannot_plan_or_submit_destruction(controller_app: FastAPI) -> None:
    headers = {
        "x-controller-transport": "ui",
        "x-controller-token": _UI_TOKEN,
    }
    async with AsyncClient(
        transport=ASGITransport(app=controller_app, client=None),
        base_url="http://controller",
        headers=headers,
    ) as client:
        response = await client.post(
            "/v1/plans/destruction",
            json={"action": "destroy_all", "scopes": ["infra", "apps", "media"]},
        )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "FORBIDDEN"


@pytest.mark.anyio
async def test_retirement_plan_is_revision_bound_and_uses_machine_specific_confirmation(
    controller_client: AsyncClient,
    tmp_path: Path,
) -> None:
    worker = MachineSpec(
        managed=True,
        hostname="worker-01",
        address="10.10.10.20",
        gateway="10.10.10.1",
        vmid=820,
        labels=("compute",),
    )
    save_config(Config(machines={**Config().machines, "worker-east": worker}), config_path(tmp_path))
    evidence = tmp_path / "restore-drill.json"
    evidence.write_text('{"ok": true}\n')
    record_verified_checkpoint(tmp_path, ["worker-east"], [evidence])

    planned = await controller_client.post(
        "/v1/plans/destruction",
        json={"action": "retire_machine", "scopes": ["worker-east"]},
    )

    assert planned.status_code == 201, planned.text
    plan = planned.json()
    assert plan["spec"]["action"] == "retire_machine"
    assert plan["spec"]["scopes"] == ["worker-east"]
    assert plan["spec"]["config_revision"] == config_revision(tmp_path)

    wrong = await controller_client.post(
        f"/v1/plans/{plan['plan_id']}/approval",
        json={
            "ttl_seconds": 300,
            "plan_hash": plan["plan_hash"],
            "confirmation": "DESTROY ALL MANAGED INFRASTRUCTURE",
        },
    )
    approved = await controller_client.post(
        f"/v1/plans/{plan['plan_id']}/approval",
        json={
            "ttl_seconds": 300,
            "plan_hash": plan["plan_hash"],
            "confirmation": "RETIRE MACHINE worker-east",
        },
    )

    assert wrong.status_code == 403
    assert approved.status_code == 201


@pytest.mark.anyio
async def test_ui_principal_can_submit_only_a_scoped_retirement(
    controller_app: FastAPI,
    tmp_path: Path,
) -> None:
    worker = MachineSpec(
        managed=True,
        hostname="worker-01",
        address="10.10.10.20",
        gateway="10.10.10.1",
        vmid=820,
        labels=("compute",),
    )
    save_config(Config(machines={**Config().machines, "worker-east": worker}), config_path(tmp_path))
    evidence = tmp_path / "restore-drill.json"
    evidence.write_text('{"ok": true}\n')
    record_verified_checkpoint(tmp_path, ["worker-east"], [evidence])
    headers = {"x-controller-transport": "ui", "x-controller-token": _UI_TOKEN}
    async with AsyncClient(
        transport=ASGITransport(app=controller_app, client=None),
        base_url="http://controller",
        headers=headers,
    ) as client:
        planned = await client.post(
            "/v1/plans/destruction",
            json={"action": "retire_machine", "scopes": ["worker-east"]},
        )
        assert planned.status_code == 201, planned.text
        plan = planned.json()
        approved = await client.post(
            f"/v1/plans/{plan['plan_id']}/approval",
            json={
                "ttl_seconds": 300,
                "plan_hash": plan["plan_hash"],
                "confirmation": "RETIRE MACHINE worker-east",
            },
        )
        submitted = await client.post(
            "/v1/jobs",
            json={
                "idempotency_key": "retire-worker-east-1234",
                "operation": {
                    "kind": "DESTROY_INFRA",
                    "action": "retire_machine",
                    "scopes": ["worker-east"],
                    "config_revision": plan["spec"]["config_revision"],
                    "plan_id": plan["plan_id"],
                    "plan_hash": plan["plan_hash"],
                    "approval_token": approved.json()["token"],
                },
            },
        )

    assert plan["actor"] == "ui:homelab-ui"
    assert approved.status_code == 201
    assert submitted.status_code == 201


@pytest.mark.anyio
async def test_rejected_transport_cannot_access_existing_jobs(
    controller_app: FastAPI,
    controller_store: ControllerStore,
) -> None:
    transport = ASGITransport(app=controller_app, client=None)
    job, _ = controller_store.submit_job(
        JobRequest(idempotency_key="existing-job-request", operation=VerifyOperation()),
        principal="ui:homelab-ui",
    )

    async with AsyncClient(
        transport=transport,
        base_url="http://controller",
        headers={
            "x-controller-transport": "mtls",
            "x-controller-principal": "homelab-ui",
            "x-client-cert-fingerprint": "a" * 64,
        },
    ) as other:
        responses = [
            await other.get(f"/v1/jobs/{job.job_id}"),
            await other.post(f"/v1/jobs/{job.job_id}/cancellation"),
            await other.get(f"/v1/jobs/{job.job_id}/events"),
        ]

    assert [response.status_code for response in responses] == [403, 403, 403]


@pytest.mark.anyio
async def test_local_operator_can_administer_ui_job_with_truthful_audit(
    controller_app: FastAPI,
    controller_store: ControllerStore,
) -> None:
    transport = ASGITransport(app=controller_app, client=None)
    async with AsyncClient(
        transport=transport,
        base_url="http://controller",
        headers={"x-controller-transport": "ui", "x-controller-token": _UI_TOKEN},
    ) as owner:
        created = await _create_verify_job(owner)

    async with AsyncClient(
        transport=transport,
        base_url="http://controller",
        headers={"x-controller-transport": "local", "x-controller-token": _LOCAL_TOKEN},
    ) as local:
        cancelled = await local.post(f"/v1/jobs/{created.json()['job_id']}/cancellation")

    assert cancelled.status_code == 200
    cancellation = [record for record in controller_store.audit_after(0) if record.action == "JOB_CANCEL_REQUEST"]
    assert cancellation[-1].principal == "local:operator"


@pytest.mark.anyio
async def test_cancellation_and_event_replay(controller_client: AsyncClient, controller_store: ControllerStore) -> None:
    created = await _create_verify_job(controller_client)
    job_id = created.json()["job_id"]
    first = controller_store.append_event(job_id, "INFO", "queued")
    controller_store.append_event(job_id, "INFO", "validated")

    replay = await controller_client.get(f"/v1/jobs/{job_id}/events", params={"after": first.sequence})
    cancelled = await controller_client.post(f"/v1/jobs/{job_id}/cancellation")

    assert [event["message"] for event in replay.json()] == ["validated"]
    assert cancelled.json()["state"] == "CANCELLED"


@pytest.mark.anyio
async def test_running_identity_job_cannot_be_cancelled(
    controller_client: AsyncClient,
    controller_store: ControllerStore,
) -> None:
    job, _ = controller_store.submit_job(
        JobRequest(
            idempotency_key="identity-running-1234",
            operation=IdentityOperation(
                command=InviteUserCommand(email="family@example.com", groups=["homelab-media"])
            ),
        ),
        principal="local:operator",
    )
    controller_store.claim_job(job.job_id, worker_id="worker-a", lease_seconds=30)

    response = await controller_client.post(f"/v1/jobs/{job.job_id}/cancellation")

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "CONFLICT"
    assert controller_store.get_job(job.job_id).state.value == "RUNNING"


@pytest.mark.anyio
async def test_event_replay_limit_is_bounded(controller_client: AsyncClient, controller_store: ControllerStore) -> None:
    created = await _create_verify_job(controller_client)
    job_id = created.json()["job_id"]
    for index in range(3):
        controller_store.append_event(job_id, "INFO", f"event {index}")

    response = await controller_client.get(f"/v1/jobs/{job_id}/events", params={"after": 0, "limit": 2})

    assert response.status_code == 200
    assert [event["message"] for event in response.json()] == ["event 0", "event 1"]


@pytest.mark.anyio
async def test_unexpected_error_never_exposes_exception_text(
    controller_client: AsyncClient,
    controller_store: ControllerStore,
    monkeypatch,
    caplog,
) -> None:
    def explode(_job_id: str):
        raise RuntimeError("private path /root/keys/age.key and token secret-value")

    monkeypatch.setattr(controller_store, "get_job", explode)
    response = await controller_client.get("/v1/jobs/anything")

    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert "age.key" not in response.text
    assert "secret-value" not in response.text
    assert "age.key" not in caplog.text
    assert "secret-value" not in caplog.text
