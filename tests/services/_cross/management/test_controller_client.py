from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from toolkit.controller.client import (
    ControllerClient,
    ControllerProtocolError,
    ControllerRejectedError,
    ControllerUnavailableError,
    controller_client_from_environment,
)
from toolkit.controller.contracts import DestroyPlanRequest, JobRequest, VerifyOperation
from toolkit.controller.read_models import (
    BootstrapDesiredState,
    BootstrapInitializeRequest,
    DirectoryUsersView,
    InviteActivationRequest,
    MachineCreate,
    SecretUpdateRequest,
)
from toolkit.core.machines import MachineSpec


def _client(handler) -> ControllerClient:
    return ControllerClient._from_transport(httpx.MockTransport(handler), base_url="http://controller")


def test_for_uds_configures_exact_socket(monkeypatch, tmp_path: Path) -> None:
    captured: dict[str, str] = {}

    def transport(*, uds: str):
        captured["uds"] = uds

        def handle(request: httpx.Request) -> httpx.Response:
            assert request.headers["x-controller-transport"] == "local"
            assert request.headers["x-controller-token"] == "local-token-for-tests-000000000000"
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "version": "1",
                    "database_ok": True,
                    "worker_ok": True,
                    "secret_store_ok": True,
                    "queued_jobs": 0,
                    "running_jobs": 0,
                },
            )

        return httpx.MockTransport(handle)

    monkeypatch.setattr("toolkit.controller.client.httpx.HTTPTransport", transport)
    client = ControllerClient.for_uds(
        tmp_path / "controller.sock",
        role="local",
        token="local-token-for-tests-000000000000",
    )

    assert client.health().status == "ok"
    assert captured == {"uds": str(tmp_path / "controller.sock")}


def test_environment_factory_rejects_unimplemented_remote_transport(monkeypatch) -> None:
    monkeypatch.setenv("HOMELAB_CONTROLLER_URL", "https://controller.internal")

    with pytest.raises(ValueError, match="not implemented"):
        controller_client_from_environment()


def test_environment_factory_loads_role_scoped_uds_token(monkeypatch, tmp_path: Path) -> None:
    token_path = tmp_path / "ui.token"
    token_path.write_text("ui-token-for-environment-test-000000000000")
    token_path.chmod(0o600)
    captured: dict[str, object] = {}

    def for_uds(socket_path: Path, *, role: str, token: str, timeout: float = 10.0):
        captured.update(socket_path=socket_path, role=role, token=token, timeout=timeout)
        return object()

    monkeypatch.delenv("HOMELAB_CONTROLLER_URL", raising=False)
    monkeypatch.setenv("HOMELAB_CONTROLLER_SOCKET", str(tmp_path / "controller.sock"))
    monkeypatch.setenv("HOMELAB_CONTROLLER_ROLE", "ui")
    monkeypatch.setenv("HOMELAB_CONTROLLER_TOKEN_FILE", str(token_path))
    monkeypatch.setattr(ControllerClient, "for_uds", staticmethod(for_uds))

    client = controller_client_from_environment()

    assert client is not None
    assert captured == {
        "socket_path": tmp_path / "controller.sock",
        "role": "ui",
        "token": "ui-token-for-environment-test-000000000000",
        "timeout": 30.0,
    }


def test_environment_factory_validates_controller_timeout(monkeypatch, tmp_path: Path) -> None:
    token_path = tmp_path / "local.token"
    token_path.write_text("local-token-for-environment-test-0000000000")
    token_path.chmod(0o600)
    monkeypatch.delenv("HOMELAB_CONTROLLER_URL", raising=False)
    monkeypatch.setenv("HOMELAB_CONTROLLER_ROLE", "local")
    monkeypatch.setenv("HOMELAB_CONTROLLER_TOKEN_FILE", str(token_path))
    monkeypatch.setenv("HOMELAB_CONTROLLER_TIMEOUT_SECONDS", "0")

    with pytest.raises(ValueError, match="between 1 and 120"):
        controller_client_from_environment()


def test_environment_factory_rejects_unknown_uds_role_before_reading_token(monkeypatch) -> None:
    monkeypatch.delenv("HOMELAB_CONTROLLER_URL", raising=False)
    monkeypatch.setenv("HOMELAB_CONTROLLER_ROLE", "administrator")

    with pytest.raises(ValueError, match="local or ui"):
        controller_client_from_environment()


