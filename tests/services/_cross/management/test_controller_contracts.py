from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError
from toolkit.controller.contracts import (
    ControllerHealth,
    DeployOperation,
    DestroyInfraOperation,
    ErrorEnvelope,
    HostReconcileOperation,
    HostRemoveOperation,
    IdentityOperation,
    IdentityOperationResult,
    IdentityStepResult,
    InviteUserCommand,
    JobEvent,
    JobKind,
    JobRequest,
    JobState,
    ServiceActionOperation,
    VerifyOperation,
)


def test_managed_host_operations_are_bounded_and_confirmed() -> None:
    reconcile = HostReconcileOperation(host_name="edge-01")
    remove = HostRemoveOperation(
        host_name="edge-01",
        expected_fingerprint="a" * 64,
        confirmation="edge-01",
    )

    assert reconcile.kind is JobKind.HOST_RECONCILE
    assert remove.kind is JobKind.HOST_REMOVE
    with pytest.raises(ValidationError, match="confirmation"):
        HostRemoveOperation(
            host_name="edge-01",
            expected_fingerprint="a" * 64,
            confirmation="other-host",
        )


def test_destructive_job_requires_plan_and_approval() -> None:
    with pytest.raises(ValidationError):
        JobRequest.model_validate(
            {
                "idempotency_key": "request-12345678",
                "operation": {"kind": "DESTROY_INFRA", "scopes": ["infra", "apps", "media"]},
            }
        )


def test_destructive_job_accepts_complete_proof() -> None:
    request = JobRequest(
        idempotency_key="request-12345678",
        operation=DestroyInfraOperation(
            action="retire_machine",
            scopes=["apps"],
            config_revision="d" * 64,
            plan_id="019f4ca8-9cf6-7d02-9e12-30def2bd32a1",
            plan_hash="a" * 64,
            approval_token="approval-token-1234",
        ),
    )

    assert request.kind is JobKind.DESTROY_INFRA
    assert request.operation.action == "retire_machine"
    assert request.operation.config_revision == "d" * 64
    assert request.operation.plan_hash == "a" * 64
    assert request.operation.approval_token == "approval-token-1234"


@pytest.mark.parametrize(
    "key",
    (
        "short",
        "contains spaces and is long",
        "contains/slash-12345",
        "x" * 129,
    ),
)
def test_idempotency_key_is_bounded_and_url_safe(key: str) -> None:
    with pytest.raises(ValidationError):
        JobRequest(operation={"kind": "VERIFY"}, idempotency_key=key)


def test_contracts_reject_unknown_fields() -> None:
    with pytest.raises(ValidationError):
        JobRequest.model_validate(
            {
                "kind": "VERIFY",
                "idempotency_key": "request-12345678",
                "unexpected": True,
            }
        )


def test_job_request_rejects_generic_arguments() -> None:
    with pytest.raises(ValidationError):
        JobRequest.model_validate(
            {
                "idempotency_key": "request-12345678",
                "operation": {
                    "kind": "DEPLOY",
                    "target": "infra",
                    "arguments": {"argv": ["sh", "-c", "id"]},
                },
            }
        )


def test_deploy_operation_matches_one_coordinated_workflow() -> None:
    request = JobRequest(
        idempotency_key="request-12345678",
        operation=DeployOperation(target="apps", skip_infrastructure=True, skip_dns=True),
    )
    assert request.kind is JobKind.DEPLOY
    assert request.operation.target == "apps"

    with pytest.raises(ValidationError):
        JobRequest.model_validate(
            {
                "idempotency_key": "request-12345678",
                "operation": {"kind": "DEPLOY", "target": "../../host"},
            }
        )


@pytest.mark.parametrize(
    "operation",
    [
        {"kind": "VERIFY", "targets": ["infra", "infra"]},
    ],
)
def test_operation_lists_reject_duplicates(operation: dict) -> None:
    with pytest.raises(ValidationError):
        JobRequest(idempotency_key="request-12345678", operation=operation)


