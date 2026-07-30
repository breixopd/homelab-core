from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from toolkit.controller.contracts import (
    DeployOperation,
    DestroyInfraOperation,
    DestroyPlanSpec,
    HostReconcileOperation,
    IdentityOperation,
    InviteUserCommand,
    JobRequest,
    JobState,
    MaintenanceOperation,
    RestoreDrillOperation,
    ServiceActionOperation,
    VerifyOperation,
)
from toolkit.controller.store import (
    ApprovalError,
    ControllerStore,
    IdempotencyConflictError,
    JobConflictError,
)


def _request(key: str = "request-12345678") -> JobRequest:
    return JobRequest(idempotency_key=key, operation=VerifyOperation())


def _store(path: Path, now: datetime | None = None) -> ControllerStore:
    current = now or datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
    return ControllerStore(path, clock=lambda: current)


def _destroy_plan_spec() -> DestroyPlanSpec:
    return DestroyPlanSpec(
        action="destroy_all",
        scopes=["infra", "apps", "media"],
        config_revision="c" * 64,
        checkpoint_id="a" * 32,
        checkpoint_verified_at=datetime(2026, 7, 10, 0, 0, tzinfo=UTC),
        evidence_digest="b" * 64,
    )


def test_store_enables_wal_and_owner_only_database(tmp_path: Path) -> None:
    path = tmp_path / "state" / "controller.db"
    store = _store(path)
    store.create_job(_request(), principal="owner")

    assert store.journal_mode() == "wal"
    assert path.parent.stat().st_mode & 0o777 == 0o700
    for artifact in path.parent.glob("controller.db*"):
        assert artifact.stat().st_mode & 0o777 == 0o600, artifact


def test_store_tolerates_sqlite_removing_wal_during_permission_reconciliation(tmp_path: Path, monkeypatch) -> None:
    path = tmp_path / "state" / "controller.db"
    store = _store(path)
    wal = path.with_name("controller.db-wal")
    wal.write_bytes(b"transient")
    real_chmod = os.chmod

    def disappearing_chmod(target, mode, *, follow_symlinks=True):
        if Path(target) == wal:
            wal.unlink()
            raise FileNotFoundError(target)
        return real_chmod(target, mode, follow_symlinks=follow_symlinks)

    monkeypatch.setattr("toolkit.controller.store.os.chmod", disappearing_chmod)

    store._secure_artifacts()


def test_identity_invite_payload_is_not_cleartext_in_sqlite(tmp_path: Path) -> None:
    path = tmp_path / "state" / "controller.db"
    store = _store(path)
    request = JobRequest(
        idempotency_key="identity-private-1234",
        operation=IdentityOperation(
            command=InviteUserCommand(
                email="private-person@example.com",
                display_name="Private Person",
                groups=["homelab-media"],
            )
        ),
    )

    job = store.create_job(request, principal="owner")
    replayed = store.create_job(request, principal="owner")

    raw = b"".join(artifact.read_bytes() for artifact in path.parent.glob("controller.db*"))
    assert b"private-person@example.com" not in raw
    assert b"Private Person" not in raw
    assert "private-person@example.com" not in job.model_dump_json()
    assert replayed.job_id == job.job_id
    key_path = path.parent / "controller-payload.key"
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert key_path.with_name("controller-payload.key.check").stat().st_mode & 0o777 == 0o600

    changed = request.model_copy(
        update={
            "operation": IdentityOperation(
                command=InviteUserCommand(
                    email="different-person@example.com",
                    display_name="Different Person",
                    groups=["homelab-media"],
                )
            )
        }
    )
    with pytest.raises(IdempotencyConflictError):
        store.create_job(changed, principal="owner")


def test_existing_controller_database_without_payload_key_fails_closed(tmp_path: Path) -> None:
    from toolkit.controller.payload_protection import PayloadKeyError

    path = tmp_path / "state" / "controller.db"
    _store(path)
    (path.parent / "controller-payload.key").unlink()

    with pytest.raises(PayloadKeyError):
        _store(path)