def test_submit_parses_typed_job_record() -> None:
    request = JobRequest(idempotency_key="request-12345678", operation=VerifyOperation())

    def handler(http_request: httpx.Request) -> httpx.Response:
        payload = json.loads(http_request.content)
        assert payload == request.model_dump(mode="json")
        return httpx.Response(
            201,
            json={
                "job_id": "job-123456789012",
                "request": payload,
                "state": "QUEUED",
                "actor": "local-operator",
                "created_at": "2026-07-10T00:00:00Z",
                "updated_at": "2026-07-10T00:00:00Z",
            },
        )

    job = _client(handler).submit(request)
    assert job.state.value == "QUEUED"


def test_jobs_requests_a_bounded_history_and_parses_the_typed_view() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/jobs"
        assert request.url.params.get("limit") == "25"
        return httpx.Response(
            200,
            json={
                "jobs": [
                    {
                        "job_id": "job-123456789012",
                        "kind": "VERIFY",
                        "state": "RUNNING",
                        "created_at": "2026-07-10T00:00:00Z",
                        "updated_at": "2026-07-10T00:01:00Z",
                        "can_cancel": True,
                        "error_code": "",
                    }
                ],
                "queued": 0,
                "running": 1,
                "attention": 0,
                "succeeded": 0,
            },
        )

    view = _client(handler).jobs(limit=25)

    assert view.running == 1
    assert view.jobs[0].job_id == "job-123456789012"
    assert view.jobs[0].state.value == "RUNNING"


def test_service_management_client_parses_typed_capabilities() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "GET"
        assert request.url.path == "/v1/services/music-sync/management"
        assert request.url.params["collect_status"] == "false"
        return httpx.Response(
            200,
            json={
                "revision": "a" * 64,
                "service": "music-sync",
                "label": "Music Sync",
                "description": "Import music libraries",
                "category": "media",
                "node": "media",
                "enabled": True,
                "status_available": True,
                "settings": [],
                "actions": [],
                "metrics": [
                    {
                        "key": "tracks",
                        "label": "Imported tracks",
                        "unit": "count",
                        "precision": 0,
                        "value": 42,
                    }
                ],
            },
        )

    view = _client(handler).service_management("music-sync", collect_status=False)

    assert view.service == "music-sync"
    assert view.metrics[0].value == 42


def test_service_settings_client_sends_revisioned_partial_update() -> None:
    from toolkit.controller.read_models import ServiceSettingsUpdate

    update = ServiceSettingsUpdate(expected_revision="a" * 64, values={"interval-minutes": 30})

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PATCH"
        assert request.url.path == "/v1/services/music-sync/settings"
        assert json.loads(request.content) == update.model_dump(mode="json")
        return httpx.Response(
            200,
            json={
                "revision": "b" * 64,
                "service": "music-sync",
                "label": "Music Sync",
                "description": "Import music libraries",
                "category": "media",
                "node": "media",
                "enabled": True,
                "status_available": False,
                "settings": [],
                "actions": [],
                "metrics": [],
            },
        )

    view = _client(handler).update_service_settings("music-sync", update)

    assert view.revision == "b" * 64


def test_destruction_client_sends_server_plan_request_and_explicit_approval() -> None:
    plan_json = {
        "plan_id": "plan-identifier-1234",
        "actor": "local:operator",
        "spec": {
            "action": "destroy_all",
            "scopes": ["media", "infra", "apps"],
            "config_revision": "d" * 64,
            "checkpoint_id": "a" * 32,
            "checkpoint_verified_at": "2026-07-10T00:00:00Z",
            "evidence_digest": "b" * 64,
        },
        "plan_hash": "c" * 64,
        "created_at": "2026-07-10T00:00:00Z",
    }

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/plans/destruction":
            assert json.loads(request.content) == {
                "action": "destroy_all",
                "scopes": ["media", "infra", "apps"],
            }
            return httpx.Response(201, json=plan_json)
        if request.method == "GET":
            return httpx.Response(200, json=plan_json)
        assert json.loads(request.content) == {
            "ttl_seconds": 300,
            "plan_hash": "c" * 64,
            "confirmation": "DESTROY ALL MANAGED INFRASTRUCTURE",
        }
        return httpx.Response(
            201,
            json={
                "plan_id": plan_json["plan_id"],
                "actor": "local:operator",
                "token": "approval-token-123456789",
                "expires_at": "2026-07-10T00:05:00Z",
            },
        )

    client = _client(handler)
    request = DestroyPlanRequest(action="destroy_all", scopes=["media", "infra", "apps"])
    plan = client.create_destruction_plan(request)

    assert plan.spec.scopes == ["media", "infra", "apps"]
    assert client.get_plan(plan.plan_id) == plan
    grant = client.approve_plan(
        plan.plan_id,
        plan_hash=plan.plan_hash,
        confirmation="DESTROY ALL MANAGED INFRASTRUCTURE",
    )
    assert grant.actor == "local:operator"


