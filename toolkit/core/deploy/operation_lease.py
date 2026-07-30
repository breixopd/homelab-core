"""Process-scoped operation leases for deployment workflows.

The lock file is intentionally retained after release as diagnostic state. The
advisory file lock, not the file's presence or contents, is the sole authority
for whether an operation currently owns the deployment boundary.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import asdict, dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TextIO


class LeaseBusyError(RuntimeError):
    """Raised when another operation owns the deployment lease."""

    def __init__(self, path: Path):
        self.path = path
        super().__init__(f"Operation lease is held ({path.name})")


class OperationCancelledError(RuntimeError):
    """Raised when the current lease owner reaches a cancellation checkpoint."""

    def __init__(self, *, lease_id: str, operation: str):
        self.lease_id = lease_id
        self.operation = operation
        super().__init__(f"{operation} operation cancelled by request")


@dataclass(frozen=True)
class LeaseSnapshot:
    lease_id: str
    operation: str
    pid: int
    started_at: str
    cancel_requested: bool


@dataclass(frozen=True)
class LeaseInspection:
    active: bool
    snapshot: LeaseSnapshot | None


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class OperationLease:
    """An exclusive operation lease backed by a non-truncating flock file."""

    def __init__(self, path: Path, handle: TextIO, snapshot: LeaseSnapshot):
        self._path = path
        self._handle = handle
        self._snapshot = snapshot
        self._released = False
        self._cancellation_shielded = False

    @property
    def snapshot(self) -> LeaseSnapshot:
        return replace(self._snapshot, cancel_requested=self._cancel_requested())

    @classmethod
    def acquire(cls, root: Path, operation: str) -> OperationLease:
        path = root.resolve() / ".deploy.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        handle = cls._open_regular(path, create=True)
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise LeaseBusyError(path) from exc

        snapshot = LeaseSnapshot(
            lease_id=uuid.uuid4().hex,
            operation=operation,
            pid=os.getpid(),
            started_at=_utc_now(),
            cancel_requested=False,
        )
        try:
            cls._clear_cancel_request(path, expected_lease_id=None)
            cls._write_locked(handle, snapshot)
        except BaseException:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            handle.close()
            raise
        return cls(path, handle, snapshot)

    @classmethod
    def inspect(cls, root: Path) -> LeaseInspection:
        """Read diagnostic metadata without mutating the lock file."""
        path = root.resolve() / ".deploy.lock"
        try:
            handle = cls._open_regular(path, create=False)
        except FileNotFoundError:
            return LeaseInspection(active=False, snapshot=None)
        try:
            try:
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                active = True
            else:
                active = False
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            snapshot = cls._read_snapshot(handle)
            if snapshot is not None:
                snapshot = replace(
                    snapshot,
                    cancel_requested=cls._cancel_requested_for(root, snapshot.lease_id),
                )
            return LeaseInspection(active=active, snapshot=snapshot)
        finally:
            handle.close()

    def request_cancel(self) -> None:
        """Persist a cancellation request scoped to this lease instance."""
        self._require_owned()
        self.request_active_cancel(self._path.parent, expected_lease_id=self._snapshot.lease_id)

    def raise_if_cancelled(self) -> None:
        """Raise when this lease has a pending cooperative cancellation request."""
        self._require_owned()
        if self._cancellation_shielded:
            return
        if self._cancel_requested():
            raise OperationCancelledError(
                lease_id=self._snapshot.lease_id,
                operation=self._snapshot.operation,
            )

    @contextmanager
    def shield_cancellation(self) -> Iterator[OperationLease]:
        """Protect an in-progress rollback from leaving credentials half-applied."""
        self._require_owned()
        previous = self._cancellation_shielded
        self._cancellation_shielded = True
        if not previous:
            self._clear_cancel_request(self._path, expected_lease_id=self._snapshot.lease_id)
        try:
            yield self
        finally:
            self._cancellation_shielded = previous

    def release(self) -> None:
        """Release ownership while preserving the diagnostic record."""
        if self._released:
            return
        self._released = True
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
        finally:
            self._handle.close()
            self._clear_cancel_request(self._path, expected_lease_id=self._snapshot.lease_id)

    def _require_owned(self) -> None:
        if self._released:
            raise RuntimeError("Operation lease has already been released")

    def assert_owns_root(self, root: Path) -> None:
        """Reject a preheld lease from another repository or a replaced lock inode."""
        self._require_owned()
        expected = root.resolve() / ".deploy.lock"
        if self._path != expected:
            raise RuntimeError("Operation lease belongs to another homelab root")
        try:
            open_file = os.fstat(self._handle.fileno())
            current_file = expected.stat(follow_symlinks=False)
        except OSError as exc:
            raise RuntimeError("Operation lease lock file is unavailable") from exc
        if not stat.S_ISREG(current_file.st_mode) or (open_file.st_dev, open_file.st_ino) != (
            current_file.st_dev,
            current_file.st_ino,
        ):
            raise RuntimeError("Operation lease lock file was replaced")

    @classmethod
    def request_active_cancel(cls, root: Path, *, expected_lease_id: str | None = None) -> LeaseInspection:
        """Request cancellation for the currently active lease.

        The marker includes the lease id, so a replacement operation cannot be
        cancelled by a delayed request intended for its predecessor.
        """
        inspection = cls.inspect(root)
        if not inspection.active or inspection.snapshot is None:
            return inspection
        if expected_lease_id is not None and inspection.snapshot.lease_id != expected_lease_id:
            return inspection

        marker = cls._cancel_path(root)
        temporary = marker.with_name(f"{marker.name}.{uuid.uuid4().hex}.tmp")
        try:
            handle = cls._open_regular(temporary, create=True, exclusive=True)
            with handle:
                handle.write(inspection.snapshot.lease_id + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, marker)
        finally:
            temporary.unlink(missing_ok=True)
        return cls.inspect(root)

    def _cancel_requested(self) -> bool:
        return self._cancel_requested_for(self._path.parent, self._snapshot.lease_id)

    @classmethod
    def _cancel_requested_for(cls, root: Path, lease_id: str) -> bool:
        marker = cls._cancel_path(root)
        try:
            with cls._open_regular(marker, create=False) as handle:
                return handle.read().strip() == lease_id
        except OSError:
            return False

    @staticmethod
    def _cancel_path(root: Path) -> Path:
        return root.resolve() / ".deploy.cancel"

    @classmethod
    def _clear_cancel_request(cls, lock_path: Path, *, expected_lease_id: str | None) -> None:
        marker = cls._cancel_path(lock_path.parent)
        try:
            with cls._open_regular(marker, create=False) as handle:
                marker_lease_id = handle.read().strip()
            if expected_lease_id is None or marker_lease_id == expected_lease_id:
                marker.unlink(missing_ok=True)
        except OSError:
            return

    @staticmethod
    def _open_regular(path: Path, *, create: bool, exclusive: bool = False) -> TextIO:
        """Open state without following attacker-controlled filesystem links."""
        flags = os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW
        if create:
            flags |= os.O_CREAT
        if exclusive:
            flags |= os.O_EXCL
        descriptor = os.open(path, flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError(f"operation state path is not a regular file: {path.name}")
            os.fchmod(descriptor, 0o600)
            return os.fdopen(descriptor, "r+", encoding="utf-8")
        except BaseException:
            os.close(descriptor)
            raise

    @staticmethod
    def _write_locked(handle: TextIO, snapshot: LeaseSnapshot) -> None:
        handle.seek(0)
        handle.truncate()
        handle.write(json.dumps(asdict(snapshot), sort_keys=True) + "\n")
        handle.flush()
        os.fsync(handle.fileno())

    @staticmethod
    def _read_snapshot(handle: TextIO) -> LeaseSnapshot | None:
        handle.seek(0)
        try:
            payload = json.loads(handle.read())
            return LeaseSnapshot(
                lease_id=str(payload["lease_id"]),
                operation=str(payload["operation"]),
                pid=int(payload["pid"]),
                started_at=str(payload["started_at"]),
                cancel_requested=bool(payload["cancel_requested"]),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None
