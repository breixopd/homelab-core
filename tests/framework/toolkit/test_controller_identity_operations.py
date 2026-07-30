from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from unittest.mock import MagicMock

from toolkit.controller.contracts import (
    IdentityOperation,
    InviteUserCommand,
    JobRequest,
    JobState,
)
from toolkit.controller.identity_api import DirectoryMutationError
from toolkit.controller.operations import build_operation_registry
from toolkit.controller.store import ControllerStore
from toolkit.controller.worker import ControllerWorker
from toolkit.core.deploy.operation_lease import OperationLease


def _request() -> JobRequest:
    return JobRequest(
        idempotency_key="identity-operation-1234",
        operation=IdentityOperation(command=InviteUserCommand(email="family@example.com", groups=["homelab-media"])),
    )


def _run(tmp_path: Path, monkeypatch, execute) -> tuple[ControllerStore, str]:
    store = ControllerStore(tmp_path / "controller.db")
    job = store.create_job(_request(), principal="local:operator")
    monkeypatch.setattr("toolkit.controller.identity_api.execute_directory_command", execute)
    ControllerWorker(
        store,
        build_operation_registry(tmp_path),
        worker_id="worker-a",
    ).run_once()
    return store, job.job_id


def test_identity_handler_persists_result_events_and_action_audit(tmp_path: Path, monkeypatch) -> None:
    execute = MagicMock(
        return_value={
            "action": "invite",
            "user_id": "family",
            "outcome": "completed",
            "steps": [
                {"key": "directory", "status": "completed"},
                {"key": "welcome_email", "status": "completed"},
            ],
        }
    )

    store, job_id = _run(tmp_path, monkeypatch, execute)

    finished = store.get_job(job_id)
    assert finished.state is JobState.SUCCEEDED
    assert finished.result == {
        "action": "invite",
        "user_id": "family",
        "outcome": "completed",
        "steps": [
            {"key": "directory", "status": "completed"},
            {"key": "welcome_email", "status": "completed"},
        ],
    }
    executed_command = execute.call_args.args[1]
    assert isinstance(executed_command, InviteUserCommand)
    assert executed_command.email == "family@example.com"
    assert [event.message for event in store.events_after(job_id, 0)] == ["Job started", "Job succeeded"]
    identity_audit = [record for record in store.audit_after(0) if record.action == "IDENTITY_INVITE"]
    assert len(identity_audit) == 1
    assert identity_audit[0].resource == "identity:family"
    assert identity_audit[0].outcome == "ALLOWED"
    assert "family@example.com" not in identity_audit[0].model_dump_json()


def test_identity_handler_maps_safe_directory_error(tmp_path: Path, monkeypatch) -> None:
    def reject(*_args, **_kwargs):
        raise DirectoryMutationError("FORBIDDEN", "Built-in directory accounts cannot be changed")

    store, job_id = _run(tmp_path, monkeypatch, reject)

    finished = store.get_job(job_id)
    assert finished.state is JobState.FAILED
    assert finished.error is not None
    assert finished.error.code == "FORBIDDEN"
    assert finished.error.message == "Built-in directory accounts cannot be changed"


def test_identity_handler_redacts_unexpected_exception(tmp_path: Path, monkeypatch) -> None:
    def explode(*_args, **_kwargs):
        raise RuntimeError("directory-password-canary")

    store, job_id = _run(tmp_path, monkeypatch, explode)

    finished = store.get_job(job_id)
    assert finished.state is JobState.FAILED
    assert finished.error is not None
    assert finished.error.code == "OPERATION_FAILED"
    assert finished.error.message == "Identity operation failed"
    assert "directory-password-canary" not in finished.model_dump_json()


def test_identity_handler_rejects_conflicting_mutation_lease(tmp_path: Path, monkeypatch) -> None:
    execute = MagicMock()
    lease = OperationLease.acquire(tmp_path, "other-mutation")
    try:
        store, job_id = _run(tmp_path, monkeypatch, execute)
    finally:
        lease.release()

    finished = store.get_job(job_id)
    assert finished.state is JobState.FAILED
    assert finished.error is not None
    assert finished.error.code == "CONFLICT"
    execute.assert_not_called()


def test_identity_handler_rejects_tampered_invite_ciphertext(tmp_path: Path, monkeypatch) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    job = store.create_job(_request(), principal="local:operator")
    with sqlite3.connect(store.path) as connection:
        raw = connection.execute("SELECT request_json FROM jobs WHERE job_id = ?", (job.job_id,)).fetchone()[0]
        request = json.loads(raw)
        ciphertext = request["operation"]["command"]["ciphertext"]
        request["operation"]["command"]["ciphertext"] = ("A" if ciphertext[0] != "A" else "B") + ciphertext[1:]
        connection.execute(
            "UPDATE jobs SET request_json = ? WHERE job_id = ?",
            (json.dumps(request, sort_keys=True, separators=(",", ":")), job.job_id),
        )
    execute = MagicMock()
    monkeypatch.setattr("toolkit.controller.identity_api.execute_directory_command", execute)

    ControllerWorker(
        store,
        build_operation_registry(tmp_path),
        worker_id="worker-a",
    ).run_once()

    finished = store.get_job(job.job_id)
    assert finished.state is JobState.FAILED
    assert finished.error is not None
    assert finished.error.code == "OPERATION_FAILED"
    assert finished.error.message == "Identity operation failed"
    execute.assert_not_called()


def test_identity_partial_result_has_distinct_terminal_state(tmp_path: Path, monkeypatch) -> None:
    execute = MagicMock(
        return_value={
            "action": "invite",
            "user_id": "family",
            "outcome": "partial_failure",
            "steps": [
                {"key": "directory", "status": "completed"},
                {"key": "welcome_email", "status": "failed"},
            ],
        }
    )

    store, job_id = _run(tmp_path, monkeypatch, execute)

    finished = store.get_job(job_id)
    assert finished.state is JobState.PARTIAL_FAILURE
    assert [event.message for event in store.events_after(job_id, 0)][-1] == "Job completed with partial failure"
    identity_audit = [record for record in store.audit_after(0) if record.action == "IDENTITY_INVITE"]
    assert identity_audit[-1].outcome == "FAILED"
    assert identity_audit[-1].details["outcome"] == "partial_failure"
