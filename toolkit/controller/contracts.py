"""Version-one controller request, state, event, and error contracts."""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Annotated, Any, Literal, Self, TypeVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator


class JobKind(StrEnum):
    GENERATE = "GENERATE"
    VERIFY = "VERIFY"
    SERVICE_VERIFY = "SERVICE_VERIFY"
    DEPLOY = "DEPLOY"
    RECOVER = "RECOVER"
    DESTROY_INFRA = "DESTROY_INFRA"
    DNS_SYNC = "DNS_SYNC"
    MAINTENANCE = "MAINTENANCE"
    BACKUP_DRILL = "BACKUP_DRILL"
    UPDATE = "UPDATE"
    RESTORE_DRILL = "RESTORE_DRILL"
    HOST_RECONCILE = "HOST_RECONCILE"
    HOST_REMOVE = "HOST_REMOVE"
    CONTAINER_ACTION = "CONTAINER_ACTION"
    SERVICE_ACTION = "SERVICE_ACTION"
    CONFIG_APPLY = "CONFIG_APPLY"
    SECRET_ROTATION = "SECRET_ROTATION"
    WEBHOOK_HEAL = "WEBHOOK_HEAL"
    IDENTITY = "IDENTITY"


class JobState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    SUCCEEDED = "SUCCEEDED"
    PARTIAL_FAILURE = "PARTIAL_FAILURE"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


_NON_CANCELLABLE_AFTER_START = frozenset(
    {
        JobKind.HOST_RECONCILE,
        JobKind.HOST_REMOVE,
        JobKind.IDENTITY,
        JobKind.MAINTENANCE,
        JobKind.BACKUP_DRILL,
        JobKind.RESTORE_DRILL,
        JobKind.UPDATE,
        JobKind.SERVICE_ACTION,
    }
)


def job_can_cancel(kind: JobKind, state: JobState) -> bool:
    """Return whether the controller will accept a cancellation request."""
    if state is JobState.QUEUED:
        return True
    return state is JobState.RUNNING and kind not in _NON_CANCELLABLE_AFTER_START


TERMINAL_JOB_STATES = frozenset({JobState.SUCCEEDED, JobState.PARTIAL_FAILURE, JobState.FAILED, JobState.CANCELLED})
ErrorCode = Literal[
    "CONFIGURATION_BUSY",
    "VALIDATION_ERROR",
    "NOT_FOUND",
    "CONFLICT",
    "FORBIDDEN",
    "CHECKPOINT_REQUIRED",
    "CONTROLLER_UNAVAILABLE",
    "OPERATION_REJECTED",
    "OPERATION_FAILED",
    "INTERNAL_ERROR",
]
EventLevel = Literal["DEBUG", "INFO", "WARNING", "ERROR"]
MachineId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
ServiceName = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")]
NodeName = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9_-]{0,62}$")]
DirectoryUserId = Annotated[str, StringConstraints(pattern=r"^[a-z0-9][a-z0-9._-]{0,31}$")]
ServiceGroupName = Annotated[str, StringConstraints(pattern=r"^homelab-[a-z0-9][a-z0-9-]{0,54}$")]
DestructionAction = Literal["destroy_all", "retire_machine"]
ItemT = TypeVar("ItemT")


def _require_unique(values: list[ItemT]) -> list[ItemT]:
    if len(values) != len(set(values)):
        raise ValueError("operation values must be unique")
    return values


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ErrorBody(StrictModel):
    code: ErrorCode
    message: str = Field(min_length=1, max_length=500)
    details: dict[str, Any] = Field(default_factory=dict)


