from __future__ import annotations

from pathlib import Path

import pytest
from toolkit.core.deploy.operation_lease import (
    LeaseBusyError,
    OperationCancelledError,
    OperationLease,
)


def test_live_wipe_cannot_replace_deploy_lease(tmp_path: Path):
    deploy = OperationLease.acquire(tmp_path, "deploy")
    try:
        with pytest.raises(LeaseBusyError):
            OperationLease.acquire(tmp_path, "wipe")
        assert (tmp_path / ".deploy.lock").exists()
    finally:
        deploy.release()


def test_cancel_marks_live_lease_without_unlinking(tmp_path: Path):
    lease = OperationLease.acquire(tmp_path, "deploy")
    try:
        lease.request_cancel()
        assert lease.snapshot.cancel_requested is True
        assert (tmp_path / ".deploy.lock").exists()
    finally:
        lease.release()


def test_cancel_checkpoint_raises_typed_exception_for_current_lease(tmp_path: Path):
    lease = OperationLease.acquire(tmp_path, "deploy")
    try:
        lease.request_cancel()

        with pytest.raises(OperationCancelledError) as raised:
            lease.raise_if_cancelled()

        assert raised.value.lease_id == lease.snapshot.lease_id
        assert raised.value.operation == "deploy"
    finally:
        lease.release()


def test_release_preserves_diagnostic_record(tmp_path: Path):
    lease = OperationLease.acquire(tmp_path, "recover")
    lease.release()

    replacement = OperationLease.acquire(tmp_path, "deploy")
    try:
        assert replacement.snapshot.operation == "deploy"
        assert (tmp_path / ".deploy.lock").exists()
    finally:
        replacement.release()


def test_cancel_request_does_not_affect_replacement_lease(tmp_path: Path):
    first = OperationLease.acquire(tmp_path, "deploy")
    first.request_cancel()
    first.release()

    replacement = OperationLease.acquire(tmp_path, "recover")
    try:
        assert replacement.snapshot.cancel_requested is False
    finally:
        replacement.release()


def test_cancellation_shield_ignores_delayed_request_until_rollback_finishes(tmp_path: Path) -> None:
    lease = OperationLease.acquire(tmp_path, "secret-rotation")
    try:
        with lease.shield_cancellation():
            lease.request_cancel()
            lease.raise_if_cancelled()
            with lease.shield_cancellation():
                lease.raise_if_cancelled()

        with pytest.raises(OperationCancelledError):
            lease.raise_if_cancelled()
    finally:
        lease.release()


def test_cancellation_shield_clears_request_that_triggered_rollback(tmp_path: Path) -> None:
    lease = OperationLease.acquire(tmp_path, "secret-rotation")
    try:
        lease.request_cancel()
        with lease.shield_cancellation():
            lease.raise_if_cancelled()
        lease.raise_if_cancelled()
    finally:
        lease.release()


def test_lease_rejects_symlink_lock_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("do not modify\n")
    (tmp_path / ".deploy.lock").symlink_to(target)

    with pytest.raises(OSError):
        OperationLease.acquire(tmp_path, "deploy")

    assert target.read_text() == "do not modify\n"


def test_preheld_operation_lease_is_scoped_to_its_root(tmp_path: Path) -> None:
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    lease = OperationLease.acquire(first, "deploy")
    try:
        with pytest.raises(RuntimeError, match="another homelab root"):
            lease.assert_owns_root(second)
    finally:
        lease.release()


def test_cancel_marker_symlink_is_replaced_without_touching_target(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.write_text("do not modify\n")
    marker = tmp_path / ".deploy.cancel"
    marker.symlink_to(target)
    lease = OperationLease.acquire(tmp_path, "deploy")
    try:
        lease.request_cancel()
        assert lease.snapshot.cancel_requested is True
    finally:
        lease.release()

    assert target.read_text() == "do not modify\n"
