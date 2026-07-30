from __future__ import annotations

from pathlib import Path

from toolkit.cli.deploy_cmd import _blocking_external_deploy
from toolkit.core.deploy.deploy_lock import cancel_active_deploy, read_deploy_lock
from toolkit.core.deploy.operation_lease import LeaseSnapshot, OperationLease


def test_read_deploy_lock_missing(tmp_path: Path):
    status = read_deploy_lock(tmp_path)
    assert not status.blocking
    assert "No deploy" in status.message


def test_preheld_rotation_lease_does_not_block_its_own_deploy(tmp_path: Path) -> None:
    lease = OperationLease.acquire(tmp_path, "secret-rotation")
    try:
        assert _blocking_external_deploy(tmp_path, lease) is None
    finally:
        lease.release()


def test_foreign_lease_identity_cannot_bypass_active_deploy(tmp_path: Path) -> None:
    active = OperationLease.acquire(tmp_path, "deploy")

    class ForeignLease:
        snapshot = LeaseSnapshot(
            lease_id="foreign",
            operation="secret-rotation",
            pid=active.snapshot.pid,
            started_at=active.snapshot.started_at,
            cancel_requested=False,
        )

    try:
        assert _blocking_external_deploy(tmp_path, ForeignLease()) is not None
    finally:
        active.release()


def test_cancel_preserves_unlocked_diagnostic_record(tmp_path: Path):
    lock = tmp_path / ".deploy.lock"
    lock.write_text('{"pid": 999999999}\n', encoding="utf-8")
    status = cancel_active_deploy(tmp_path)
    assert not status.blocking
    assert lock.exists()


def test_cancel_requests_current_lease_without_removing_lock(tmp_path: Path):
    lease = OperationLease.acquire(tmp_path, "deploy")
    try:
        status = cancel_active_deploy(tmp_path)
        assert status.blocking
        assert status.cancel_requested
        assert lease.snapshot.cancel_requested
        assert (tmp_path / ".deploy.lock").exists()
    finally:
        lease.release()