def test_existing_controller_database_with_wrong_payload_key_fails_closed(tmp_path: Path) -> None:
    from toolkit.controller.payload_protection import PayloadKeyError

    path = tmp_path / "state" / "controller.db"
    _store(path)
    (path.parent / "controller-payload.key").write_bytes(b"x" * 32)

    with pytest.raises(PayloadKeyError):
        _store(path)


def test_controller_payload_key_rejects_permissive_mode_or_symlink(tmp_path: Path) -> None:
    from toolkit.controller.payload_protection import PayloadKeyError

    path = tmp_path / "state" / "controller.db"
    _store(path)
    key_path = path.parent / "controller-payload.key"
    key_path.chmod(0o640)
    with pytest.raises(PayloadKeyError):
        _store(path)

    key_path.unlink()
    target = tmp_path / "outside.key"
    target.write_bytes(b"x" * 32)
    target.chmod(0o600)
    key_path.symlink_to(target)
    with pytest.raises(PayloadKeyError):
        _store(path)


def test_duplicate_idempotency_key_returns_original_job(tmp_path: Path) -> None:
    store = _store(tmp_path / "controller.db")
    first = store.create_job(_request(), principal="owner")
    second = store.create_job(_request(), principal="owner")

    assert second.job_id == first.job_id


def test_recent_jobs_is_bounded_newest_first_and_principal_scoped(tmp_path: Path) -> None:
    store = _store(tmp_path / "controller.db")
    first = store.create_job(_request("request-owner-first"), principal="owner")
    second = store.create_job(_request("request-owner-second"), principal="owner")
    automation = store.create_job(_request("request-automation"), principal="automation")

    assert [job.job_id for job in store.recent_jobs(principal="owner", limit=10)] == [second.job_id, first.job_id]
    assert {job.job_id for job in store.recent_jobs(principal=None, limit=10)} == {
        first.job_id,
        second.job_id,
        automation.job_id,
    }
    assert [job.job_id for job in store.recent_jobs(principal=None, limit=1)] == [automation.job_id]
    with pytest.raises(ValueError):
        store.recent_jobs(principal=None, limit=0)
    with pytest.raises(ValueError):
        store.recent_jobs(principal=None, limit=201)


def test_idempotency_key_rejects_payload_change_for_same_principal(tmp_path: Path) -> None:
    store = _store(tmp_path / "controller.db")
    store.create_job(_request(), principal="owner")
    changed = JobRequest(
        idempotency_key="request-12345678",
        operation=DeployOperation(target="infra"),
    )

    with pytest.raises(IdempotencyConflictError):
        store.create_job(changed, principal="owner")


def test_idempotency_is_scoped_to_principal(tmp_path: Path) -> None:
    store = _store(tmp_path / "controller.db")

    owner = store.create_job(_request(), principal="owner")
    automation = store.create_job(_request(), principal="automation")

    assert automation.job_id != owner.job_id


def test_only_one_store_connection_can_claim_a_live_job(tmp_path: Path) -> None:
    path = tmp_path / "controller.db"
    first_store = _store(path)
    second_store = _store(path)
    job = first_store.create_job(_request(), principal="owner")

    claimed = first_store.claim_job(job.job_id, worker_id="worker-a", lease_seconds=30)
    assert claimed.state is JobState.RUNNING

    with pytest.raises(JobConflictError):
        second_store.claim_job(job.job_id, worker_id="worker-b", lease_seconds=30)


def test_expired_job_lease_can_be_reclaimed(tmp_path: Path) -> None:
    path = tmp_path / "controller.db"
    start = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
    first_store = _store(path, start)
    job = first_store.create_job(_request(), principal="owner")
    first_store.claim_job(job.job_id, worker_id="worker-a", lease_seconds=30)

    later_store = _store(path, start + timedelta(seconds=31))
    reclaimed = later_store.claim_job(job.job_id, worker_id="worker-b", lease_seconds=30)

    assert reclaimed.lease_owner == "worker-b"
    assert reclaimed.lease_generation == 2