def test_secret_client_sends_values_but_only_accepts_non_secret_result() -> None:
    canary = "secret-canary-that-must-not-be-returned"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "PUT"
        assert request.url.path == "/v1/settings/secrets"
        assert json.loads(request.content) == {"values": {"CF_API_TOKEN": canary}}
        return httpx.Response(
            200,
            json={
                "changed_names": ["CF_API_TOKEN"],
                "inventory": {
                    "owner_email": "owner@example.test",
                    "storage_mode": "encrypted",
                    "encryption_available": True,
                    "entries": [
                        {
                            "name": "CF_API_TOKEN",
                            "isConfigured": True,
                            "tier": "user",
                            "description": "Cloudflare API token",
                        }
                    ],
                },
            },
        )

    result = _client(handler).update_secrets(SecretUpdateRequest(values={"CF_API_TOKEN": canary}))

    assert result.changed_names == ["CF_API_TOKEN"]
    assert "secret-canary" not in result.model_dump_json()


def test_services_client_sends_repeated_group_parameters() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/services"
        assert request.url.params.get("family") == "true"
        assert request.url.params.get_list("group") == ["homelab-media", "homelab-cloud"]
        return httpx.Response(
            200,
            json={
                "domain": "example.test",
                "categories": [],
                "bookmark_groups": [],
                "family_sections": [],
                "tier_labels": ["Media", "Cloud"],
            },
        )

    view = _client(handler).services_view(
        family=True,
        groups=["homelab-media", "homelab-cloud"],
    )

    assert view.tier_labels == ["Media", "Cloud"]


