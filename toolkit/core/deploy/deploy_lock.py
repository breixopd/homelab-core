"""Deploy lock status for CLI and WebUI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from toolkit.core.deploy.operation_lease import OperationLease


@dataclass
class DeployLockStatus:
    locked: bool
    pid: int | None = None
    alive: bool = False
    operation: str | None = None
    cancel_requested: bool = False
    message: str = ""

    @property
    def blocking(self) -> bool:
        return self.locked and self.alive


def read_deploy_lock(root: Path) -> DeployLockStatus:
    """Inspect the operation lease without changing diagnostic state."""
    inspection = OperationLease.inspect(root)
    if inspection.snapshot is None and not inspection.active:
        return DeployLockStatus(locked=False, message="No deploy in progress")
    if inspection.snapshot is None:
        return DeployLockStatus(
            locked=inspection.active,
            alive=inspection.active,
            message="Operation lease is active but its diagnostic record is invalid"
            if inspection.active
            else "Inactive operation lease has an invalid diagnostic record",
        )
    snapshot = inspection.snapshot
    return DeployLockStatus(
        locked=inspection.active,
        pid=snapshot.pid,
        alive=inspection.active,
        operation=snapshot.operation,
        cancel_requested=snapshot.cancel_requested,
        message=(
            f"Cancellation requested for {snapshot.operation} (pid {snapshot.pid})"
            if inspection.active and snapshot.cancel_requested
            else f"{snapshot.operation.title()} in progress (pid {snapshot.pid})"
            if inspection.active
            else f"Last {snapshot.operation} lease released (pid {snapshot.pid})"
        ),
    )


def cancel_active_deploy(root: Path) -> DeployLockStatus:
    """Request cancellation of the current owner without deleting its lease."""
    before = OperationLease.inspect(root)
    if not before.active or before.snapshot is None:
        return DeployLockStatus(locked=False, message="No deploy lock to cancel")
    after = OperationLease.request_active_cancel(root, expected_lease_id=before.snapshot.lease_id)
    if after.snapshot is not None and after.snapshot.lease_id == before.snapshot.lease_id:
        return read_deploy_lock(root)
    return DeployLockStatus(
        locked=after.active,
        pid=after.snapshot.pid if after.snapshot else None,
        alive=after.active,
        operation=after.snapshot.operation if after.snapshot else None,
        message="Operation changed before cancellation could be requested",
    )
