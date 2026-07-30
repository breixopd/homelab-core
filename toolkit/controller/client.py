"""Strict client for the authenticated local controller transport."""

from __future__ import annotations

import os
from pathlib import Path
from types import TracebackType
from typing import TYPE_CHECKING, TypeVar
from urllib.parse import quote

import httpx
from pydantic import BaseModel, TypeAdapter, ValidationError

from toolkit.controller.contracts import (
    ApprovalGrant,
    ControllerHealth,
    DestroyPlanRequest,
    ErrorCode,
    ErrorEnvelope,
    JobEvent,
    JobRecord,
    JobRequest,
    PlanRecord,
)
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
    GrafanaWebhookReceipt,
    InviteActivationRequest,
    InviteActivationResult,
    InvitePreview,
    InvitePreviewRequest,
    JobsView,
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
    SecretInventory,
    SecretMutationResult,
    SecretUpdateRequest,
    ServiceManagementView,
    ServiceSettingsUpdate,
    ServicesView,
    ServiceTopology,
    SettingsUpdate,
    SettingsView,
)
from toolkit.controller.transport_auth import read_transport_token

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

ModelT = TypeVar("ModelT", bound=BaseModel)


class ControllerClientError(RuntimeError):
    pass


class ControllerUnavailableError(ControllerClientError):
    def __init__(self):
        super().__init__("The homelab controller is unavailable")


class ControllerProtocolError(ControllerClientError):
    def __init__(self):
        super().__init__("The homelab controller returned an invalid response")


class ControllerRejectedError(ControllerClientError):
    def __init__(
        self,
        code: ErrorCode,
        message: str,
        details: dict,
        correlation_id: str | None,
        status_code: int | None = None,
    ):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details
        self.correlation_id = correlation_id
        self.status_code = status_code


