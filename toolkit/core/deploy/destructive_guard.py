"""Shared safety primitives for destructive deployment operations."""

from __future__ import annotations

import json
import os
import tempfile
import uuid
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from pathlib import Path


class RecoveryCheckpointRequiredError(RuntimeError):
    """Raised when a destructive operation lacks a fresh recovery proof."""


@dataclass(frozen=True)
class RecoveryCheckpoint:
    checkpoint_id: str
    verified_at: datetime
    scope: tuple[str, ...]
    evidence: dict[str, str]


class ResourcesStillPresentError(RuntimeError):
    """Raised when infrastructure remains after a reported destroy."""


def record_verified_checkpoint(root: Path, scope: Iterable[str], evidence_files: Iterable[Path]) -> RecoveryCheckpoint:
    """Record a restore-verified checkpoint with hashes of its evidence files."""
    evidence: dict[str, str] = {}
    for source in evidence_files:
        resolved = source.resolve()
        if not resolved.is_file():
            raise RecoveryCheckpointRequiredError(f"Checkpoint evidence is missing: {source}")
        evidence[str(resolved)] = sha256(resolved.read_bytes()).hexdigest()
    if not evidence:
        raise RecoveryCheckpointRequiredError("At least one restore-drill evidence file is required")

    checkpoint = RecoveryCheckpoint(
        checkpoint_id=uuid.uuid4().hex,
        verified_at=datetime.now(UTC),
        scope=tuple(sorted(set(scope))),
        evidence=evidence,
    )
    payload = {
        "checkpoint_id": checkpoint.checkpoint_id,
        "verified_at": checkpoint.verified_at.isoformat(),
        "scope": list(checkpoint.scope),
        "evidence": checkpoint.evidence,
    }
    path = root.resolve() / ".homelab-state" / "checkpoints" / "latest.json"
    write_sensitive_file(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return checkpoint


def require_verified_checkpoint(root: Path, scope: Iterable[str], max_age: timedelta) -> RecoveryCheckpoint:
    """Load a matching, fresh restore checkpoint or reject destructive work."""
    required_scope = tuple(sorted(set(scope)))
    path = root.resolve() / ".homelab-state" / "checkpoints" / "latest.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        checkpoint = RecoveryCheckpoint(
            checkpoint_id=str(payload["checkpoint_id"]),
            verified_at=datetime.fromisoformat(str(payload["verified_at"])).astimezone(UTC),
            scope=tuple(sorted(str(item) for item in payload["scope"])),
            evidence={str(name): str(digest) for name, digest in payload["evidence"].items()},
        )
    except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RecoveryCheckpointRequiredError("No verified recovery checkpoint is available") from exc

    if not set(required_scope).issubset(checkpoint.scope):
        raise RecoveryCheckpointRequiredError("Recovery checkpoint does not cover the requested destruction scope")
    now = datetime.now(UTC)
    if checkpoint.verified_at > now + timedelta(minutes=5):
        raise RecoveryCheckpointRequiredError("Recovery checkpoint timestamp is in the future")
    if now - checkpoint.verified_at > max_age:
        raise RecoveryCheckpointRequiredError("Recovery checkpoint is too old for destructive work")
    if not checkpoint.evidence or any(
        len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest)
        for digest in checkpoint.evidence.values()
    ):
        raise RecoveryCheckpointRequiredError("Recovery checkpoint evidence is invalid")
    for name, expected_digest in checkpoint.evidence.items():
        evidence_path = Path(name)
        try:
            actual_digest = sha256(evidence_path.read_bytes()).hexdigest()
        except OSError as exc:
            raise RecoveryCheckpointRequiredError(f"Recovery checkpoint evidence is missing: {evidence_path}") from exc
        if actual_digest != expected_digest:
            raise RecoveryCheckpointRequiredError(
                f"Recovery checkpoint evidence changed after verification: {evidence_path}"
            )
    return checkpoint


def assert_destroyed(observed_guests: Iterable[str], expected_guests: Iterable[str]) -> None:
    """Reject state cleanup while any targeted managed guest is still observed."""
    remaining = sorted(set(observed_guests) & set(expected_guests))
    if remaining:
        raise ResourcesStillPresentError(f"Target managed guests still exist after destroy: {', '.join(remaining)}")


def write_sensitive_file(path: Path, content: str) -> None:
    """Atomically write sensitive content with owner-only permissions."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)