def test_claim_next_is_atomic_and_returns_oldest_job(tmp_path: Path) -> None:
    path = tmp_path / "controller.db"
    first_store = _store(path)
    second_store = _store(path)
    oldest = first_store.create_job(_request("request-oldest-1234"), principal="owner")
    first_store.create_job(_request("request-newest-1234"), principal="owner")

    claimed = first_store.claim_next(worker_id="worker-a", lease_seconds=30)
    assert claimed is not None
    assert claimed.job_id == oldest.job_id

    next_claimed = second_store.claim_next(worker_id="worker-b", lease_seconds=30)
    assert next_claimed is not None
    assert next_claimed.job_id != oldest.job_id


def test_lease_renewal_requires_current_owner(tmp_path: Path) -> None:
    store = _store(tmp_path / "controller.db")
    job = store.create_job(_request(), principal="owner")
    claimed = store.claim_job(job.job_id, worker_id="worker-a", lease_seconds=30)

    renewed = store.renew_lease(
        job.job_id,
        worker_id="worker-a",
        lease_generation=claimed.lease_generation,
        lease_seconds=60,
    )
    assert renewed.lease_expires_at is not None
    assert renewed.lease_expires_at > claimed.lease_expires_at

    with pytest.raises(JobConflictError):
        store.renew_lease(
            job.job_id,
            worker_id="worker-b",
            lease_generation=claimed.lease_generation,
            lease_seconds=60,
        )


def test_reclaimed_job_fences_stale_worker_events_and_transitions(tmp_path: Path) -> None:
    path = tmp_path / "controller.db"
    start = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
    first_store = _store(path, start)
    job = first_store.create_job(_request(), principal="owner")
    first_claim = first_store.claim_job(job.job_id, worker_id="worker-a", lease_seconds=30)

    later_store = _store(path, start + timedelta(seconds=31))
    second_claim = later_store.claim_job(job.job_id, worker_id="worker-a", lease_seconds=30)

    with pytest.raises(JobConflictError):
        first_store.append_event(
            job.job_id,
            "INFO",
            "stale result",
            worker_id="worker-a",
            lease_generation=first_claim.lease_generation,
        )
    with pytest.raises(JobConflictError):
        first_store.transition(
            job.job_id,
            expected=JobState.RUNNING,
            target=JobState.SUCCEEDED,
            worker_id="worker-a",
            lease_generation=first_claim.lease_generation,
        )

    finished = later_store.transition(
        job.job_id,
        expected=JobState.RUNNING,
        target=JobState.SUCCEEDED,
        worker_id="worker-a",
        lease_generation=second_claim.lease_generation,
    )
    assert finished.state is JobState.SUCCEEDED


def test_expired_lease_cannot_write_or_renew_before_reclaim(tmp_path: Path) -> None:
    path = tmp_path / "controller.db"
    start = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
    first_store = _store(path, start)
    job = first_store.create_job(_request(), principal="owner")
    claim = first_store.claim_job(job.job_id, worker_id="worker-a", lease_seconds=30)
    expired_store = _store(path, start + timedelta(seconds=31))

    with pytest.raises(JobConflictError):
        expired_store.renew_lease(
            job.job_id,
            worker_id="worker-a",
            lease_generation=claim.lease_generation,
            lease_seconds=30,
        )
    with pytest.raises(JobConflictError):
        expired_store.append_event(
            job.job_id,
            "INFO",
            "late event",
            worker_id="worker-a",
            lease_generation=claim.lease_generation,
        )
    with pytest.raises(JobConflictError):
        expired_store.transition(
            job.job_id,
            expected=JobState.RUNNING,
            target=JobState.SUCCEEDED,
            worker_id="worker-a",
            lease_generation=claim.lease_generation,
        )


