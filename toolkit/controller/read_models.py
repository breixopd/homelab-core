"""Typed controller read models that never expose secret values."""

from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal

from pydantic import ConfigDict, Field, StringConstraints, field_validator

from toolkit.controller.contracts import (
    DirectoryUserId,
    JobKind,
    JobState,
    MachineId,
    ServiceGroupName,
    ServiceName,
    StrictModel,
)
from toolkit.core.machines import MachineSpec

SecretName = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,127}$")]
SecretValue = Annotated[str, StringConstraints(min_length=1, max_length=65_536)]
SecretTierName = Literal["user", "gen", "derived"]
SecretRotationPolicy = Literal["restart", "reconcile", "persistent"]
SecretStorageMode = Literal["encrypted", "plaintext", "missing"]
ContainerHealth = Literal["healthy", "unhealthy", "starting", "none"]
BootstrapPhase = Literal["uninitialized", "recovery_required", "ready"]
BootstrapServiceSettingKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
BootstrapServiceSettingText = Annotated[str, StringConstraints(max_length=4_096)]
BootstrapServiceSettingScalar = bool | int | float | BootstrapServiceSettingText


class BootstrapStatus(StrictModel):
    phase: BootstrapPhase
    has_active_capability: bool = False
    has_active_session: bool = False


class BootstrapService(StrictModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    label: str = Field(min_length=1, max_length=100)


class BootstrapCategory(StrictModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=500)
    node: MachineId
    service_count: int = Field(ge=0, le=512)
    services: list[BootstrapService] = Field(max_length=512)


class BootstrapServiceSettingView(StrictModel):
    service: ServiceName
    service_label: str = Field(min_length=1, max_length=100)
    key: BootstrapServiceSettingKey
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=500)
    type: Literal["boolean", "number", "text", "select"]
    default: BootstrapServiceSettingScalar
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    choices: list[str] = Field(default_factory=list, max_length=32)


class BootstrapServiceSecretConditionView(StrictModel):
    setting: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}\.[a-z][a-z0-9-]{0,62}$")
    values: list[bool | int | float | str] = Field(min_length=1, max_length=32)


class BootstrapServiceSecretView(StrictModel):
    service: ServiceName
    name: SecretName
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=500)
    input: Literal["password", "text"]
    required: bool
    conditions: list[BootstrapServiceSecretConditionView] = Field(default_factory=list, max_length=8)


class BootstrapDesiredState(StrictModel):
    domain: str = Field(min_length=1, max_length=253)
    email: str = Field(min_length=3, max_length=254)
    timezone: str = Field(min_length=1, max_length=100)
    proxmox_api_url: str = Field(max_length=2_048, pattern=r"^[^\x00-\x1f\x7f]*$")
    proxmox_node: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._-]+$")
    proxmox_storage: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9._:-]+$")
    service_settings: dict[
        ServiceName,
        dict[BootstrapServiceSettingKey, BootstrapServiceSettingScalar],
    ] = Field(default_factory=dict, max_length=512)


class BootstrapView(StrictModel):
    status: BootstrapStatus
    categories: list[BootstrapCategory] = Field(max_length=2562)
    service_settings: list[BootstrapServiceSettingView] = Field(default_factory=list, max_length=2562)
    service_secrets: list[BootstrapServiceSecretView] = Field(default_factory=list, max_length=2562)


class BootstrapCapabilityIssue(StrictModel):
    token: str = Field(min_length=32, max_length=512, repr=False)
    expires_at: datetime


class BootstrapSessionExchange(StrictModel):
    capability_token: str = Field(min_length=32, max_length=512, repr=False)


class BootstrapSessionGrant(StrictModel):
    session_token: str = Field(min_length=32, max_length=512, repr=False)
    expires_at: datetime


class BootstrapInitializeRequest(StrictModel):
    session_token: str = Field(min_length=32, max_length=512, repr=False)
    desired_state: BootstrapDesiredState
    credential_values: dict[SecretName, SecretValue] = Field(default_factory=dict, max_length=64, repr=False)