def test_machine_client_posts_typed_desired_state() -> None:
    spec = MachineSpec(
        managed=False,
        hostname="worker-01",
        address="10.10.10.20",
        gateway="10.10.10.1",
        vmid=820,
        labels=("compute",),
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/v1/machines"
        payload = json.loads(request.content)
        assert payload["machine_id"] == "worker-east"
        return httpx.Response(
            201,
            json={
                "revision": "b" * 64,
                "machines": [
                    {
                        "machine_id": "worker-east",
                        "spec": spec.model_dump(mode="json"),
                        "services": [],
                        "projects": [],
                        "can_remove": False,
                        "removal_blockers": ["machine is enabled"],
                        "can_retire": False,
                        "retirement_blockers": ["external machines are removed without infrastructure retirement"],
                    }
                ],
                "templates": [],
            },
        )

    view = _client(handler).create_machine(
        MachineCreate(expected_revision="a" * 64, machine_id="worker-east", spec=spec)
    )

    assert view.machines[0].machine_id == "worker-east"


def test_account_client_sends_groups_and_parses_display_safe_view() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/identity/account"
        assert request.url.params.get_list("group") == ["homelab-media"]
        return httpx.Response(
            200,
            json={
                "domain": "example.test",
                "auth_url": "https://auth.example.test",
                "sections": [],
                "tier_labels": ["Media"],
            },
        )

    view = _client(handler).account_view(groups=["homelab-media"])

    assert view.auth_url == "https://auth.example.test"


def test_directory_users_client_parses_safe_typed_view() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/identity/users"
        return httpx.Response(
            200,
            json={
                "domain": "example.test",
                "users": [],
                "group_options": [
                    {"name": "homelab-media", "label": "Media", "description": "Media", "is_default": True},
                    {"name": "homelab-cloud", "label": "Cloud", "description": "Cloud", "is_default": False},
                    {"name": "homelab-admin", "label": "Admin", "description": "Admin", "is_default": False},
                ],
                "invites_enabled": False,
                "invite_disabled_reason": "Email is disabled.",
            },
        )

    view = _client(handler).directory_users()

    assert isinstance(view, DirectoryUsersView)
    assert view.invites_enabled is False


def test_invite_activation_client_posts_secrets_only_in_request_body() -> None:
    token = "opaque-token-canary"
    password = "password-canary-123"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/identity/invite-activation"
        assert token not in str(request.url)
        body = json.loads(request.content)
        assert body["token"] == token
        assert body["password"] == password
        return httpx.Response(200, json={"outcome": "activated", "secure_cookie": True})

    result = _client(handler).activate_invite(
        InviteActivationRequest(
            token=token,
            activation_csrf="a" * 64,
            origin="https://homelab.example.test",
            password=password,
        )
    )

    assert result.outcome == "activated"
    assert token not in result.model_dump_json()
    assert password not in result.model_dump_json()


def test_invite_preview_client_keeps_bearer_out_of_url() -> None:
    token = "opaque-token-canary"

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/identity/invite-preview"
        assert token not in str(request.url)
        assert json.loads(request.content) == {"token": token}
        return httpx.Response(
            200,
            json={
                "valid": False,
                "domain": "example.test",
                "secure_cookie": True,
                "cookie_max_age_seconds": 259200,
                "activation_csrf": "",
                "display_name": "",
                "email": "",
                "sections": [],
            },
        )

    result = _client(handler).invite_preview(token)

    assert result.valid is False
    assert token not in result.model_dump_json()


def test_grafana_client_preserves_raw_body_and_exact_auth_headers() -> None:
    body = b'{"status": "firing", "whitespace": true}'

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/integrations/grafana/alerts"
        assert request.content == body
        assert request.headers["content-type"] == "application/json"
        assert request.headers["x-grafana-alerting-signature"] == "a" * 64
        assert request.headers["x-grafana-alerting-signature-timestamp"] == "1800000000"
        return httpx.Response(200, json={"outcome": "ignored", "reason": "no_firing_services", "jobs": []})

    result = _client(handler).accept_grafana_alert(
        body,
        signature="a" * 64,
        timestamp="1800000000",
        content_type="application/json",
    )

    assert result.outcome == "ignored"


def test_bootstrap_client_keeps_credentials_out_of_urls_and_parses_typed_resources() -> None:
    capability = "00000000-0000-4000-8000-000000000000.capability-secret-value"
    session = "10000000-0000-4000-8000-000000000000.session-secret-value"
    credential = "cloudflare-secret-canary"

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/bootstrap/capabilities":
            return httpx.Response(201, json={"token": capability, "expires_at": "2026-07-10T00:15:00Z"})
        if request.url.path == "/v1/bootstrap/sessions":
            assert capability not in str(request.url)
            assert json.loads(request.content) == {"capability_token": capability}
            return httpx.Response(201, json={"session_token": session, "expires_at": "2026-07-10T00:15:00Z"})
        if request.url.path == "/v1/bootstrap/status":
            return httpx.Response(
                200,
                json={"phase": "uninitialized", "has_active_capability": False, "has_active_session": True},
            )
        if request.url.path == "/v1/bootstrap" and request.method == "GET":
            assert session not in str(request.url)
            assert request.headers["x-bootstrap-session"] == session
            return httpx.Response(
                200,
                json={
                    "status": {
                        "phase": "uninitialized",
                        "has_active_capability": False,
                        "has_active_session": True,
                    },
                    "categories": [],
                    "service_settings": [],
                    "service_secrets": [],
                },
            )
        assert request.url.path == "/v1/bootstrap/initializations"
        assert session not in str(request.url)
        assert credential not in str(request.url)
        payload = json.loads(request.content)
        assert payload["session_token"] == session
        assert payload["credential_values"]["CLOUDFLARE_API_TOKEN"] == credential
        return httpx.Response(
            201,
            json={
                "outcome": "initialized",
                "phase": "ready",
                "config_revision": "a" * 64,
                "configured_secret_names": ["CLOUDFLARE_API_TOKEN"],
            },
        )

    client = _client(handler)
    assert client.issue_bootstrap_capability().token == capability
    assert client.exchange_bootstrap_capability(capability).session_token == session
    assert client.bootstrap_status().phase == "uninitialized"
    assert client.bootstrap_view(session).status.has_active_session is True
    result = client.initialize_bootstrap(
        BootstrapInitializeRequest(
            session_token=session,
            desired_state=BootstrapDesiredState(
                domain="home.example.com",
                email="operator@example.com",
                timezone="Europe/Madrid",
                proxmox_api_url="https://192.0.2.10:8006",
                proxmox_node="pve",
                proxmox_storage="local-zfs",
                service_settings={"media-library": {"server": "jellyfin"}},
            ),
            credential_values={"CLOUDFLARE_API_TOKEN": credential},
        )
    )
    assert result.phase == "ready"
    assert session not in result.model_dump_json()
    assert credential not in result.model_dump_json()


def test_bootstrap_initialization_uses_extended_operation_timeout() -> None:
    request_timeout = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal request_timeout
        request_timeout = request.extensions["timeout"]
        return httpx.Response(
            201,
            json={
                "outcome": "initialized",
                "phase": "ready",
                "config_revision": "a" * 64,
                "configured_secret_names": [],
            },
        )

    _client(handler).initialize_bootstrap(
        BootstrapInitializeRequest(
            session_token="10000000-0000-4000-8000-000000000000.session-secret-value",
            desired_state=BootstrapDesiredState(
                domain="home.example.com",
                email="operator@example.com",
                timezone="Europe/Madrid",
                proxmox_api_url="https://192.0.2.10:8006",
                proxmox_node="pve",
                proxmox_storage="local-zfs",
                service_settings={"gluetun": {"enabled": False}},
            ),
            credential_values={"CLOUDFLARE_API_TOKEN": "value"},
        )
    )

    assert request_timeout is not None
    assert request_timeout["read"] == 60.0


def test_dashboard_client_parses_typed_metric_snapshot() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/dashboard/metrics"
        return httpx.Response(
            200,
            json={
                "cpu": 15.5,
                "memory": None,
                "disk": None,
                "containers": 12,
                "targets_up": 9,
                "targets_down": 1,
                "cpu_history": [{"timestamp_ms": 1000, "value": 14.0}],
                "memory_history": [{"timestamp_ms": 1000, "value": 32.0}],
                "disk_history": [{"timestamp_ms": 1000, "value": 48.0}],
            },
        )

    metrics = _client(handler).dashboard_metrics()

    assert metrics.cpu == 15.5
    assert metrics.cpu_history[0].timestamp_ms == 1000
    assert metrics.memory_history[0].value == 32.0
    assert metrics.disk_history[0].value == 48.0


def test_dashboard_client_parses_portal_status_snapshot() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/dashboard/portal-status"
        return httpx.Response(
            200,
            json={
                "checked_at": "2026-07-28T12:00:00Z",
                "complete": True,
                "unavailable_nodes": 0,
                "services": {"prometheus": "online"},
            },
        )

    status = _client(handler).portal_status()

    assert status.complete is True
    assert status.services == {"prometheus": "online"}


def test_deployment_client_parses_bounded_controller_view() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/deployment"
        return httpx.Response(
            200,
            json={
                "state": "config_only",
                "enabled_targets": ["infra", "apps", "media"],
                "node_count": 3,
                "total_services": 24,
                "category_count": 6,
                "generated_config_count": 1,
                "step_labels": {"preflight": "Pre-flight checks"},
                "preflight": [{"check_id": "config", "label": "Config", "ok": True}],
                "preflight_ok": True,
                "active_jobs": [],
            },
        )

    view = _client(handler).deployment_view()

    assert view.node_count == 3
    assert view.preflight[0].check_id == "config"


def test_rejected_request_parses_stable_error_and_correlation_id() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            409,
            headers={"x-correlation-id": "correlation-1234"},
            json={"error": {"code": "CONFLICT", "message": "Already exists", "details": {}}},
        )

    with pytest.raises(ControllerRejectedError) as caught:
        _client(handler).get_job("job-123456789012")

    assert caught.value.code == "CONFLICT"
    assert caught.value.correlation_id == "correlation-1234"
    assert caught.value.status_code == 409


