"""Cross-process coordination for canonical configuration reads and writes."""

from __future__ import annotations

import fcntl
import hashlib
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from toolkit.core.config.storage import config_path


class ConfigurationUnavailableError(RuntimeError):
    pass


class ConfigurationBusyError(RuntimeError):
    """Raised when an active operation owns the desired-state boundary."""


@contextmanager
def configuration_lock(root: Path) -> Iterator[None]:
    """Serialize desired-state mutations across the controller and CLI."""
    state_dir = root.resolve() / ".homelab-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_dir.chmod(0o700)
    descriptor = os.open(
        state_dir / "configuration.lock",
        os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def configuration_mutation(root: Path, operation: str) -> Iterator[None]:
    """Serialize a desired-state write against deployments and other writes."""
    from toolkit.core.deploy.operation_lease import LeaseBusyError, OperationLease

    try:
        lease = OperationLease.acquire(root, operation)
    except LeaseBusyError as exc:
        raise ConfigurationBusyError("Another deployment or mutation is already running") from exc
    try:
        with configuration_lock(root):
            yield
    finally:
        lease.release()


def config_revision(root: Path) -> str:
    """Return the SHA-256 revision of the persisted canonical configuration."""
    try:
        content = config_path(root).read_bytes()
    except OSError as exc:
        raise ConfigurationUnavailableError("configuration is unavailable") from exc
    return hashlib.sha256(content).hexdigest()