class ControllerClient:
    def __init__(self, client: httpx.Client):
        self._client = client

    @classmethod
    def for_uds(
        cls,
        socket_path: Path,
        *,
        role: str,
        token: str,
        timeout: float = 10.0,
    ) -> ControllerClient:
        if role not in {"local", "ui"}:
            raise ValueError("controller UDS role must be local or ui")
        if len(token) < 32:
            raise ValueError("controller UDS token is invalid")
        transport = httpx.HTTPTransport(uds=str(socket_path))
        return cls(
            httpx.Client(
                base_url="http://controller",
                transport=transport,
                headers={"X-Controller-Transport": role, "X-Controller-Token": token},
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            )
        )

    @classmethod
    def for_managed_ssh(
        cls,
        cfg: Config,
        root: Path,
        *,
        timeout: int = 30,
    ) -> ControllerClient:
        """Reach the private controller UDS through verified managed-node SSH."""
        from toolkit.controller.ssh_transport import SSHControllerTransport

        return cls._from_transport(
            SSHControllerTransport(cfg, root, timeout=timeout),
            base_url="http://controller",
            timeout=float(timeout),
        )

    @classmethod
    def _from_transport(
        cls,
        transport: httpx.BaseTransport,
        *,
        base_url: str,
        timeout: float = 10.0,
    ) -> ControllerClient:
        return cls(
            httpx.Client(
                base_url=base_url,
                transport=transport,
                timeout=timeout,
                follow_redirects=False,
                trust_env=False,
            )
        )

    def close(self) -> None:
        self._client.close()

    def __enter__(self) -> ControllerClient:
        return self

    def __exit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        self.close()

    def health(self) -> ControllerHealth:
        return self._model_request("GET", "/v1/health", ControllerHealth)

    def issue_bootstrap_capability(self) -> BootstrapCapabilityIssue:
        return self._model_request("POST", "/v1/bootstrap/capabilities", BootstrapCapabilityIssue)

    def exchange_bootstrap_capability(self, token: str) -> BootstrapSessionGrant:
        exchange = BootstrapSessionExchange(capability_token=token)
        return self._model_request(
            "POST",
            "/v1/bootstrap/sessions",
            BootstrapSessionGrant,
            json=exchange.model_dump(mode="json"),
        )

    def bootstrap_status(self) -> BootstrapStatus:
        return self._model_request("GET", "/v1/bootstrap/status", BootstrapStatus)

    def bootstrap_view(self, session_token: str) -> BootstrapView:
        return self._model_request(
            "GET",
            "/v1/bootstrap",
            BootstrapView,
            headers={"X-Bootstrap-Session": session_token},
        )

    def initialize_bootstrap(self, request: BootstrapInitializeRequest) -> BootstrapInitializeResult:
        return self._model_request(
            "POST",
            "/v1/bootstrap/initializations",
            BootstrapInitializeResult,
            json=request.model_dump(mode="json"),
            timeout=60.0,
        )

    def secret_inventory(self) -> SecretInventory:
        return self._model_request("GET", "/v1/settings/secrets", SecretInventory)

    def update_secrets(self, update: SecretUpdateRequest) -> SecretMutationResult:
        return self._model_request(
            "PUT",
            "/v1/settings/secrets",
            SecretMutationResult,
            json=update.model_dump(mode="json"),
        )

    def generate_secrets(self) -> SecretMutationResult:
        return self._model_request("POST", "/v1/settings/secrets/generation", SecretMutationResult)

    def services_view(self, *, family: bool = False, groups: list[str] | None = None) -> ServicesView:
        return self._model_request(
            "GET",
            "/v1/services",
            ServicesView,
            params={"family": str(family).lower(), "group": groups or []},
        )

    def account_view(self, *, groups: list[str] | None = None) -> AccountView:
        return self._model_request(
            "GET",
            "/v1/identity/account",
            AccountView,
            params={"group": groups or []},
        )

    def directory_users(self) -> DirectoryUsersView:
        return self._model_request("GET", "/v1/identity/users", DirectoryUsersView)

    def invite_preview(self, token: str) -> InvitePreview:
        request = InvitePreviewRequest(token=token)
        return self._model_request(
            "POST",
            "/v1/identity/invite-preview",
            InvitePreview,
            json=request.model_dump(mode="json"),
        )

    def activate_invite(self, request: InviteActivationRequest) -> InviteActivationResult:
        return self._model_request(
            "POST",
            "/v1/identity/invite-activation",
            InviteActivationResult,
            json=request.model_dump(mode="json"),
        )

    def accept_grafana_alert(
        self,
        raw_body: bytes,
        *,
        signature: str,
        timestamp: str,
        content_type: str,
    ) -> GrafanaWebhookReceipt:
        return self._model_request(
            "POST",
            "/v1/integrations/grafana/alerts",
            GrafanaWebhookReceipt,
            content=raw_body,
            headers={
                "Content-Type": content_type,
                "X-Grafana-Alerting-Signature": signature,
                "X-Grafana-Alerting-Signature-Timestamp": timestamp,
            },
        )

    def dashboard_view(self, *, family: bool = False, groups: list[str] | None = None) -> DashboardView:
        return self._model_request(
            "GET",
            "/v1/dashboard",
            DashboardView,
            params={"family": str(family).lower(), "group": groups or []},
        )

    def dashboard_metrics(self) -> DashboardMetrics:
        return self._model_request("GET", "/v1/dashboard/metrics", DashboardMetrics)

    def portal_status(self) -> PortalStatus:
        return self._model_request("GET", "/v1/dashboard/portal-status", PortalStatus)

    def deployment_view(self) -> DeploymentView:
        return self._model_request("GET", "/v1/deployment", DeploymentView)

    def operations_view(self) -> OperationsView:
        return self._model_request("GET", "/v1/operations", OperationsView)

    def managed_hosts(self) -> ManagedHostsView:
        return self._model_request("GET", "/v1/hosts", ManagedHostsView)

    def create_managed_host(self, request: ManagedHostCreate) -> ManagedHostsView:
        return self._model_request(
            "POST",
            "/v1/hosts",
            ManagedHostsView,
            json=request.model_dump(mode="json"),
        )

    def update_managed_host(self, name: str, request: ManagedHostUpdate) -> ManagedHostsView:
        return self._model_request(
            "PUT",
            f"/v1/hosts/{quote(name, safe='')}",
            ManagedHostsView,
            json=request.model_dump(mode="json"),
        )

    def dns_view(self) -> DnsView:
        return self._model_request("GET", "/v1/dns", DnsView)

    def update_dns_public_ip(self, update: DnsIpUpdate) -> DnsView:
        return self._model_request(
            "PUT",
            "/v1/dns/public-ip",
            DnsView,
            json=update.model_dump(mode="json"),
        )

    def settings_view(self) -> SettingsView:
        return self._model_request("GET", "/v1/settings", SettingsView)

    def update_settings(self, update: SettingsUpdate) -> SettingsView:
        return self._model_request(
            "PUT",
            "/v1/settings",
            SettingsView,
            json=update.model_dump(mode="json"),
        )

    def machines_view(self) -> MachinesView:
        return self._model_request("GET", "/v1/machines", MachinesView)

    def create_machine(self, request: MachineCreate) -> MachinesView:
        return self._model_request(
            "POST",
            "/v1/machines",
            MachinesView,
            json=request.model_dump(mode="json"),
        )

    def update_machine(self, machine_id: str, request: MachineUpdate) -> MachinesView:
        return self._model_request(
            "PUT",
            f"/v1/machines/{quote(machine_id, safe='')}",
            MachinesView,
            json=request.model_dump(mode="json"),
        )

    def remove_machine(self, request: MachineRemove) -> MachinesView:
        return self._model_request(
            "DELETE",
            f"/v1/machines/{quote(request.machine_id, safe='')}",
            MachinesView,
            json=request.model_dump(mode="json"),
        )

    def projects_view(self) -> ProjectsView:
        return self._model_request("GET", "/v1/projects", ProjectsView)

    def create_project(self, request: ProjectCreate) -> ProjectsView:
        return self._model_request(
            "POST",
            "/v1/projects",
            ProjectsView,
            json=request.model_dump(mode="json"),
        )

    def remove_project(self, request: ProjectRemove) -> ProjectsView:
        return self._model_request(
            "DELETE",
            f"/v1/projects/{request.subdomain}",
            ProjectsView,
            json=request.model_dump(mode="json"),
        )

    def container_inventory(self) -> ContainerInventory:
        return self._model_request("GET", "/v1/containers", ContainerInventory)

    def service_topology(self) -> ServiceTopology:
        return self._model_request("GET", "/v1/services/topology", ServiceTopology)

    def service_management(self, service: str, *, collect_status: bool = True) -> ServiceManagementView:
        return self._model_request(
            "GET",
            f"/v1/services/{quote(service, safe='')}/management",
            ServiceManagementView,
            params={"collect_status": str(collect_status).lower()},
        )

    def update_service_settings(
        self,
        service: str,
        update: ServiceSettingsUpdate,
    ) -> ServiceManagementView:
        return self._model_request(
            "PATCH",
            f"/v1/services/{quote(service, safe='')}/settings",
            ServiceManagementView,
            json=update.model_dump(mode="json"),
        )

    def create_destruction_plan(self, request: DestroyPlanRequest) -> PlanRecord:
        return self._model_request(
            "POST",
            "/v1/plans/destruction",
            PlanRecord,
            json=request.model_dump(mode="json"),
        )

    def get_plan(self, plan_id: str) -> PlanRecord:
        return self._model_request("GET", f"/v1/plans/{plan_id}", PlanRecord)

    def approve_plan(
        self,
        plan_id: str,
        *,
        plan_hash: str,
        confirmation: str,
        ttl_seconds: int = 300,
    ) -> ApprovalGrant:
        return self._model_request(
            "POST",
            f"/v1/plans/{plan_id}/approval",
            ApprovalGrant,
            json={
                "ttl_seconds": ttl_seconds,
                "plan_hash": plan_hash,
                "confirmation": confirmation,
            },
        )

    def submit(self, request: JobRequest) -> JobRecord:
        return self._model_request(
            "POST",
            "/v1/jobs",
            JobRecord,
            json=request.model_dump(mode="json"),
        )

    def jobs(self, *, limit: int = 100) -> JobsView:
        return self._model_request("GET", "/v1/jobs", JobsView, params={"limit": limit})

    def get_job(self, job_id: str) -> JobRecord:
        return self._model_request("GET", f"/v1/jobs/{job_id}", JobRecord)

    def cancel(self, job_id: str) -> JobRecord:
        return self._model_request("POST", f"/v1/jobs/{job_id}/cancellation", JobRecord)

    def events(self, job_id: str, *, after: int = 0, limit: int = 200) -> list[JobEvent]:
        response = self._request("GET", f"/v1/jobs/{job_id}/events", params={"after": after, "limit": limit})
        try:
            return TypeAdapter(list[JobEvent]).validate_python(response.json())
        except (ValueError, ValidationError) as exc:
            raise ControllerProtocolError from exc

    def _model_request(self, method: str, path: str, model: type[ModelT], **kwargs) -> ModelT:
        response = self._request(method, path, **kwargs)
        try:
            return model.model_validate(response.json())
        except (ValueError, ValidationError) as exc:
            raise ControllerProtocolError from exc

    def _request(self, method: str, path: str, **kwargs) -> httpx.Response:
        try:
            response = self._client.request(method, path, **kwargs)
        except httpx.TransportError as exc:
            raise ControllerUnavailableError from exc
        if response.is_error:
            try:
                envelope = ErrorEnvelope.model_validate(response.json())
            except (ValueError, ValidationError) as exc:
                raise ControllerProtocolError from exc
            raise ControllerRejectedError(
                envelope.error.code,
                envelope.error.message,
                envelope.error.details,
                response.headers.get("x-correlation-id"),
                response.status_code,
            )
        return response