def test_malformed_success_response_is_protocol_error() -> None:
    client = _client(lambda _request: httpx.Response(200, json={"state": "made-up"}))

    with pytest.raises(ControllerProtocolError):
        client.get_job("job-123456789012")


def test_non_json_error_is_protocol_error_without_body_leak() -> None:
    client = _client(lambda _request: httpx.Response(502, text="upstream token=secret-value"))

    with pytest.raises(ControllerProtocolError) as caught:
        client.get_job("job-123456789012")

    assert "secret-value" not in str(caught.value)


def test_transport_failure_is_not_retried() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        raise httpx.ConnectError("socket unavailable", request=request)

    request = JobRequest(idempotency_key="request-12345678", operation=VerifyOperation())
    with pytest.raises(ControllerUnavailableError):
        _client(handler).submit(request)

    assert attempts == 1


def test_event_replay_sends_after_sequence() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.params["after"] == "41"
        assert request.url.params["limit"] == "100"
        return httpx.Response(
            200,
            json=[
                {
                    "job_id": "job-123456789012",
                    "sequence": 42,
                    "timestamp": "2026-07-10T00:00:00Z",
                    "level": "INFO",
                    "message": "continued",
                    "payload": {},
                }
            ],
        )

    events = _client(handler).events("job-123456789012", after=41, limit=100)
    assert [event.sequence for event in events] == [42]