def test_maintenance_operation_has_no_partial_task_surface() -> None:
    request = JobRequest(idempotency_key="maintenance-request-1234", operation={"kind": "MAINTENANCE"})

    assert request.operation.model_dump() == {"kind": JobKind.MAINTENANCE}
    with pytest.raises(ValidationError):
        JobRequest(
            idempotency_key="maintenance-request-1234",
            operation={"kind": "MAINTENANCE", "tasks": ["backup"]},
        )


def test_backup_drill_operation_has_no_unbounded_target_surface() -> None:
    request = JobRequest(idempotency_key="backup-drill-request-1234", operation={"kind": "BACKUP_DRILL"})

    assert request.operation.model_dump() == {"kind": JobKind.BACKUP_DRILL}
    with pytest.raises(ValidationError):
        JobRequest(
            idempotency_key="backup-drill-request-1234",
            operation={"kind": "BACKUP_DRILL", "path": "/"},
        )


def test_config_apply_is_revision_locked_and_service_scoped() -> None:
    request = JobRequest(
        idempotency_key="config-apply-request-1234",
        operation={"kind": "CONFIG_APPLY", "revision_hash": "a" * 64, "service": "music-sync"},
    )

    assert request.operation.model_dump() == {
        "kind": JobKind.CONFIG_APPLY,
        "revision_hash": "a" * 64,
        "service": "music-sync",
    }
    with pytest.raises(ValidationError):
        JobRequest(
            idempotency_key="config-apply-request-1234",
            operation={"kind": "CONFIG_APPLY", "revision_hash": "a" * 64},
        )


def test_update_actions_have_complete_and_disjoint_payloads() -> None:
    refresh = JobRequest(
        idempotency_key="update-refresh-1234",
        operation={"kind": "UPDATE", "action": "refresh"},
    )
    apply = JobRequest(
        idempotency_key="update-apply-123456",
        operation={"kind": "UPDATE", "action": "apply", "services": ["redis"], "revision": "a" * 64},
    )
    rollback = JobRequest(
        idempotency_key="update-rollback-1234",
        operation={"kind": "UPDATE", "action": "rollback", "revision": "b" * 64},
    )

    assert refresh.operation.model_dump()["services"] == []
    assert apply.operation.model_dump()["revision"] == "a" * 64
    assert rollback.operation.model_dump()["revision"] == "b" * 64
    for invalid in (
        {"kind": "UPDATE", "action": "apply", "services": ["redis"]},
        {"kind": "UPDATE", "action": "apply", "revision": "a" * 64},
        {"kind": "UPDATE", "action": "refresh", "services": ["redis"]},
        {"kind": "UPDATE", "action": "rollback"},
    ):
        with pytest.raises(ValidationError):
            JobRequest(idempotency_key="invalid-update-1234", operation=invalid)


def test_identity_invite_is_normalized_and_contains_no_password_surface() -> None:
    request = JobRequest(
        idempotency_key="identity-invite-1234",
        operation=IdentityOperation(
            command=InviteUserCommand(
                email=" Family@Example.COM ",
                display_name=" Family User ",
                groups=["homelab-media", "homelab-cloud"],
            )
        ),
    )

    assert request.kind is JobKind.IDENTITY
    assert request.operation.command.email == "family@example.com"
    assert request.operation.command.display_name == "Family User"
    assert "password" not in request.model_dump_json()

    with pytest.raises(ValidationError):
        JobRequest.model_validate(
            {
                "idempotency_key": "identity-invite-1234",
                "operation": {
                    "kind": "IDENTITY",
                    "command": {
                        "action": "invite",
                        "email": "family@example.com",
                        "groups": ["homelab-media"],
                        "password": "must-never-persist",
                    },
                },
            }
        )


@pytest.mark.parametrize(
    "groups",
    (["homelab-media", "homelab-media"], ["lldap_admin"], ["homelab-unknown"]),
)
def test_identity_commands_reject_duplicate_or_unmanaged_groups(groups: list[str]) -> None:
    with pytest.raises(ValidationError):
        InviteUserCommand(email="family@example.com", groups=groups)