class ErrorEnvelope(StrictModel):
    error: ErrorBody

    @classmethod
    def from_code(
        cls,
        code: ErrorCode,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> Self:
        return cls(error=ErrorBody(code=code, message=message, details=details or {}))


class GenerateOperation(StrictModel):
    kind: Literal[JobKind.GENERATE] = JobKind.GENERATE
    validate_output: bool = True


class VerifyOperation(StrictModel):
    kind: Literal[JobKind.VERIFY] = JobKind.VERIFY
    targets: list[MachineId] = Field(default_factory=list, max_length=256)

    @field_validator("targets")
    @classmethod
    def unique_targets(cls, values: list[MachineId]) -> list[MachineId]:
        return _require_unique(values)


class ServiceVerifyOperation(StrictModel):
    kind: Literal[JobKind.SERVICE_VERIFY] = JobKind.SERVICE_VERIFY
    service: ServiceName
    include_framework: bool = False


class DeployOperation(StrictModel):
    kind: Literal[JobKind.DEPLOY] = JobKind.DEPLOY
    target: MachineId | None = None
    skip_infrastructure: bool = False
    skip_dns: bool = False


class RecoverOperation(StrictModel):
    kind: Literal[JobKind.RECOVER] = JobKind.RECOVER
    target: MachineId | None = None


class DestroyInfraOperation(StrictModel):
    kind: Literal[JobKind.DESTROY_INFRA] = JobKind.DESTROY_INFRA
    action: DestructionAction
    scopes: list[MachineId] = Field(min_length=1, max_length=256)
    config_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_id: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9-]+$")
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    approval_token: str = Field(min_length=16, max_length=512)

    @field_validator("scopes")
    @classmethod
    def unique_scopes(cls, values: list[MachineId]) -> list[MachineId]:
        return _require_unique(values)


class DnsSyncOperation(StrictModel):
    kind: Literal[JobKind.DNS_SYNC] = JobKind.DNS_SYNC
    action: Literal["sync", "cleanup"] = "sync"
    dry_run: bool = False


class MaintenanceOperation(StrictModel):
    kind: Literal[JobKind.MAINTENANCE] = JobKind.MAINTENANCE


class BackupDrillOperation(StrictModel):
    kind: Literal[JobKind.BACKUP_DRILL] = JobKind.BACKUP_DRILL


class UpdateOperation(StrictModel):
    kind: Literal[JobKind.UPDATE] = JobKind.UPDATE
    action: Literal["refresh", "apply", "rollback", "recover"]
    services: list[ServiceName] = Field(default_factory=list, max_length=64)
    revision: str = Field(default="", pattern=r"^(?:|[0-9a-f]{64})$")

    @field_validator("services")
    @classmethod
    def unique_services(cls, values: list[ServiceName]) -> list[ServiceName]:
        return _require_unique(values)

    @model_validator(mode="after")
    def complete_update_request(self) -> UpdateOperation:
        if self.action == "apply" and (not self.services or not self.revision):
            raise ValueError("apply requires selected services and the update-plan revision")
        if self.action != "apply" and self.services:
            raise ValueError("only apply accepts selected services")
        if self.action == "refresh" and self.revision:
            raise ValueError("refresh does not accept a revision")
        if self.action == "rollback" and not self.revision:
            raise ValueError("rollback requires the active release revision")
        if self.action == "recover" and self.revision:
            raise ValueError("recover does not accept a revision")
        return self


class RestoreDrillOperation(StrictModel):
    kind: Literal[JobKind.RESTORE_DRILL] = JobKind.RESTORE_DRILL
    dump_id: str = Field(min_length=8, max_length=128, pattern=r"^[A-Za-z0-9._-]+$")


class HostReconcileOperation(StrictModel):
    kind: Literal[JobKind.HOST_RECONCILE] = JobKind.HOST_RECONCILE
    host_name: NodeName


class HostRemoveOperation(StrictModel):
    kind: Literal[JobKind.HOST_REMOVE] = JobKind.HOST_REMOVE
    host_name: NodeName
    expected_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    confirmation: NodeName

    @model_validator(mode="after")
    def confirmation_matches(self) -> HostRemoveOperation:
        if self.confirmation != self.host_name:
            raise ValueError("managed host removal confirmation does not match")
        return self


class ContainerActionOperation(StrictModel):
    kind: Literal[JobKind.CONTAINER_ACTION] = JobKind.CONTAINER_ACTION
    service: ServiceName
    action: Literal["start", "stop", "restart"]


class ServiceActionOperation(StrictModel):
    kind: Literal[JobKind.SERVICE_ACTION] = JobKind.SERVICE_ACTION
    service: ServiceName
    action: Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9-]{0,62}$")]


