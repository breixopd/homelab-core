"""Version-one controller HTTP API served behind authenticated transports."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import re
import uuid
from dataclasses import dataclass
from datetime import timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from fastapi import Depends, FastAPI, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from toolkit.controller.bootstrap_api import (
    BootstrapInitializationError,
    bootstrap_phase,
    initialize_bootstrap,
    read_bootstrap_status,
    read_bootstrap_view,
)
from toolkit.controller.contracts import (
    ControllerHealth,
    DestroyInfraOperation,
    DestroyPlanRequest,
    DestroyPlanSpec,
    ErrorCode,
    ErrorEnvelope,
    IdentityOperation,
    JobKind,
    JobRequest,
    SealedInviteUserCommand,
    ServiceActionOperation,
)
from toolkit.controller.dashboard_api import read_dashboard_metrics_view, read_dashboard_view, read_portal_status
from toolkit.controller.deployment_api import DEPLOYMENT_JOB_KINDS, read_deployment_view
from toolkit.controller.desired_state_api import (
    DesiredStateConflictError,
    DesiredStateValidationError,
    create_machine,
    create_project,
    machine_retirement_blockers,
    read_dns_view,
    read_machines_view,
    read_projects_view,
    read_settings_view,
    remove_machine,
    remove_project,
    update_dns_public_ip,
    update_machine,
    update_settings,
)
from toolkit.controller.errors import ControllerAPIError
from toolkit.controller.identity_api import (
    DirectoryUnavailableError,
    InviteRequestRejectedError,
    activate_invite,
    preview_invite,
    read_account_view,
    read_directory_users,
)
from toolkit.controller.inventory_api import (
    InventoryRequestError,
    read_container_inventory,
    read_service_topology,
    read_services_view,
)
from toolkit.controller.jobs_api import read_jobs_view
from toolkit.controller.managed_hosts_api import create_managed_host, read_managed_hosts_view, update_managed_host
from toolkit.controller.operations_api import read_operations_view
from toolkit.controller.read_models import (
    AccountView,
    BootstrapCapabilityIssue,
    BootstrapInitializeRequest,
    BootstrapInitializeResult,
    BootstrapSessionExchange,
    BootstrapSessionGrant,
    BootstrapStatus,
    BootstrapView,
    ContainerInventory,
    DashboardMetrics,
    DashboardView,
    DeploymentView,
    DirectoryUsersView,
    DnsIpUpdate,
    DnsView,
    InviteActivationRequest,
    InviteActivationResult,
    InvitePreview,
    InvitePreviewRequest,
    MachineCreate,
    MachineRemove,
    MachinesView,
    MachineUpdate,
    ManagedHostCreate,
    ManagedHostsView,
    ManagedHostUpdate,
    OperationsView,
    PortalStatus,
    ProjectCreate,
    ProjectRemove,
    ProjectsView,
    SecretUpdateRequest,
    ServiceManagementView,
    ServiceSettingsUpdate,
    ServicesView,
    ServiceTopology,
    SettingsUpdate,
    SettingsView,
)
from toolkit.controller.service_management_api import (
    ServiceManagementNotFoundError,
    ServiceSettingValidationError,
    read_service_management,
    update_service_settings,
)
from toolkit.controller.settings_api import (
    SecretMutationError,
    generate_secret_values,
    read_secret_inventory,
    update_secret_values,
)
from toolkit.controller.store import (
    ApprovalError,
    BootstrapCapabilityError,
    ControllerStore,
    IdempotencyConflictError,
    JobConflictError,
    JobNotFoundError,
    JobQueueLimitError,
    PlanNotFoundError,
)
from toolkit.controller.webhooks_api import (
    WebhookAuthenticationError,
    WebhookConfigurationError,
    WebhookPayloadError,
    accept_grafana_alert,
)
from toolkit.core.async_utils import run_blocking
from toolkit.core.config.mutations import ConfigurationBusyError, ConfigurationUnavailableError, config_revision
from toolkit.core.config.storage import secrets_path
from toolkit.core.deploy.destructive_guard import RecoveryCheckpointRequiredError, require_verified_checkpoint
from toolkit.core.secrets.secrets import load_secrets_plaintext

if TYPE_CHECKING:
    from toolkit.controller.worker import ControllerWorker

logger = logging.getLogger(__name__)

_CORRELATION_ID = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
_MAX_REQUEST_BODY_BYTES = 64 * 1024
_ENABLED_API_KINDS = frozenset(
    {
        JobKind.GENERATE,
        JobKind.VERIFY,
        JobKind.DEPLOY,
        JobKind.RECOVER,
        JobKind.DNS_SYNC,
        JobKind.MAINTENANCE,
        JobKind.BACKUP_DRILL,
        JobKind.UPDATE,
        JobKind.RESTORE_DRILL,
        JobKind.HOST_RECONCILE,
        JobKind.HOST_REMOVE,
        JobKind.CONTAINER_ACTION,
        JobKind.SERVICE_ACTION,
        JobKind.CONFIG_APPLY,
        JobKind.IDENTITY,
        JobKind.SECRET_ROTATION,
    }
)
_DEPLOYMENT_MUTATION_KINDS = frozenset(
    {JobKind.DEPLOY, JobKind.RECOVER, JobKind.GENERATE, JobKind.UPDATE, JobKind.SECRET_ROTATION}
)
_HOST_MUTATION_KINDS = frozenset({JobKind.HOST_RECONCILE, JobKind.HOST_REMOVE})


@dataclass(frozen=True)
class ControllerPrincipal:
    identity: str
    transport: Literal["local", "ui"]


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ttl_seconds: int = Field(ge=1, le=300)
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: str = Field(min_length=1, max_length=128)


class CorrelationMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        supplied = headers.get(b"x-correlation-id", b"").decode("ascii", errors="ignore")
        correlation_id = supplied if _CORRELATION_ID.fullmatch(supplied) else str(uuid.uuid4())
        scope.setdefault("state", {})["correlation_id"] = correlation_id

        async def send_with_correlation(message):
            if message["type"] == "http.response.start":
                response_headers = list(message.get("headers", []))
                response_headers.append((b"x-correlation-id", correlation_id.encode("ascii")))
                message = {**message, "headers": response_headers}
            await send(message)

        await self.app(scope, receive, send_with_correlation)


class RequestBodyLimitMiddleware:
    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        method = str(scope.get("method", "GET")).upper()
        if scope["type"] != "http" or method not in {"POST", "PUT", "PATCH", "DELETE"}:
            await self.app(scope, receive, send)
            return

        body = bytearray()
        more = True
        while more:
            message = await receive()
            body.extend(message.get("body", b""))
            if len(body) > _MAX_REQUEST_BODY_BYTES:
                response = _error_response(413, "VALIDATION_ERROR", "Request body exceeds the controller limit")
                await response(scope, receive, send)
                return
            more = bool(message.get("more_body", False))

        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)


async def _principal(request: Request) -> ControllerPrincipal:
    transport = request.headers.get("X-Controller-Transport", "").strip().lower()
    if transport == "local":
        supplied = request.headers.get("X-Controller-Token", "")
        if hmac.compare_digest(supplied, request.app.state.local_transport_token):
            return ControllerPrincipal(identity="local:operator", transport="local")
        raise ControllerAPIError(403, "FORBIDDEN", "Local controller authentication failed")
    if transport == "ui":
        supplied = request.headers.get("X-Controller-Token", "")
        if hmac.compare_digest(supplied, request.app.state.ui_transport_token):
            return ControllerPrincipal(identity="ui:homelab-ui", transport="ui")
        raise ControllerAPIError(403, "FORBIDDEN", "UI controller authentication failed")
    raise ControllerAPIError(403, "FORBIDDEN", "An authenticated controller transport is required")


def _require_local(principal: ControllerPrincipal) -> None:
    if principal.transport != "local":
        raise ControllerAPIError(403, "FORBIDDEN", "This operation requires a local controller session")


def _require_ui_or_local(principal: ControllerPrincipal) -> None:
    if principal.transport in {"local", "ui"}:
        return
    raise ControllerAPIError(403, "FORBIDDEN", "This controller resource is not available to the caller")


def _require_job_submitter(principal: ControllerPrincipal) -> None:
    if principal.transport in {"local", "ui"}:
        return
    raise ControllerAPIError(403, "FORBIDDEN", "This controller principal cannot submit jobs")


def _authorize_job(store: ControllerStore, job_id: str, principal: ControllerPrincipal):
    job = store.get_job(job_id)
    if principal.transport != "local" and job.actor != principal.identity:
        raise ControllerAPIError(403, "FORBIDDEN", "Job access is not permitted")
    return job


def _error_response(
    status_code: int,
    code: ErrorCode,
    message: str,
    details: dict | None = None,
) -> JSONResponse:
    envelope = ErrorEnvelope.from_code(code, message, details)
    return JSONResponse(status_code=status_code, content=envelope.model_dump(mode="json"))


def create_controller_app(
    *,
    root: Path,
    store: ControllerStore,
    local_transport_token: str,
    ui_transport_token: str,
    worker: ControllerWorker | None = None,
) -> FastAPI:
    if len(local_transport_token) < 32 or len(ui_transport_token) < 32:
        raise ValueError("Controller transport tokens must contain at least 32 characters")
    if hmac.compare_digest(local_transport_token, ui_transport_token):
        raise ValueError("Controller transport tokens must be distinct")
    app = FastAPI(title="Homelab Controller", version="1", docs_url=None, redoc_url=None)
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(CorrelationMiddleware)
    app.state.root = root.resolve()
    app.state.store = store
    app.state.worker = worker
    app.state.local_transport_token = local_transport_token
    app.state.ui_transport_token = ui_transport_token

    @app.exception_handler(RequestValidationError)
    async def validation_error(_request: Request, exc: RequestValidationError):
        errors = [
            {
                "location": [str(part) for part in error.get("loc", ())],
                "type": str(error.get("type", "validation_error")),
                "message": str(error.get("msg", "Invalid value")),
            }
            for error in exc.errors()
        ]
        return _error_response(422, "VALIDATION_ERROR", "Request validation failed", {"errors": errors})

    @app.exception_handler(ControllerAPIError)
    async def controller_api_error(_request: Request, exc: ControllerAPIError):
        return _error_response(exc.status_code, exc.code, exc.message, exc.details)

    @app.exception_handler(JobNotFoundError)
    async def job_not_found(_request: Request, _exc: JobNotFoundError):
        return _error_response(404, "NOT_FOUND", "Job not found")

    @app.exception_handler(PlanNotFoundError)
    async def plan_not_found(_request: Request, _exc: PlanNotFoundError):
        return _error_response(404, "NOT_FOUND", "Plan not found")

    @app.exception_handler(IdempotencyConflictError)
    async def idempotency_conflict(_request: Request, _exc: IdempotencyConflictError):
        return _error_response(409, "CONFLICT", "Idempotency key conflicts with an existing request")

    @app.exception_handler(JobConflictError)
    async def job_conflict(_request: Request, _exc: JobConflictError):
        return _error_response(409, "CONFLICT", "Job state conflicts with the requested operation")

    @app.exception_handler(JobQueueLimitError)
    async def job_queue_limit(_request: Request, _exc: JobQueueLimitError):
        return _error_response(429, "OPERATION_REJECTED", "Webhook heal queue is at capacity")

    @app.exception_handler(ApprovalError)
    async def approval_error(_request: Request, _exc: ApprovalError):
        return _error_response(403, "FORBIDDEN", "Destructive approval was rejected")

    @app.exception_handler(RecoveryCheckpointRequiredError)
    async def checkpoint_required(_request: Request, _exc: RecoveryCheckpointRequiredError):
        return _error_response(409, "CHECKPOINT_REQUIRED", "A fresh verified recovery checkpoint is required")

    @app.exception_handler(SecretMutationError)
    async def secret_mutation_error(_request: Request, _exc: SecretMutationError):
        return _error_response(422, "VALIDATION_ERROR", "Secret update was rejected")

    @app.exception_handler(InventoryRequestError)
    async def inventory_request_error(_request: Request, _exc: InventoryRequestError):
        return _error_response(422, "VALIDATION_ERROR", "Service inventory request was rejected")

    @app.exception_handler(InviteRequestRejectedError)
    async def invite_request_rejected(_request: Request, _exc: InviteRequestRejectedError):
        return _error_response(403, "FORBIDDEN", "Invite activation request was rejected")

    @app.exception_handler(DirectoryUnavailableError)
    async def directory_unavailable(_request: Request, _exc: DirectoryUnavailableError):
        return _error_response(503, "OPERATION_REJECTED", "The identity directory is unavailable")

    @app.exception_handler(WebhookAuthenticationError)
    async def webhook_authentication_error(_request: Request, _exc: WebhookAuthenticationError):
        return _error_response(401, "FORBIDDEN", "Grafana webhook authentication failed")

    @app.exception_handler(WebhookConfigurationError)
    async def webhook_configuration_error(_request: Request, _exc: WebhookConfigurationError):
        return _error_response(503, "OPERATION_REJECTED", "Grafana webhook integration is unavailable")

    @app.exception_handler(WebhookPayloadError)
    async def webhook_payload_error(_request: Request, _exc: WebhookPayloadError):
        return _error_response(422, "VALIDATION_ERROR", "Grafana webhook payload was rejected")

    @app.exception_handler(DesiredStateConflictError)
    async def desired_state_conflict(_request: Request, _exc: DesiredStateConflictError):
        return _error_response(409, "CONFLICT", "Desired state changed; reload and retry")

    @app.exception_handler(DesiredStateValidationError)
    async def desired_state_validation(_request: Request, _exc: DesiredStateValidationError):
        return _error_response(422, "VALIDATION_ERROR", "Desired-state update was rejected")

    @app.exception_handler(ConfigurationUnavailableError)
    async def configuration_unavailable(_request: Request, _exc: ConfigurationUnavailableError):
        return _error_response(503, "CONTROLLER_UNAVAILABLE", "Canonical configuration is unavailable")

    @app.exception_handler(ConfigurationBusyError)
    async def configuration_busy(_request: Request, _exc: ConfigurationBusyError):
        return _error_response(409, "CONFIGURATION_BUSY", "Another operation is currently changing configuration")

    @app.exception_handler(BootstrapCapabilityError)
    async def bootstrap_capability_error(_request: Request, _exc: BootstrapCapabilityError):
        return _error_response(403, "FORBIDDEN", "Bootstrap authorization was rejected")

    @app.exception_handler(BootstrapInitializationError)
    async def bootstrap_initialization_error(_request: Request, _exc: BootstrapInitializationError):
        return _error_response(409, "CONFLICT", "Bootstrap initialization was rejected")

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception):
        traceback = exc.__traceback__
        while traceback and traceback.tb_next:
            traceback = traceback.tb_next
        location = "unknown"
        if traceback:
            code = traceback.tb_frame.f_code
            location = f"{Path(code.co_filename).name}:{traceback.tb_lineno}:{code.co_name}"
        logger.error(
            "Controller request failed correlation_id=%s error_type=%s location=%s",
            getattr(request.state, "correlation_id", "unknown"),
            type(exc).__name__,
            location,
        )
        return _error_response(500, "INTERNAL_ERROR", "The controller could not complete the request")

    @app.get("/v1/health", response_model=ControllerHealth)
    async def health():
        database_ok = store.journal_mode() == "wal"
        worker_ok = worker.is_healthy() if worker is not None else True
        secret_store_ok = True
        secret_store = secrets_path(app.state.root)
        if secret_store.is_file():
            try:
                await run_blocking(load_secrets_plaintext, secret_store)
            except Exception as exc:
                secret_store_ok = False
                logger.warning("Controller secret store readiness failed error_type=%s", type(exc).__name__)
        queued_jobs, running_jobs = store.active_job_counts()
        readiness_ok = database_ok and worker_ok and secret_store_ok
        result = ControllerHealth(
            status="ok" if readiness_ok else "degraded",
            version="1",
            database_ok=database_ok,
            worker_ok=worker_ok,
            secret_store_ok=secret_store_ok,
            queued_jobs=queued_jobs,
            running_jobs=running_jobs,
        )
        if not readiness_ok:
            return JSONResponse(status_code=503, content=result.model_dump(mode="json"))
        return result

    @app.post("/v1/bootstrap/capabilities", status_code=201)
    async def bootstrap_capability(
        principal: ControllerPrincipal = Depends(_principal),
    ) -> BootstrapCapabilityIssue:
        _require_local(principal)
        if await run_blocking(bootstrap_phase, app.state.root) != "uninitialized":
            raise ControllerAPIError(409, "CONFLICT", "Bootstrap is not available in the current state")
        return store.issue_bootstrap_capability(
            principal=principal.identity,
            ttl=timedelta(minutes=15),
        )

    @app.get("/v1/bootstrap/status")
    async def bootstrap_status(
        principal: ControllerPrincipal = Depends(_principal),
    ) -> BootstrapStatus:
        _require_ui_or_local(principal)
        return await run_blocking(read_bootstrap_status, app.state.root, store)

    @app.post("/v1/bootstrap/sessions", status_code=201)
    async def bootstrap_session(
        exchange: BootstrapSessionExchange,
        principal: ControllerPrincipal = Depends(_principal),
    ) -> BootstrapSessionGrant:
        _require_ui_or_local(principal)
        if await run_blocking(bootstrap_phase, app.state.root) != "uninitialized":
            raise ControllerAPIError(409, "CONFLICT", "Bootstrap is not available in the current state")
        return store.exchange_bootstrap_capability(
            exchange.capability_token,
            ttl=timedelta(minutes=15),
        )

    @app.get("/v1/bootstrap")
    async def bootstrap_view(
        request: Request,
        principal: ControllerPrincipal = Depends(_principal),
    ) -> BootstrapView:
        _require_ui_or_local(principal)
        return await run_blocking(
            read_bootstrap_view,
            app.state.root,
            store,
            request.headers.get("X-Bootstrap-Session", ""),
        )

    @app.post("/v1/bootstrap/initializations", status_code=201)
    async def bootstrap_initialization(
        request: BootstrapInitializeRequest,
        principal: ControllerPrincipal = Depends(_principal),
    ) -> BootstrapInitializeResult:
        _require_ui_or_local(principal)
        return await run_blocking(
            initialize_bootstrap,
            app.state.root,
            store,
            request,
            principal=principal.identity,
        )

    @app.get("/v1/settings/secrets")
    async def secret_inventory(principal: ControllerPrincipal = Depends(_principal)):
        _require_ui_or_local(principal)
        return await run_blocking(read_secret_inventory, app.state.root)

    @app.put("/v1/settings/secrets")
    async def secret_update(
        update: SecretUpdateRequest,
        principal: ControllerPrincipal = Depends(_principal),
    ):
        _require_ui_or_local(principal)
        return await run_blocking(update_secret_values, app.state.root, update)

    @app.post("/v1/settings/secrets/generation")
    async def secret_generation(principal: ControllerPrincipal = Depends(_principal)):
        _require_ui_or_local(principal)
        return await run_blocking(generate_secret_values, app.state.root)

    @app.get("/v1/services")
    async def services_view(
        family: bool = False,
        group: list[str] = Query(default=[]),
        principal: ControllerPrincipal = Depends(_principal),
    ) -> ServicesView:
        _require_ui_or_local(principal)
        return await run_blocking(
            read_services_view,
            app.state.root,
            family=family,
            groups=group,
        )

    @app.get("/v1/identity/account")
    async def account_view(
        group: list[str] = Query(default=[]),
        principal: ControllerPrincipal = Depends(_principal),
    ) -> AccountView:
        _require_ui_or_local(principal)
        return await run_blocking(read_account_view, app.state.root, groups=group)

    @app.get("/v1/identity/users")
    async def directory_users(principal: ControllerPrincipal = Depends(_principal)) -> DirectoryUsersView:
        _require_ui_or_local(principal)
        return await run_blocking(read_directory_users, app.state.root)

    @app.post("/v1/identity/invite-preview")
    async def invite_preview(
        request: InvitePreviewRequest,
        principal: ControllerPrincipal = Depends(_principal),
    ) -> InvitePreview:
        _require_ui_or_local(principal)
        return await run_blocking(preview_invite, app.state.root, request.token)

    @app.post("/v1/identity/invite-activation")
    async def invite_activation(
        request: InviteActivationRequest,
        principal: ControllerPrincipal = Depends(_principal),
    ) -> InviteActivationResult:
        _require_ui_or_local(principal)
        resource = f"invite:{hashlib.sha256(request.token.encode('utf-8')).hexdigest()[:16]}"
        try:
            result = await run_blocking(activate_invite, app.state.root, request)
        except InviteRequestRejectedError:
            store.append_audit(
                principal.identity,
                "INVITE_ACTIVATION",
                resource,
                "DENIED",
                {"reason": "request_rejected"},
            )
            raise
        store.append_audit(
            principal.identity,
            "INVITE_ACTIVATION",
            resource,
            "ALLOWED" if result.outcome == "activated" else "FAILED",
            {"outcome": result.outcome},
        )
        return result

    @app.post("/v1/integrations/grafana/alerts")
    async def grafana_alert(
        request: Request,
        principal: ControllerPrincipal = Depends(_principal),
    ):
        _require_ui_or_local(principal)
        raw_body = await request.body()
        receipt = await run_blocking(
            accept_grafana_alert,
            app.state.root,
            store,
            raw_body,
            signature=request.headers.get("X-Grafana-Alerting-Signature", ""),
            timestamp=request.headers.get("X-Grafana-Alerting-Signature-Timestamp", ""),
            content_type=request.headers.get("Content-Type", ""),
        )
        return JSONResponse(
            status_code=202 if receipt.outcome == "queued" else 200,
            content=receipt.model_dump(mode="json"),
        )

    @app.get("/v1/dashboard")
    async def dashboard_view(
        family: bool = False,
        group: list[str] = Query(default=[]),
        principal: ControllerPrincipal = Depends(_principal),
    ) -> DashboardView:
        _require_ui_or_local(principal)
        jobs = [] if family else store.recent_jobs(principal=None, limit=10)
        return await run_blocking(
            read_dashboard_view,
            app.state.root,
            family=family,
            groups=group,
            jobs=jobs,
        )

    @app.get("/v1/dashboard/metrics")
    async def dashboard_metrics(
        principal: ControllerPrincipal = Depends(_principal),
    ) -> DashboardMetrics:
        _require_ui_or_local(principal)
        return await run_blocking(read_dashboard_metrics_view, app.state.root)

    @app.get("/v1/dashboard/portal-status")
    async def dashboard_portal_status(
        principal: ControllerPrincipal = Depends(_principal),
    ) -> PortalStatus:
        _require_ui_or_local(principal)
        return await run_blocking(read_portal_status, app.state.root)

    @app.get("/v1/deployment")
    async def deployment_view(
        principal: ControllerPrincipal = Depends(_principal),
    ) -> DeploymentView:
        _require_ui_or_local(principal)
        jobs = store.active_jobs(
            principal=None,
            kinds=DEPLOYMENT_JOB_KINDS,
            limit=10,
        )
        return await run_blocking(read_deployment_view, app.state.root, jobs, principal.identity)

    @app.get("/v1/dns")
    async def dns_view(principal: ControllerPrincipal = Depends(_principal)) -> DnsView:
        _require_ui_or_local(principal)
        return await run_blocking(read_dns_view, app.state.root)

    @app.put("/v1/dns/public-ip")
    async def dns_public_ip(
        update: DnsIpUpdate,
        principal: ControllerPrincipal = Depends(_principal),
    ) -> DnsView:
        _require_ui_or_local(principal)
        return await run_blocking(update_dns_public_ip, app.state.root, update)

    @app.get("/v1/settings")
    async def settings_view(principal: ControllerPrincipal = Depends(_principal)) -> SettingsView:
        _require_ui_or_local(principal)
        return await run_blocking(read_settings_view, app.state.root)

    @app.put("/v1/settings")
    async def settings_update(
        update: SettingsUpdate,
        principal: ControllerPrincipal = Depends(_principal),
    ) -> SettingsView:
        _require_ui_or_local(principal)
        return await run_blocking(update_settings, app.state.root, update)

    @app.get("/v1/machines")
    async def machines_view(principal: ControllerPrincipal = Depends(_principal)) -> MachinesView:
        _require_ui_or_local(principal)
        return await run_blocking(read_machines_view, app.state.root)

    @app.post("/v1/machines", status_code=201)
    async def machine_create(
        request: MachineCreate,
        principal: ControllerPrincipal = Depends(_principal),
    ) -> MachinesView:
        _require_ui_or_local(principal)
        return await run_blocking(create_machine, app.state.root, request)

    @app.put("/v1/machines/{machine_id}")
    async def machine_update(
        machine_id: str,
        request: MachineUpdate,
        principal: ControllerPrincipal = Depends(_principal),
    ) -> MachinesView:
        _require_ui_or_local(principal)
        return await run_blocking(update_machine, app.state.root, machine_id, request)

    @app.delete("/v1/machines/{machine_id}")
    async def machine_remove(
        machine_id: str,
        request: MachineRemove,
        principal: ControllerPrincipal = Depends(_principal),
    ) -> MachinesView:
        _require_ui_or_local(principal)
        return await run_blocking(remove_machine, app.state.root, machine_id, request)

    @app.get("/v1/projects")
    async def projects_view(principal: ControllerPrincipal = Depends(_principal)) -> ProjectsView:
        _require_ui_or_local(principal)
        return await run_blocking(read_projects_view, app.state.root)

    @app.post("/v1/projects", status_code=201)
    async def project_create(
        request: ProjectCreate,
        principal: ControllerPrincipal = Depends(_principal),
    ) -> ProjectsView:
        _require_ui_or_local(principal)
        return await run_blocking(create_project, app.state.root, request)

    @app.delete("/v1/projects/{subdomain}")
    async def project_remove(
        subdomain: str,
        request: ProjectRemove,
        principal: ControllerPrincipal = Depends(_principal),
    ) -> ProjectsView:
        _require_ui_or_local(principal)
        if subdomain != request.subdomain:
            raise ControllerAPIError(422, "VALIDATION_ERROR", "Project identity does not match the request path")
        return await run_blocking(remove_project, app.state.root, request)

    @app.get("/v1/containers")
    async def container_inventory(
        principal: ControllerPrincipal = Depends(_principal),
    ) -> ContainerInventory:
        _require_ui_or_local(principal)
        return await run_blocking(read_container_inventory, app.state.root)

    @app.get("/v1/services/topology")
    async def service_topology(
        principal: ControllerPrincipal = Depends(_principal),
    ) -> ServiceTopology:
        _require_ui_or_local(principal)
        return await run_blocking(read_service_topology, app.state.root)

    @app.get("/v1/services/{service}/management")
    async def service_management(
        service: str,
        collect_status: bool = True,
        principal: ControllerPrincipal = Depends(_principal),
    ) -> ServiceManagementView:
        _require_ui_or_local(principal)
        try:
            return await run_blocking(
                read_service_management,
                app.state.root,
                service,
                collect_status=collect_status,
            )
        except ServiceManagementNotFoundError as exc:
            raise ControllerAPIError(404, "NOT_FOUND", "Service management resource was not found") from exc

    @app.get("/v1/operations")
    async def operations_view(
        principal: ControllerPrincipal = Depends(_principal),
    ) -> OperationsView:
        _require_ui_or_local(principal)
        return await run_blocking(read_operations_view, app.state.root)

    @app.get("/v1/hosts")
    async def managed_hosts_view(
        principal: ControllerPrincipal = Depends(_principal),
    ) -> ManagedHostsView:
        _require_ui_or_local(principal)
        return await run_blocking(read_managed_hosts_view, app.state.root)

    @app.post("/v1/hosts", status_code=201)
    async def managed_host_create(
        request: ManagedHostCreate,
        principal: ControllerPrincipal = Depends(_principal),
    ) -> ManagedHostsView:
        _require_ui_or_local(principal)
        return await run_blocking(create_managed_host, app.state.root, request)

    @app.put("/v1/hosts/{name}")
    async def managed_host_update(
        name: str,
        request: ManagedHostUpdate,
        principal: ControllerPrincipal = Depends(_principal),
    ) -> ManagedHostsView:
        _require_ui_or_local(principal)
        return await run_blocking(update_managed_host, app.state.root, name, request)

    @app.patch("/v1/services/{service}/settings")
    async def service_settings_update(
        service: str,
        update: ServiceSettingsUpdate,
        principal: ControllerPrincipal = Depends(_principal),
    ) -> ServiceManagementView:
        _require_ui_or_local(principal)
        try:
            return await run_blocking(update_service_settings, app.state.root, service, update)
        except ServiceManagementNotFoundError as exc:
            raise ControllerAPIError(404, "NOT_FOUND", "Service settings resource was not found") from exc
        except ServiceSettingValidationError as exc:
            raise ControllerAPIError(422, "VALIDATION_ERROR", "Service settings were rejected") from exc

    @app.post("/v1/plans/destruction", status_code=201)
    async def create_destruction_plan(
        request: DestroyPlanRequest,
        principal: ControllerPrincipal = Depends(_principal),
    ):
        if request.action == "destroy_all":
            _require_local(principal)
        else:
            _require_ui_or_local(principal)
        from toolkit.core.config.config import load_config
        from toolkit.core.config.storage import config_path

        cfg = await run_blocking(load_config, config_path(app.state.root))
        if request.action == "destroy_all":
            if set(request.scopes) != set(cfg.enabled_nodes):
                raise ControllerAPIError(422, "VALIDATION_ERROR", "Destruction must include every enabled machine")
        else:
            if len(request.scopes) != 1:
                raise ControllerAPIError(422, "VALIDATION_ERROR", "Retirement must target exactly one machine")
            blockers = await run_blocking(machine_retirement_blockers, app.state.root, cfg, request.scopes[0])
            if blockers:
                raise ControllerAPIError(
                    422,
                    "VALIDATION_ERROR",
                    "Machine retirement is blocked",
                    {"blockers": blockers},
                )
        checkpoint = await run_blocking(
            require_verified_checkpoint,
            app.state.root,
            request.scopes,
            timedelta(days=7),
        )
        evidence_json = json.dumps(checkpoint.evidence, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
        spec = DestroyPlanSpec(
            action=request.action,
            scopes=request.scopes,
            config_revision=await run_blocking(config_revision, app.state.root),
            checkpoint_id=checkpoint.checkpoint_id,
            checkpoint_verified_at=checkpoint.verified_at,
            evidence_digest=hashlib.sha256(evidence_json.encode("utf-8")).hexdigest(),
        )
        return store.create_plan(spec, actor=principal.identity)

    @app.get("/v1/plans/{plan_id}")
    async def get_plan(plan_id: str, principal: ControllerPrincipal = Depends(_principal)):
        _require_ui_or_local(principal)
        plan = store.get_plan(plan_id)
        if plan.actor != principal.identity:
            raise ControllerAPIError(403, "FORBIDDEN", "Plan access is not permitted")
        if plan.spec.action == "destroy_all":
            _require_local(principal)
        return plan

    @app.post("/v1/plans/{plan_id}/approval", status_code=201)
    async def approve_plan(
        plan_id: str,
        approval: ApprovalRequest,
        principal: ControllerPrincipal = Depends(_principal),
    ):
        _require_ui_or_local(principal)
        plan = store.get_plan(plan_id)
        if plan.spec.action == "destroy_all":
            _require_local(principal)
        if plan.actor != principal.identity or plan.plan_hash != approval.plan_hash:
            raise ApprovalError("approval does not match the actor-bound plan")
        expected_confirmation = (
            "DESTROY ALL MANAGED INFRASTRUCTURE"
            if plan.spec.action == "destroy_all"
            else f"RETIRE MACHINE {plan.spec.scopes[0]}"
        )
        if not hmac.compare_digest(approval.confirmation, expected_confirmation):
            raise ApprovalError("approval confirmation does not match the destructive plan")
        return store.issue_approval(
            plan_id,
            actor=principal.identity,
            ttl=timedelta(seconds=approval.ttl_seconds),
        )

    @app.post("/v1/jobs")
    async def submit_job(request: JobRequest, principal: ControllerPrincipal = Depends(_principal)):
        if isinstance(request.operation, DestroyInfraOperation):
            if request.operation.action == "destroy_all":
                _require_local(principal)
            else:
                _require_ui_or_local(principal)
            job, created = store.submit_job(request, principal=principal.identity)
            return JSONResponse(status_code=201 if created else 200, content=job.model_dump(mode="json"))
        _require_job_submitter(principal)
        if request.kind not in _ENABLED_API_KINDS:
            raise ControllerAPIError(403, "FORBIDDEN", "This controller operation is not enabled")
        if isinstance(request.operation, ServiceActionOperation):
            try:
                service_view = await run_blocking(
                    read_service_management,
                    app.state.root,
                    request.operation.service,
                    collect_status=False,
                )
            except ServiceManagementNotFoundError as exc:
                raise ControllerAPIError(422, "VALIDATION_ERROR", "Service action was rejected") from exc
            action = next(
                (item for item in service_view.actions if item.id == request.operation.action),
                None,
            )
            if action is None or not action.can_run:
                raise ControllerAPIError(422, "VALIDATION_ERROR", "Service action was rejected")
        if isinstance(request.operation, IdentityOperation) and isinstance(
            request.operation.command,
            SealedInviteUserCommand,
        ):
            raise ControllerAPIError(422, "VALIDATION_ERROR", "Internal identity payloads cannot be submitted")
        if request.kind in _DEPLOYMENT_MUTATION_KINDS:
            job, created = store.submit_job(
                request,
                principal=principal.identity,
                active_limit=1,
                active_kinds=_DEPLOYMENT_MUTATION_KINDS,
            )
        elif request.kind is JobKind.IDENTITY:
            job, created = store.submit_job(request, principal=principal.identity, active_limit=1)
        elif request.kind is JobKind.VERIFY:
            job, created = store.submit_job(request, principal=principal.identity, active_limit=1)
        elif request.kind in _HOST_MUTATION_KINDS:
            job, created = store.submit_job(
                request,
                principal=principal.identity,
                active_limit=1,
                active_kinds=_HOST_MUTATION_KINDS,
            )
        elif request.kind in {
            JobKind.MAINTENANCE,
            JobKind.BACKUP_DRILL,
            JobKind.RESTORE_DRILL,
            JobKind.SERVICE_ACTION,
        }:
            job, created = store.submit_job(request, principal=principal.identity, active_limit=1)
        else:
            job, created = store.submit_job(request, principal=principal.identity)
        return JSONResponse(status_code=201 if created else 200, content=job.model_dump(mode="json"))

    @app.get("/v1/jobs")
    async def list_jobs(
        limit: int = Query(default=100, ge=1, le=200),
        principal: ControllerPrincipal = Depends(_principal),
    ):
        _require_ui_or_local(principal)
        principal_scope = None if principal.transport == "local" else principal.identity
        jobs = store.recent_jobs(principal=principal_scope, limit=limit)
        return read_jobs_view(jobs)

    @app.get("/v1/jobs/{job_id}")
    async def get_job(job_id: str, principal: ControllerPrincipal = Depends(_principal)):
        return _authorize_job(store, job_id, principal)

    @app.post("/v1/jobs/{job_id}/cancellation")
    async def cancel_job(job_id: str, principal: ControllerPrincipal = Depends(_principal)):
        _authorize_job(store, job_id, principal)
        return store.request_cancel(job_id, principal=principal.identity)

    @app.get("/v1/jobs/{job_id}/events")
    async def job_events(
        job_id: str,
        after: int = 0,
        limit: int = 200,
        principal: ControllerPrincipal = Depends(_principal),
    ):
        if after < 0:
            raise ControllerAPIError(422, "VALIDATION_ERROR", "Event sequence must not be negative")
        if limit < 1 or limit > 500:
            raise ControllerAPIError(422, "VALIDATION_ERROR", "Event replay limit must be between 1 and 500")
        _authorize_job(store, job_id, principal)
        return store.events_after(job_id, after, limit=limit)

    return app