def test_invalid_state_transition_is_rejected(tmp_path: Path) -> None:
    store = _store(tmp_path / "controller.db")
    job = store.create_job(_request(), principal="owner")

    with pytest.raises(JobConflictError):
        store.transition(job.job_id, expected=JobState.QUEUED, target=JobState.SUCCEEDED)


def test_cancellation_is_persisted_and_terminal_jobs_are_immutable(tmp_path: Path) -> None:
    store = _store(tmp_path / "controller.db")
    job = store.create_job(_request(), principal="owner")
    store.claim_job(job.job_id, worker_id="worker-a", lease_seconds=30)

    claimed = store.get_job(job.job_id)
    cancelling = store.request_cancel(job.job_id, principal="owner")
    assert cancelling.state is JobState.CANCEL_REQUESTED
    cancelled = store.transition(
        job.job_id,
        expected=JobState.CANCEL_REQUESTED,
        target=JobState.CANCELLED,
        worker_id="worker-a",
        lease_generation=claimed.lease_generation,
    )
    assert cancelled.cancel_requested is True

    with pytest.raises(JobConflictError):
        store.request_cancel(job.job_id, principal="owner")


def test_identity_cancellation_is_only_allowed_before_execution(tmp_path: Path) -> None:
    store = _store(tmp_path / "controller.db")
    request = JobRequest(
        idempotency_key="identity-cancel-1234",
        operation=IdentityOperation(command=InviteUserCommand(email="family@example.com", groups=["homelab-media"])),
    )
    queued = store.create_job(request, principal="owner")
    cancelled = store.request_cancel(queued.job_id, principal="owner")
    assert cancelled.state is JobState.CANCELLED

    running = store.create_job(
        request.model_copy(update={"idempotency_key": "identity-running-1234"}),
        principal="owner",
    )
    store.claim_job(running.job_id, worker_id="worker-a", lease_seconds=30)

    with pytest.raises(JobConflictError):
        store.request_cancel(running.job_id, principal="owner")

    assert store.get_job(running.job_id).state is JobState.RUNNING


def test_service_action_cancellation_is_only_allowed_before_execution(tmp_path: Path) -> None:
    store = _store(tmp_path / "controller.db")
    request = JobRequest(
        idempotency_key="service-action-cancel-1234",
        operation=ServiceActionOperation(service="music-sync", action="sync-now"),
    )
    queued = store.create_job(request, principal="owner")
    cancelled = store.request_cancel(queued.job_id, principal="owner")
    assert cancelled.state is JobState.CANCELLED

    running = store.create_job(
        request.model_copy(update={"idempotency_key": "service-action-running-1234"}),
        principal="owner",
    )
    store.claim_job(running.job_id, worker_id="worker-a", lease_seconds=30)

    with pytest.raises(JobConflictError):
        store.request_cancel(running.job_id, principal="owner")

    assert store.get_job(running.job_id).state is JobState.RUNNING


def test_restore_drill_cancellation_is_only_allowed_before_execution(tmp_path: Path) -> None:
    request = JobRequest(
        idempotency_key="restore-drill-cancel-1234",
        operation=RestoreDrillOperation(dump_id="dmp_" + "a" * 20),
    )
    store = _store(tmp_path / "controller.db")
    running = store.create_job(request, principal="owner")
    store.claim_job(running.job_id, worker_id="worker-a", lease_seconds=30)

    with pytest.raises(JobConflictError):
        store.request_cancel(running.job_id, principal="owner")


@pytest.mark.parametrize(
    "operation",
    [
        MaintenanceOperation(),
        HostReconcileOperation(host_name="nas-01"),
    ],
)
def test_non_cooperative_operations_cannot_be_cancelled_after_start(tmp_path: Path, operation) -> None:
    store = _store(tmp_path / "controller.db")
    job = store.create_job(
        JobRequest(idempotency_key=f"non-cooperative-{operation.kind.value.lower()}", operation=operation),
        principal="owner",
    )
    store.claim_job(job.job_id, worker_id="worker-a", lease_seconds=30)

    with pytest.raises(JobConflictError):
        store.request_cancel(job.job_id, principal="owner")