def _validate_service_groups(values: list[ServiceGroupName]) -> list[ServiceGroupName]:
    from toolkit.core.identity.service_groups import validate_service_groups

    _require_unique(values)
    return validate_service_groups(values)


class InviteUserCommand(StrictModel):
    action: Literal["invite"] = "invite"
    email: str = Field(min_length=3, max_length=254, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    display_name: str = Field(default="", max_length=128, pattern=r"^[^\x00-\x1f\x7f]*$")
    groups: list[ServiceGroupName] = Field(min_length=1, max_length=256)

    @field_validator("email", mode="before")
    @classmethod
    def normalize_email(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("email must be a string")
        normalized = value.strip().lower()
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise ValueError("email contains control characters")
        return normalized

    @field_validator("display_name", mode="before")
    @classmethod
    def normalize_display_name(cls, value: object) -> str:
        if not isinstance(value, str):
            raise ValueError("display name must be a string")
        return value.strip()

    @field_validator("groups")
    @classmethod
    def unique_groups(cls, values: list[ServiceGroupName]) -> list[ServiceGroupName]:
        return _validate_service_groups(values)


class SealedInviteUserCommand(StrictModel):
    action: Literal["invite_sealed"] = "invite_sealed"
    ciphertext: str = Field(min_length=64, max_length=4096, pattern=r"^[A-Za-z0-9_-]+={0,2}$")


class ReprovisionUserCommand(StrictModel):
    action: Literal["reprovision"] = "reprovision"
    user_id: DirectoryUserId


class SetUserGroupsCommand(StrictModel):
    action: Literal["set_groups"] = "set_groups"
    user_id: DirectoryUserId
    groups: list[ServiceGroupName] = Field(max_length=256)

    @field_validator("groups")
    @classmethod
    def unique_groups(cls, values: list[ServiceGroupName]) -> list[ServiceGroupName]:
        return _validate_service_groups(values)


class DeleteDirectoryIdentityCommand(StrictModel):
    action: Literal["delete_directory_identity"] = "delete_directory_identity"
    user_id: DirectoryUserId
    confirmation: DirectoryUserId

    @model_validator(mode="after")
    def confirmation_matches(self) -> DeleteDirectoryIdentityCommand:
        if self.confirmation != self.user_id:
            raise ValueError("identity deletion confirmation does not match")
        return self


IdentityCommand = Annotated[
    InviteUserCommand
    | SealedInviteUserCommand
    | ReprovisionUserCommand
    | SetUserGroupsCommand
    | DeleteDirectoryIdentityCommand,
    Field(discriminator="action"),
]


class IdentityOperation(StrictModel):
    kind: Literal[JobKind.IDENTITY] = JobKind.IDENTITY
    command: IdentityCommand


IdentityAction = Literal["invite", "reprovision", "set_groups", "delete_directory_identity"]
IdentityStepStatus = Literal["completed", "pending", "skipped", "warning", "failed"]
IdentityOutcome = Literal["completed", "completed_with_warnings", "partial_failure"]
IdentityStepKey = Annotated[str, StringConstraints(pattern=r"^[a-z][a-z0-9_]{0,63}$")]


class IdentityStepResult(StrictModel):
    key: IdentityStepKey
    status: IdentityStepStatus


class IdentityOperationResult(StrictModel):
    action: IdentityAction
    user_id: DirectoryUserId
    outcome: IdentityOutcome
    steps: list[IdentityStepResult] = Field(min_length=1, max_length=64)

    @model_validator(mode="after")
    def validate_outcome(self) -> IdentityOperationResult:
        _require_unique([step.key for step in self.steps])
        statuses = {step.status for step in self.steps}
        expected: IdentityOutcome
        if "failed" in statuses:
            expected = "partial_failure"
        elif "warning" in statuses:
            expected = "completed_with_warnings"
        else:
            expected = "completed"
        if self.outcome != expected:
            raise ValueError("identity outcome does not match its step statuses")
        return self


class ConfigApplyOperation(StrictModel):
    kind: Literal[JobKind.CONFIG_APPLY] = JobKind.CONFIG_APPLY
    revision_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    service: ServiceName


class SecretRotationOperation(StrictModel):
    kind: Literal[JobKind.SECRET_ROTATION] = JobKind.SECRET_ROTATION
    secret_names: list[Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z0-9_]{1,127}$")]] = Field(
        min_length=1,
        max_length=64,
    )

    @field_validator("secret_names")
    @classmethod
    def unique_secret_names(cls, values: list[str]) -> list[str]:
        return _require_unique(values)


class WebhookHealOperation(StrictModel):
    kind: Literal[JobKind.WEBHOOK_HEAL] = JobKind.WEBHOOK_HEAL
    service: ServiceName
    source: Literal["grafana"] = "grafana"
    alert_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")


OperationPayload = Annotated[
    GenerateOperation
    | VerifyOperation
    | ServiceVerifyOperation
    | DeployOperation
    | RecoverOperation
    | DestroyInfraOperation
    | DnsSyncOperation
    | MaintenanceOperation
    | BackupDrillOperation
    | UpdateOperation
    | RestoreDrillOperation
    | HostReconcileOperation
    | HostRemoveOperation
    | ContainerActionOperation
    | ServiceActionOperation
    | IdentityOperation
    | ConfigApplyOperation
    | SecretRotationOperation
    | WebhookHealOperation,
    Field(discriminator="kind"),
]


class JobRequest(StrictModel):
    idempotency_key: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9._~-]+$")
    operation: OperationPayload

    @property
    def kind(self) -> JobKind:
        return self.operation.kind