def controller_client_from_environment() -> ControllerClient:
    url = os.environ.get("HOMELAB_CONTROLLER_URL", "").strip()
    if url:
        raise ValueError("remote controller transport is not implemented; use the authenticated Unix socket")
    socket_path = Path(os.environ.get("HOMELAB_CONTROLLER_SOCKET", "/run/homelab-controller/controller.sock"))
    role = os.environ.get("HOMELAB_CONTROLLER_ROLE", "local").strip().lower()
    if role not in {"local", "ui"}:
        raise ValueError("controller UDS role must be local or ui")
    timeout_default = 30.0 if role == "ui" else 10.0
    try:
        timeout = float(os.environ.get("HOMELAB_CONTROLLER_TIMEOUT_SECONDS", timeout_default))
    except ValueError as exc:
        raise ValueError("controller timeout must be numeric") from exc
    if not 1 <= timeout <= 120:
        raise ValueError("controller timeout must be between 1 and 120 seconds")
    default_token_path = (
        "/run/homelab-controller/ui.token" if role == "ui" else "/var/lib/homelab-controller/local.token"
    )
    token_path = Path(os.environ.get("HOMELAB_CONTROLLER_TOKEN_FILE", default_token_path))
    return ControllerClient.for_uds(
        socket_path,
        role=role,
        token=read_transport_token(token_path, allow_group_read=role == "ui"),
        timeout=timeout,
    )