def test_identity_directory_deletion_has_narrow_explicit_contract() -> None:
    request = JobRequest.model_validate(
        {
            "idempotency_key": "identity-delete-1234",
            "operation": {
                "kind": "IDENTITY",
                "command": {
                    "action": "delete_directory_identity",
                    "user_id": "family",
                    "confirmation": "family",
                },
            },
        }
    )

    assert request.operation.command.action == "delete_directory_identity"

    with pytest.raises(ValidationError):
        JobRequest.model_validate(
            {
                "idempotency_key": "identity-delete-invalid",
                "operation": {
                    "kind": "IDENTITY",
                    "command": {"action": "delete", "user_id": "family", "confirmation": "family"},
                },
            }
        )


def test_identity_result_rejects_free_form_or_duplicate_step_data() -> None:
    result = IdentityOperationResult(
        action="reprovision",
        user_id="family",
        outcome="partial_failure",
        steps=[
            IdentityStepResult(key="welcome_email", status="completed"),
            IdentityStepResult(key="vaultwarden_invite", status="failed"),
        ],
    )

    assert "message" not in result.model_dump_json()

    with pytest.raises(ValidationError):
        IdentityOperationResult.model_validate(
            {
                **result.model_dump(),
                "steps": [
                    {"key": "welcome_email", "status": "completed"},
                    {"key": "welcome_email", "status": "failed"},
                ],
            }
        )

    with pytest.raises(ValidationError):
        IdentityStepResult.model_validate({"key": "welcome_email", "status": "failed", "message": "secret-canary"})


def test_verify_contract_rejects_unimplemented_options() -> None:
    with pytest.raises(ValidationError):
        VerifyOperation.model_validate({"include_hooks": False})


def test_service_action_contract_is_parameterless_and_bounded() -> None:
    request = JobRequest(
        idempotency_key="service-action-1234",
        operation=ServiceActionOperation(service="music-sync", action="sync-now"),
    )

    assert request.kind is JobKind.SERVICE_ACTION
    with pytest.raises(ValidationError):
        ServiceActionOperation.model_validate(
            {
                "service": "music-sync",
                "action": "sync-now",
                "parameters": {"token": "must-not-persist"},
            }
        )


def test_error_envelope_has_one_stable_shape() -> None:
    error = ErrorEnvelope.from_code(
        "VALIDATION_ERROR",
        "Invalid request",
        {"field": "kind"},
    )

    assert error.model_dump() == {
        "error": {
            "code": "VALIDATION_ERROR",
            "message": "Invalid request",
            "details": {"field": "kind"},
        }
    }


def test_job_event_sequence_must_be_positive() -> None:
    with pytest.raises(ValidationError):
        JobEvent(
            job_id="019f4ca8-9cf6-7d02-9e12-30def2bd32a1",
            sequence=0,
            timestamp=datetime.now(UTC),
            level="INFO",
            message="queued",
        )


def test_controller_health_is_strict() -> None:
    health = ControllerHealth(
        status="ok",
        version="1",
        database_ok=True,
        worker_ok=True,
        secret_store_ok=True,
        queued_jobs=0,
        running_jobs=0,
    )
    assert health.model_dump() == {
        "status": "ok",
        "version": "1",
        "database_ok": True,
        "worker_ok": True,
        "secret_store_ok": True,
        "queued_jobs": 0,
        "running_jobs": 0,
    }

    with pytest.raises(ValidationError):
        ControllerHealth(
            status="unknown",
            version="1",
            database_ok=True,
            worker_ok=True,
            secret_store_ok=True,
            queued_jobs=0,
            running_jobs=0,
        )


def test_job_states_are_explicit() -> None:
    assert {state.value for state in JobState} == {
        "QUEUED",
        "RUNNING",
        "CANCEL_REQUESTED",
        "SUCCEEDED",
        "PARTIAL_FAILURE",
        "FAILED",
        "CANCELLED",
    }