class DestroyPlanRequest(StrictModel):
    action: DestructionAction
    scopes: list[MachineId] = Field(min_length=1, max_length=256)

    @field_validator("scopes")
    @classmethod
    def unique_scopes(cls, values: list[MachineId]) -> list[MachineId]:
        return _require_unique(values)


class DestroyPlanSpec(DestroyPlanRequest):
    config_revision: str = Field(pattern=r"^[0-9a-f]{64}$")
    checkpoint_id: str = Field(pattern=r"^[0-9a-f]{32}$")
    checkpoint_verified_at: datetime
    evidence_digest: str = Field(pattern=r"^[0-9a-f]{64}$")


class PlanRecord(StrictModel):
    plan_id: str = Field(min_length=16, max_length=128)
    actor: str = Field(min_length=1, max_length=255)
    spec: DestroyPlanSpec
    plan_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    created_at: datetime


class ApprovalGrant(StrictModel):
    plan_id: str = Field(min_length=16, max_length=128)
    actor: str = Field(min_length=1, max_length=255)
    token: str = Field(min_length=16, max_length=512)
    expires_at: datetime


class JobRecord(StrictModel):
    job_id: str = Field(min_length=1, max_length=128)
    request: JobRequest
    state: JobState
    actor: str = Field(min_length=1, max_length=255)
    created_at: datetime
    updated_at: datetime
    cancel_requested: bool = False
    result: dict[str, Any] | None = None
    error: ErrorBody | None = None
    lease_owner: str | None = Field(default=None, max_length=255)
    lease_generation: int = Field(default=0, ge=0)
    lease_expires_at: datetime | None = None


class JobEvent(StrictModel):
    job_id: str = Field(min_length=1, max_length=128)
    sequence: int = Field(gt=0)
    timestamp: datetime
    level: EventLevel
    message: str = Field(min_length=1, max_length=4000)
    payload: dict[str, Any] = Field(default_factory=dict)


class AuditRecord(StrictModel):
    sequence: int = Field(gt=0)
    timestamp: datetime
    principal: str = Field(min_length=1, max_length=255)
    action: str = Field(min_length=1, max_length=128)
    resource: str = Field(min_length=1, max_length=512)
    outcome: Literal["ALLOWED", "DENIED", "FAILED"]
    details: dict[str, Any] = Field(default_factory=dict)


class ControllerHealth(StrictModel):
    status: Literal["ok", "degraded"]
    version: str = Field(min_length=1, max_length=32)
    database_ok: bool
    worker_ok: bool
    secret_store_ok: bool
    queued_jobs: int = Field(ge=0)
    running_jobs: int = Field(ge=0)
    checked_at: datetime = Field(default_factory=lambda: datetime.now(UTC), exclude=True)