def test_expired_cancel_request_is_finalized_instead_of_reclaimed(tmp_path: Path) -> None:
    path = tmp_path / "controller.db"
    start = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
    first_store = _store(path, start)
    job = first_store.create_job(_request(), principal="owner")
    first_store.claim_job(job.job_id, worker_id="worker-a", lease_seconds=30)
    first_store.request_cancel(job.job_id, principal="requesting-operator")

    later_store = _store(path, start + timedelta(seconds=31))

    assert later_store.claim_next(worker_id="worker-b", lease_seconds=30) is None
    assert later_store.get_job(job.job_id).state is JobState.CANCELLED
    cancellation_audit = [record for record in later_store.audit_after(0) if record.action == "JOB_CANCEL_REQUEST"]
    assert cancellation_audit[0].principal == "requesting-operator"
    assert any(record.action == "JOB_CANCEL_RECOVER" for record in later_store.audit_after(0))


def test_events_replay_after_sequence_without_duplicates(tmp_path: Path) -> None:
    store = _store(tmp_path / "controller.db")
    job = store.create_job(_request(), principal="owner")
    first = store.append_event(job.job_id, "INFO", "one")
    second = store.append_event(job.job_id, "INFO", "two", {"step": 2})

    replay = store.events_after(job.job_id, first.sequence)

    assert [event.sequence for event in replay] == [second.sequence]
    assert replay[0].payload == {"step": 2}


def test_invalid_event_is_rejected_before_it_can_poison_replay(tmp_path: Path) -> None:
    store = _store(tmp_path / "controller.db")
    job = store.create_job(_request(), principal="owner")

    with pytest.raises(Exception):
        store.append_event(job.job_id, "INFO", "x" * 4001)

    assert store.events_after(job.job_id, 0) == []


def test_event_replay_is_bounded(tmp_path: Path) -> None:
    store = _store(tmp_path / "controller.db")
    job = store.create_job(_request(), principal="owner")
    for index in range(5):
        store.append_event(job.job_id, "INFO", f"event {index}")

    assert len(store.events_after(job.job_id, 0, limit=2)) == 2


def test_terminal_transition_persists_event_and_worker_audit_atomically(tmp_path: Path) -> None:
    store = _store(tmp_path / "controller.db")
    job = store.create_job(_request(), principal="owner")
    claimed = store.claim_job(job.job_id, worker_id="worker-a", lease_seconds=30)

    finished = store.transition(
        job.job_id,
        expected=JobState.RUNNING,
        target=JobState.SUCCEEDED,
        result={"ok": True},
        event=("INFO", "Job succeeded", {}),
        worker_id="worker-a",
        lease_generation=claimed.lease_generation,
    )

    assert finished.state is JobState.SUCCEEDED
    assert [event.message for event in store.events_after(job.job_id, 0)] == ["Job succeeded"]
    transitions = [record for record in store.audit_after(0) if record.action == "JOB_TRANSITION"]
    assert transitions[-1].principal == "worker-a"


def test_destructive_job_consumes_actor_bound_approval_atomically(tmp_path: Path) -> None:
    store = _store(tmp_path / "controller.db")
    plan = store.create_plan(_destroy_plan_spec(), actor="owner")
    approval = store.issue_approval(plan.plan_id, actor="owner", ttl=timedelta(minutes=5))
    request = JobRequest(
        idempotency_key="destroy-request-1234",
        operation=DestroyInfraOperation(
            action=plan.spec.action,
            scopes=plan.spec.scopes,
            config_revision=plan.spec.config_revision,
            plan_id=plan.plan_id,
            plan_hash=plan.plan_hash,
            approval_token=approval.token,
        ),
    )

    job = store.create_job(request, principal="owner")
    assert job.state is JobState.QUEUED
    assert store.create_job(request, principal="owner").job_id == job.job_id

    replay = request.model_copy(update={"idempotency_key": "destroy-request-5678"})
    with pytest.raises(ApprovalError):
        store.create_job(replay, principal="owner")