class BootstrapInitializeResult(StrictModel):
    outcome: Literal["initialized"] = "initialized"
    phase: Literal["ready"] = "ready"
    config_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    configured_secret_names: list[SecretName] = Field(max_length=128)


class SecretStatus(StrictModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: SecretName
    is_configured: bool = Field(alias="isConfigured")
    tier: SecretTierName
    rotation_policy: SecretRotationPolicy = Field(default="persistent", alias="rotationPolicy")
    description: str = Field(max_length=500)


class SecretInventory(StrictModel):
    owner_email: str = Field(max_length=254)
    storage_mode: SecretStorageMode
    encryption_available: bool
    entries: list[SecretStatus] = Field(max_length=256)


class SecretUpdateRequest(StrictModel):
    values: dict[SecretName, SecretValue] = Field(min_length=1, max_length=128)


class SecretMutationResult(StrictModel):
    changed_names: list[SecretName] = Field(max_length=128)
    inventory: SecretInventory


class BookmarkItem(StrictModel):
    title: str = Field(min_length=1, max_length=100)
    href: str = Field(min_length=1, max_length=2_048)
    description: str = Field(max_length=500)


class BookmarkGroup(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    items: list[BookmarkItem] = Field(max_length=64)


class FamilyServiceCard(StrictModel):
    label: str = Field(min_length=1, max_length=100)
    url: str = Field(min_length=1, max_length=2_048)
    blurb: str = Field(max_length=500)
    sign_in: str = Field(max_length=500)


class FamilyServiceSection(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    cards: list[FamilyServiceCard] = Field(max_length=64)


class ServiceRouteSummary(StrictModel):
    url: str = Field(min_length=1, max_length=2_048)
    exposure: Literal["public", "private"]
    auth_mode: Literal["forward_auth", "oidc", "native", "split"]
    scope: str = Field(min_length=1, max_length=500)


class ServiceSummary(StrictModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=500)
    routes: list[ServiceRouteSummary] = Field(max_length=128)
    node: MachineId
    is_manageable: bool = False


class ServiceCategorySummary(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    node: MachineId
    services: list[ServiceSummary] = Field(max_length=128)


class ServicesView(StrictModel):
    domain: str = Field(min_length=1, max_length=253)
    categories: list[ServiceCategorySummary] = Field(max_length=2562)
    bookmark_groups: list[BookmarkGroup] = Field(max_length=2562)
    family_sections: list[FamilyServiceSection] = Field(max_length=16)
    tier_labels: list[str] = Field(max_length=16)


class AccountView(StrictModel):
    domain: str = Field(min_length=1, max_length=253)
    auth_url: str = Field(min_length=1, max_length=2_048)
    sections: list[FamilyServiceSection] = Field(max_length=16)
    tier_labels: list[str] = Field(max_length=16)


class DirectoryGroupView(StrictModel):
    name: ServiceGroupName
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=500)
    is_default: bool


class DirectoryUserView(StrictModel):
    id: DirectoryUserId
    email: str = Field(max_length=254, pattern=r"^[^\x00-\x1f\x7f]*$")
    display_name: str = Field(max_length=128, pattern=r"^[^\x00-\x1f\x7f]*$")
    groups: list[ServiceGroupName] = Field(max_length=256)
    is_protected: bool

    @field_validator("groups")
    @classmethod
    def unique_groups(cls, values: list[ServiceGroupName]) -> list[ServiceGroupName]:
        if len(values) != len(set(values)):
            raise ValueError("directory groups must be unique")
        return values


class DirectoryUsersView(StrictModel):
    domain: str = Field(min_length=1, max_length=253)
    users: list[DirectoryUserView] = Field(max_length=10_000)
    group_options: list[DirectoryGroupView] = Field(min_length=3, max_length=256)
    invites_enabled: bool
    invite_disabled_reason: str = Field(default="", max_length=500)


class InvitePreviewRequest(StrictModel):
    token: str = Field(default="", max_length=4_096, repr=False)


class InvitePreview(StrictModel):
    valid: bool
    domain: str = Field(min_length=1, max_length=253)
    secure_cookie: bool
    cookie_max_age_seconds: int = Field(ge=60, le=7 * 24 * 3600)
    activation_csrf: str = Field(default="", max_length=128)
    display_name: str = Field(default="", max_length=128)
    email: str = Field(default="", max_length=254)
    sections: list[FamilyServiceSection] = Field(max_length=16)


class InviteActivationRequest(StrictModel):
    token: str = Field(min_length=1, max_length=4_096, repr=False)
    activation_csrf: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    origin: str = Field(min_length=1, max_length=2_048, pattern=r"^[^\x00-\x1f\x7f]+$")
    password: str = Field(min_length=10, max_length=128, repr=False)


class InviteActivationResult(StrictModel):
    outcome: Literal["activated", "invalid", "failed"]
    secure_cookie: bool


class WebhookJobReceipt(StrictModel):
    service: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    job_id: str = Field(min_length=1, max_length=128)
    replayed: bool


class GrafanaWebhookReceipt(StrictModel):
    outcome: Literal["queued", "ignored"]
    reason: Literal["no_firing_services"] | None = None
    jobs: list[WebhookJobReceipt] = Field(max_length=8)


class ContainerStatus(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
    node: MachineId
    status: str = Field(max_length=500)
    state: str = Field(max_length=50)
    image: str = Field(max_length=500)
    health: ContainerHealth
    completed: bool = False


class ContainerInventory(StrictModel):
    is_available: bool
    unavailable_nodes: list[MachineId] = Field(max_length=256)
    containers: list[ContainerStatus] = Field(max_length=512)


class ServiceGraphNode(StrictModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    health: Literal["healthy", "warning", "critical"]
    image: str = Field(max_length=500)
    node: str = Field(max_length=50)
    tier: str = Field(max_length=50)
    category: str = Field(max_length=100)
    icon: str = Field(max_length=50)


class ServiceGraphEdge(StrictModel):
    source: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    target: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")


class ServiceCatalogEntry(StrictModel):
    name: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=500)
    node: str = Field(max_length=50)
    tier: str = Field(max_length=50)
    category: str = Field(max_length=100)
    icon: str = Field(max_length=50)
    restart_policy: str = Field(max_length=50)
    image: str = Field(max_length=500)
    state: str = Field(max_length=50)
    health: str = Field(max_length=50)


class ServiceTopology(StrictModel):
    nodes: list[ServiceGraphNode] = Field(max_length=512)
    edges: list[ServiceGraphEdge] = Field(max_length=2_048)
    catalog: list[ServiceCatalogEntry] = Field(max_length=512)


class ManagedServiceSettingView(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=500)
    type: Literal["boolean", "number", "text", "select"]
    value: bool | int | float | str
    default: bool | int | float | str
    minimum: float | None = None
    maximum: float | None = None
    step: float | None = None
    choices: list[str] = Field(max_length=2562)
    requires_redeploy: bool


class ManagedServiceActionView(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=500)
    confirmation: str = Field(max_length=200)
    is_dangerous: bool
    can_run: bool


class ManagedServiceMetricView(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    label: str = Field(min_length=1, max_length=100)
    unit: Literal["none", "count", "percent", "bytes", "megabytes", "seconds", "mbps"]
    precision: int = Field(ge=0, le=4)
    value: int | float | None


class MetricPoint(StrictModel):
    timestamp_ms: int = Field(ge=0)
    value: float


class ManagedServiceMetricSeriesView(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    label: str = Field(min_length=1, max_length=100)
    unit: Literal["percent", "megabytes"]
    average: float | None = Field(default=None, ge=0)
    peak: float | None = Field(default=None, ge=0)
    points: list[tuple[int, float]] = Field(default_factory=list, max_length=120)


class ManagedServiceResourceColumnView(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    label: str = Field(min_length=1, max_length=100)


ServiceResourceValue = Annotated[
    str,
    StringConstraints(max_length=200, pattern=r"^[^\x00-\x1f\x7f]*$"),
]


class ManagedServiceResourceView(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{0,62}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=500)
    available: bool
    columns: list[ManagedServiceResourceColumnView] = Field(min_length=1, max_length=12)
    rows: list[dict[str, ServiceResourceValue]] = Field(max_length=100)


class ManagedServiceInfoItemView(StrictModel):
    label: str = Field(min_length=1, max_length=100)
    value: str = Field(min_length=1, max_length=500)
    copyable: bool
    href: str = Field(default="", max_length=2_048)


class ManagedServiceInfoPanelView(StrictModel):
    id: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    title: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=500)
    items: list[ManagedServiceInfoItemView] = Field(min_length=1, max_length=16)


class ManagedServiceSecretView(StrictModel):
    name: SecretName
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=500)
    is_configured: bool


class ServiceManagementView(StrictModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    service: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=500)
    category: str = Field(min_length=1, max_length=100)
    node: MachineId
    enabled: bool
    status_available: bool
    panels: list[ManagedServiceInfoPanelView] = Field(default_factory=list, max_length=8)
    secrets: list[ManagedServiceSecretView] = Field(default_factory=list, max_length=32)
    settings: list[ManagedServiceSettingView] = Field(max_length=256)
    actions: list[ManagedServiceActionView] = Field(max_length=16)
    metrics: list[ManagedServiceMetricView] = Field(max_length=256)
    metric_series: list[ManagedServiceMetricSeriesView] = Field(default_factory=list, max_length=8)
    resources: list[ManagedServiceResourceView] = Field(default_factory=list, max_length=8)


ServiceSettingKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}$")]
ServiceSettingText = Annotated[str, StringConstraints(max_length=4_096)]


class ServiceSettingsUpdate(StrictModel):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    values: dict[ServiceSettingKey, bool | int | float | ServiceSettingText] = Field(min_length=1, max_length=256)


class DashboardMetrics(StrictModel):
    cpu: float | None = Field(default=None, ge=0, le=100)
    memory: float | None = Field(default=None, ge=0, le=100)
    disk: float | None = Field(default=None, ge=0, le=100)
    containers: int | None = Field(default=None, ge=0)
    targets_up: int | None = Field(default=None, ge=0)
    targets_down: int | None = Field(default=None, ge=0)
    cpu_history: list[MetricPoint] = Field(default_factory=list, max_length=1_440)
    memory_history: list[MetricPoint] = Field(default_factory=list, max_length=1_440)
    disk_history: list[MetricPoint] = Field(default_factory=list, max_length=1_440)


class DashboardAlert(StrictModel):
    severity: Literal["info", "warning", "critical"]
    message: str = Field(min_length=1, max_length=500)
    href: str = Field(default="", max_length=2_048, pattern=r"^(?:$|/[A-Za-z0-9_./-]*)$")


class DashboardRuntimeSummary(StrictModel):
    total: int = Field(default=0, ge=0, le=512)
    running: int = Field(default=0, ge=0, le=512)
    healthy: int = Field(default=0, ge=0, le=512)
    unhealthy: int = Field(default=0, ge=0, le=512)
    exited: int = Field(default=0, ge=0, le=512)
    unavailable_nodes: int = Field(default=0, ge=0, le=256)


class PortalStatus(StrictModel):
    checked_at: datetime
    complete: bool
    unavailable_nodes: int = Field(ge=0, le=256)
    services: dict[ServiceName, Literal["online", "degraded", "offline", "unknown"]] = Field(
        default_factory=dict,
        max_length=256,
    )


class DashboardOperationsSummary(StrictModel):
    maintenance_enabled: bool = False
    maintenance_ok: bool | None = None
    maintenance_last_run_at: datetime | None = None
    backups_enabled: bool = False
    backup_target: Literal["local", "remote"] = "local"
    backup_drill_ok: bool | None = None
    backup_drill_last_run_at: datetime | None = None
    restore_points: int = Field(default=0, ge=0, le=7)
    managed_hosts: int = Field(default=0, ge=0, le=128)
    pending_hosts: int = Field(default=0, ge=0, le=128)
    updates_available: bool = False


class DashboardJobView(StrictModel):
    job_id: str = Field(min_length=1, max_length=128)
    kind: JobKind
    state: JobState
    created_at: datetime
    updated_at: datetime
    can_cancel: bool
    error_code: str = Field(default="", max_length=64)


class VerifyNodeSummary(StrictModel):
    ok: bool
    healthy: int = Field(ge=0)
    unhealthy: int = Field(ge=0)
    pending: int = Field(ge=0)


class DashboardAction(StrictModel):
    title: str = Field(min_length=1, max_length=200)
    category: str = Field(min_length=1, max_length=50)
    service: str = Field(min_length=1, max_length=100)


class DashboardCategory(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    node: MachineId
    services: list[str] = Field(max_length=128)


class DashboardView(StrictModel):
    state: Literal["uninitialized", "config_only", "ready"]
    domain: str = Field(default="", max_length=253)
    enabled_nodes: list[MachineId] = Field(max_length=256)
    categories: list[DashboardCategory] = Field(max_length=2562)
    total_services: int = Field(default=0, ge=0)
    metrics: DashboardMetrics
    alerts: list[DashboardAlert] = Field(max_length=512)
    last_verify: dict[str, VerifyNodeSummary] | None = None
    next_action: DashboardAction | None = None
    bookmark_groups: list[BookmarkGroup] = Field(max_length=2562)
    tier_labels: list[str] = Field(max_length=16)
    health: Literal["healthy", "attention", "critical", "unknown", "setup"] = "unknown"
    runtime: DashboardRuntimeSummary = Field(default_factory=DashboardRuntimeSummary)
    operations: DashboardOperationsSummary = Field(default_factory=DashboardOperationsSummary)
    recent_jobs: list[DashboardJobView] = Field(default_factory=list, max_length=10)
    active_jobs: int = Field(default=0, ge=0, le=10_000)
    attention_jobs: int = Field(default=0, ge=0, le=10_000)
    metrics_service_href: str = Field(default="/services", pattern=r"^/services(?:/[a-z0-9-]+)?$")
    metrics_dashboard_href: str = Field(default="/services", pattern=r"^/services(?:/[a-z0-9-]+)?$")


class DeploymentPreflightCheck(StrictModel):
    check_id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    label: str = Field(min_length=1, max_length=200)
    ok: bool
    detail: str = Field(default="", max_length=500)


class DeploymentJobSummary(StrictModel):
    job_id: str = Field(min_length=1, max_length=128)
    kind: Literal["DEPLOY", "RECOVER", "GENERATE", "VERIFY"]
    state: Literal["QUEUED", "RUNNING", "CANCEL_REQUESTED"]
    created_at: datetime
    manageable: bool


class DeploymentView(StrictModel):
    state: Literal["uninitialized", "config_only", "ready"]
    enabled_targets: list[MachineId] = Field(max_length=256)
    node_count: int = Field(ge=0, le=256)
    total_services: int = Field(ge=0, le=1_024)
    category_count: int = Field(ge=0, le=2562)
    generated_config_count: int = Field(ge=0, le=256)
    step_labels: dict[str, str] = Field(max_length=64)
    preflight: list[DeploymentPreflightCheck] = Field(max_length=128)
    preflight_ok: bool
    last_verify: dict[str, VerifyNodeSummary] | None = None
    active_jobs: list[DeploymentJobSummary] = Field(max_length=10)


class JobSummaryView(StrictModel):
    job_id: str = Field(min_length=1, max_length=128)
    kind: JobKind
    state: JobState
    created_at: datetime
    updated_at: datetime
    can_cancel: bool
    error_code: str = Field(default="", max_length=64)


class JobsView(StrictModel):
    jobs: list[JobSummaryView] = Field(max_length=200)
    queued: int = Field(ge=0, le=200)
    running: int = Field(ge=0, le=200)
    attention: int = Field(ge=0, le=200)
    succeeded: int = Field(ge=0, le=200)


class MaintenanceOperationsView(StrictModel):
    enabled: bool = True
    daily_at: str = Field(pattern=r"^(?:[01][0-9]|2[0-3]):[0-5][0-9]$")
    schedule_label: str = Field(default="", max_length=100)
    last_run_at: datetime | None = None
    ok: bool | None = None
    action_count: int = Field(default=0, ge=0, le=10_000)
    error_count: int = Field(default=0, ge=0, le=10_000)


class BackupNodeView(StrictModel):
    role: MachineId
    status: Literal["fresh", "stale", "missing", "error"]
    ok: bool
    snapshot_count: int = Field(ge=0, le=1_000)
    last_snapshot_at: datetime | None = None
    age_hours: float | None = Field(default=None, ge=0)
    size_bytes: int = Field(default=0, ge=0)


class BackupDrillView(StrictModel):
    last_run_at: datetime | None = None
    ok: bool | None = None
    node_count: int = Field(default=0, ge=0, le=256)
    artifact_count: int = Field(default=0, ge=0, le=100)
    error_count: int = Field(default=0, ge=0, le=100)


class BackupOperationsView(StrictModel):
    enabled: bool
    target: Literal["local", "remote"]
    storage_host: str = Field(max_length=63)
    ok: bool | None = None
    error: str = Field(default="", max_length=180)
    nodes: list[BackupNodeView] = Field(default_factory=list, max_length=256)
    drill: BackupDrillView = Field(default_factory=BackupDrillView)


class BackupDumpView(StrictModel):
    dump_id: str = Field(pattern=r"^dmp_[0-9a-f]{20}$")
    name: str = Field(min_length=1, max_length=128)
    size_bytes: int = Field(ge=0)
    size: str = Field(min_length=1, max_length=20)


class ManagedHostIntegrationFieldChoice(StrictModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9-]{0,62}$")
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(max_length=500)
    type: Literal["boolean", "integer", "number", "path", "text"]
    required: bool
    default: bool | int | float | str | None = None
    placeholder: str = Field(max_length=200)


class ManagedHostServiceChoice(StrictModel):
    name: ServiceName
    label: str = Field(min_length=1, max_length=100)
    default_for_plain: bool
    default_for_fleet: bool
    fleet_only: bool
    fields: list[ManagedHostIntegrationFieldChoice] = Field(default_factory=list, max_length=16)


class ManagedHostView(StrictModel):
    fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
    ip: str = Field(min_length=7, max_length=15)
    kind: Literal["plain", "fleet"]
    ssh_user: str = Field(min_length=1, max_length=100)
    ssh_port: int = Field(ge=1, le=65535)
    cluster_group: str = Field(max_length=63)
    lldap_email: str = Field(max_length=254)
    headscale_tags: list[str] = Field(max_length=16)
    services: list[ServiceName] = Field(max_length=16)
    applied_services: list[ServiceName] = Field(max_length=16)
    integrations: dict[str, dict[str, bool | int | float | str]] = Field(default_factory=dict, max_length=16)
    reconciled: bool
    last_reconcile_at: datetime | None = None


class ManagedHostsView(StrictModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    hosts: list[ManagedHostView] = Field(max_length=128)
    service_choices: list[ManagedHostServiceChoice] = Field(max_length=16)


class UpdateCandidateView(StrictModel):
    service: ServiceName
    current: str = Field(min_length=1, max_length=128)
    target: str = Field(min_length=1, max_length=128)
    changelog_url: str = Field(default="", max_length=2_048)


class UpdateOperationsView(StrictModel):
    available: bool
    reason: str = Field(max_length=25600)
    revision: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    checked_at: datetime | None = None
    candidates: list[UpdateCandidateView] = Field(default_factory=list, max_length=512)
    active_revision: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")
    rollback_available: bool = False
    recovery_required: bool = False


class OperationsView(StrictModel):
    maintenance: MaintenanceOperationsView
    backups: BackupOperationsView
    dumps: list[BackupDumpView] = Field(max_length=7)
    hosts: ManagedHostsView
    updates: UpdateOperationsView


class DnsRecordView(StrictModel):
    type: str = Field(min_length=1, max_length=16)
    name: str = Field(min_length=1, max_length=253)
    content: str = Field(min_length=1, max_length=2_048)
    is_proxied: bool


class DnsView(StrictModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    public_ip: str = Field(max_length=45)
    ip_source: Literal["config", "override", "autodetect", "proxmox-url", "missing"]
    records: list[DnsRecordView] = Field(max_length=512)
    has_cloudflare_credentials: bool


class DnsIpUpdate(StrictModel):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    public_ip: str = Field(min_length=1, max_length=45)


class SettingsValues(StrictModel):
    domain: str = Field(min_length=1, max_length=253)
    email: str = Field(max_length=254)
    timezone: str = Field(min_length=1, max_length=100)
    services: dict[str, bool] = Field(max_length=16)
    deploy_ntfy_url: str = Field(max_length=2_048)
    smtp_mode: Literal["auto", "external", "disabled"]
    smtp_host: str = Field(max_length=253)
    smtp_port: int = Field(ge=1, le=65_535)
    smtp_starttls: bool
    smtp_username: str = Field(max_length=320)
    smtp_password_secret: str = Field(pattern=r"^(?:[A-Z][A-Z0-9_]{0,127})?$")
    smtp_from_address: str = Field(max_length=254)
    ssh_auth: Literal["key", "password"]
    ssh_key_file: str = Field(max_length=1_024)
    proxmox_api_url: str = Field(max_length=2_048)
    proxmox_control_host: str = Field(max_length=253)
    proxmox_ssh_user: str = Field(pattern=r"^[a-z_][a-z0-9_-]{0,31}$")
    proxmox_ssh_port: int = Field(ge=1, le=65_535)
    proxmox_ssh_key_file: str = Field(max_length=4_096)
    proxmox_ssh_connect_timeout: int = Field(ge=1, le=300)
    proxmox_ssh_command_timeout: int = Field(ge=5, le=7_200)
    proxmox_ssh_retries: int = Field(ge=1, le=10)
    proxmox_node: str = Field(min_length=1, max_length=100)
    proxmox_storage: str = Field(min_length=1, max_length=100)
    proxmox_template_datastore: str = Field(min_length=1, max_length=100)
    proxmox_template_url: str = Field(min_length=1, max_length=2_048)
    proxmox_template_checksum: str = Field(pattern=r"^[a-f0-9]{64}$")
    proxmox_tls_ca_file: str = Field(max_length=4_096)
    proxmox_provision_machines: bool
    expose_internet: bool
    container_ipv4_cidr: str = Field(min_length=9, max_length=18)
    container_network_prefix: int = Field(ge=24, le=29)
    dns_provider: str = Field(min_length=1, max_length=100)
    dns_public_ip: str = Field(max_length=45)
    dns_proxy: bool


class SettingsView(StrictModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    values: SettingsValues
    service_toggles: list[str] = Field(max_length=16)


class SettingsUpdate(StrictModel):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    values: SettingsValues


class MachineView(StrictModel):
    machine_id: MachineId
    spec: MachineSpec
    services: list[ServiceName] = Field(default_factory=list, max_length=1_024)
    projects: list[str] = Field(default_factory=list, max_length=256)
    can_remove: bool
    removal_blockers: list[str] = Field(default_factory=list, max_length=256)
    can_retire: bool
    retirement_blockers: list[str] = Field(default_factory=list, max_length=256)


class MachineTemplateView(StrictModel):
    template_id: MachineId
    spec: MachineSpec


class MachinesView(StrictModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    machines: list[MachineView] = Field(max_length=256)
    templates: list[MachineTemplateView] = Field(max_length=256)


class MachineCreate(StrictModel):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    machine_id: MachineId
    spec: MachineSpec


class MachineUpdate(StrictModel):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    spec: MachineSpec


class MachineRemove(StrictModel):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    machine_id: MachineId
    confirmation: str = Field(min_length=1, max_length=128)


class ProjectDefinition(StrictModel):
    name: str = Field(min_length=1, max_length=100)
    subdomain: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,53}[a-z0-9])?$")
    auth_mode: Literal["forward_auth", "native"]
    exposure: Literal["public", "private"]
    description: str = Field(max_length=500)
    show_on_portal: bool
    docker_image: str = Field(
        min_length=1,
        max_length=500,
    )
    container_port: int = Field(ge=1, le=65535)
    placement: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    health_endpoint: str = Field(default="", max_length=2_048, pattern=r"^(?:$|/[^?#\\]*)$")
    read_only: bool = True
    database_service: str = Field(default="", pattern=r"^(?:[a-z0-9][a-z0-9-]{0,62})?$")

    @field_validator("docker_image")
    @classmethod
    def immutable_image(cls, value: str) -> str:
        from toolkit.core.images.references import parse_immutable_image_reference

        try:
            parse_immutable_image_reference(value)
        except ValueError as exc:
            raise ValueError("project image must include an immutable tag and sha256 digest") from exc
        return value


class ProjectView(ProjectDefinition):
    node: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    upstream: str = Field(min_length=1, max_length=25620, pattern=r"^[a-z0-9-]+:[0-9]{1,5}$")


class ProjectPlacementOption(StrictModel):
    selector: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    node: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    kind: Literal["capability", "machine"]


class ProjectDatabaseOption(StrictModel):
    service: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    label: str = Field(min_length=1, max_length=100)
    engine: Literal["postgresql"]
    node: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")


class ProjectsView(StrictModel):
    revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    domain: str = Field(min_length=1, max_length=253)
    available_placements: list[ProjectPlacementOption] = Field(min_length=1, max_length=512)
    available_databases: list[ProjectDatabaseOption] = Field(max_length=128)
    projects: list[ProjectView] = Field(max_length=128)


class ProjectCreate(StrictModel):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    project: ProjectDefinition


class ProjectRemove(StrictModel):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    subdomain: str = Field(pattern=r"^[a-z0-9](?:[a-z0-9-]{0,53}[a-z0-9])?$")


class ManagedHostSpec(StrictModel):
    name: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")
    ip: str = Field(min_length=7, max_length=15)
    kind: Literal["plain", "fleet"]
    ssh_user: str = Field(min_length=1, max_length=100, pattern=r"^[a-z_][a-z0-9_-]*$")
    ssh_port: int = Field(ge=1, le=65535)
    cluster_group: str = Field(default="", max_length=63, pattern=r"^[A-Za-z0-9_-]*$")
    lldap_email: str = Field(default="", max_length=254, pattern=r"^(?:$|[^@\s]+@[^@\s]+\.[^@\s]+)$")
    headscale_tags: list[Annotated[str, StringConstraints(pattern=r"^tag:[A-Za-z0-9_-]+$", max_length=100)]] = Field(
        default_factory=list, max_length=16
    )
    services: list[ServiceName] = Field(default_factory=list, max_length=16)
    integrations: dict[str, dict[str, bool | int | float | str]] = Field(default_factory=dict, max_length=16)

    @field_validator("ip")
    @classmethod
    def valid_ipv4(cls, value: str) -> str:
        from toolkit.core.config.validators import validate_ipv4

        return validate_ipv4(value)

    @field_validator("headscale_tags", "services")
    @classmethod
    def unique_values(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("managed host values must be unique")
        return values


class ManagedHostCreate(StrictModel):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    host: ManagedHostSpec


class ManagedHostUpdate(StrictModel):
    expected_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    host: ManagedHostSpec