def test_approval_rejects_wrong_actor_expiry_and_plan_hash(tmp_path: Path) -> None:
    path = tmp_path / "controller.db"
    start = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)
    store = _store(path, start)
    plan = store.create_plan(_destroy_plan_spec(), actor="owner")
    approval = store.issue_approval(plan.plan_id, actor="owner", ttl=timedelta(seconds=30))

    def request(*, principal_plan_hash: str = plan.plan_hash) -> JobRequest:
        return JobRequest(
            idempotency_key="destroy-request-1234",
            operation=DestroyInfraOperation(
                action="destroy_all",
                scopes=["infra", "apps", "media"],
                config_revision="c" * 64,
                plan_id=plan.plan_id,
                plan_hash=principal_plan_hash,
                approval_token=approval.token,
            ),
        )

    with pytest.raises(ApprovalError):
        store.create_job(request(), principal="different-owner")
    with pytest.raises(ApprovalError):
        store.create_job(request(principal_plan_hash="b" * 64), principal="owner")

    expired_store = _store(path, start + timedelta(seconds=31))
    with pytest.raises(ApprovalError):
        expired_store.create_job(request(), principal="owner")


@pytest.mark.parametrize(
    "operation_update",
    (
        {"action": "destroy_all"},
        {"config_revision": "d" * 64},
    ),
)
def test_destructive_job_must_reproduce_plan_action_and_revision(tmp_path: Path, operation_update) -> None:
    store = _store(tmp_path / "controller.db")
    spec = DestroyPlanSpec(
        action="retire_machine",
        scopes=["apps"],
        config_revision="c" * 64,
        checkpoint_id="a" * 32,
        checkpoint_verified_at=datetime(2026, 7, 10, tzinfo=UTC),
        evidence_digest="b" * 64,
    )
    plan = store.create_plan(spec, actor="owner")
    approval = store.issue_approval(plan.plan_id, actor="owner", ttl=timedelta(minutes=5))
    operation = DestroyInfraOperation(
        action=spec.action,
        scopes=spec.scopes,
        config_revision=spec.config_revision,
        plan_id=plan.plan_id,
        plan_hash=plan.plan_hash,
        approval_token=approval.token,
    ).model_copy(update=operation_update)

    with pytest.raises(ApprovalError):
        store.create_job(
            JobRequest(idempotency_key="destroy-request-1234", operation=operation),
            principal="owner",
        )


def test_audit_records_are_append_only_and_ordered(tmp_path: Path) -> None:
    store = _store(tmp_path / "controller.db")
    first = store.append_audit("owner", "PLAN_CREATE", "plan:one", "ALLOWED")
    second = store.append_audit("owner", "JOB_CREATE", "job:one", "ALLOWED", {"kind": "VERIFY"})

    records = store.audit_after(first.sequence)
    assert [record.sequence for record in records] == [second.sequence]
    assert records[0].details == {"kind": "VERIFY"}


def test_successful_mutations_emit_audit_without_approval_secrets(tmp_path: Path) -> None:
    store = _store(tmp_path / "controller.db")
    plan = store.create_plan(_destroy_plan_spec(), actor="owner")
    store.issue_approval(plan.plan_id, actor="owner", ttl=timedelta(minutes=5))
    store.create_job(_request(), principal="owner")

    records = store.audit_after(0)
    assert [record.action for record in records] == ["PLAN_CREATE", "APPROVAL_ISSUE", "JOB_CREATE"]
    serialized = " ".join(str(record.model_dump()) for record in records)
    assert "approval_token" not in serialized
    assert "token_hash" not in serialized
