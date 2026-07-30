"""Durable operational approvals with explicit execution outcomes."""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import secrets
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from toolkit.core.state.files import atomic_write_json


class ApprovalPersistenceError(RuntimeError):
    pass


_APPROVAL_ID = re.compile(r"^[a-f0-9]{32}$")
_SERVICE_NAME = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_MAX_PAYLOAD_BYTES = 32 * 1024


def _bounded_text(value: object, *, name: str, maximum: int, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not allow_empty and not value):
        raise ValueError(f"approval {name} is invalid")
    if any(ord(char) < 32 for char in value):
        raise ValueError(f"approval {name} contains control characters")
    return value


def _bounded_object(value: object, *, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"approval {name} is invalid")
    try:
        encoded = json.dumps(value, allow_nan=False, separators=(",", ":")).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError(f"approval {name} is not valid JSON") from exc
    if len(encoded) > _MAX_PAYLOAD_BYTES:
        raise ValueError(f"approval {name} exceeds its size limit")
    return value


class ApprovalKind(StrEnum):
    RIGHTSIZE = "rightsize"


class ApprovalStatus(StrEnum):
    REQUESTED = "requested"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXECUTED = "executed"


@dataclass
class Approval:
    """One approval entry."""

    kind: ApprovalKind
    service: str
    current: str
    proposed: str
    id: str = field(default_factory=lambda: secrets.token_hex(16))
    status: ApprovalStatus = ApprovalStatus.REQUESTED
    reason: str = ""
    requested_at: float = field(default_factory=time.time)
    requested_by: str = "system"
    decided_at: float | None = None
    decided_by: str = ""
    decision_reason: str = ""
    outcome: dict[str, Any] | None = None  # {success: bool, detail: str}
    payload: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _APPROVAL_ID.fullmatch(self.id):
            raise ValueError("approval id is invalid")
        if not isinstance(self.service, str) or not _SERVICE_NAME.fullmatch(self.service):
            raise ValueError("approval service is invalid")
        self.current = _bounded_text(self.current, name="current value", maximum=256)
        self.proposed = _bounded_text(self.proposed, name="proposed value", maximum=256)
        self.reason = _bounded_text(self.reason, name="reason", maximum=500)
        self.requested_by = _bounded_text(self.requested_by, name="requester", maximum=128)
        self.decided_by = _bounded_text(self.decided_by, name="decision actor", maximum=128)
        self.decision_reason = _bounded_text(self.decision_reason, name="decision reason", maximum=500)
        if not isinstance(self.requested_at, int | float) or not math.isfinite(self.requested_at):
            raise ValueError("approval request timestamp is invalid")
        if self.requested_at <= 0:
            raise ValueError("approval request timestamp is invalid")
        if self.decided_at is not None and (
            not isinstance(self.decided_at, int | float) or not math.isfinite(self.decided_at) or self.decided_at <= 0
        ):
            raise ValueError("approval decision timestamp is invalid")
        self.payload = _bounded_object(self.payload, name="payload")
        if self.outcome is not None:
            self.outcome = _bounded_object(self.outcome, name="outcome")

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "kind": str(self.kind.value),
            "service": self.service,
            "current": self.current,
            "proposed": self.proposed,
            "status": str(self.status.value),
            "reason": self.reason,
            "requested_at": self.requested_at,
            "requested_by": self.requested_by,
            "decided_at": self.decided_at,
            "decided_by": self.decided_by,
            "decision_reason": self.decision_reason,
            "outcome": self.outcome,
            "payload": self.payload,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Approval:
        payload = d.get("payload", {})
        if not isinstance(payload, dict):
            raise ValueError("approval payload is invalid")
        return cls(
            id=d["id"],
            kind=ApprovalKind(d["kind"]),
            service=d.get("service", ""),
            current=d.get("current", ""),
            proposed=d.get("proposed", ""),
            status=ApprovalStatus(d.get("status", "requested")),
            reason=d.get("reason", ""),
            requested_at=float(d.get("requested_at", 0)),
            requested_by=d.get("requested_by", "system"),
            decided_at=d.get("decided_at"),
            decided_by=d.get("decided_by", ""),
            decision_reason=d.get("decision_reason", ""),
            outcome=d.get("outcome"),
            payload=payload,
        )


class ApprovalStore:
    """JSON-persisted approval queue with the full lifecycle."""

    DEFAULT_RETENTION_DAYS = 30
    MAX_QUEUE_BYTES = 1024 * 1024
    MAX_ENTRIES = 2048

    def __init__(self, *, root: Path | str = ".", retention_days: int = DEFAULT_RETENTION_DAYS):
        self.root = Path(root)
        self.queue_path = self.root / ".homelab-state" / "approvals.json"
        self.retention_days = retention_days
        self._entries: list[Approval] = self._load()

    # --- persistence --------------------------------------------------------

    def _load(self) -> list[Approval]:
        try:
            descriptor = os.open(self.queue_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            return []
        except OSError as exc:
            raise ApprovalPersistenceError("approval queue could not be opened safely") from exc
        try:
            content = os.read(descriptor, self.MAX_QUEUE_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(content) > self.MAX_QUEUE_BYTES:
            raise ApprovalPersistenceError("approval queue exceeds its size limit")
        try:
            document = json.loads(content)
            data = document["entries"]
            if not isinstance(data, list) or len(data) > self.MAX_ENTRIES:
                raise TypeError
        except (KeyError, TypeError, UnicodeError, json.JSONDecodeError) as exc:
            raise ApprovalPersistenceError("approval queue is unreadable; refusing approval operations") from exc
        cutoff = time.time() - (self.retention_days * 86400)
        entries: list[Approval] = []
        for d in data:
            try:
                a = Approval.from_dict(d)
            except (KeyError, TypeError, ValueError) as exc:
                raise ApprovalPersistenceError("approval queue contains an invalid entry") from exc
            # Prune terminal entries (EXECUTED/REJECTED) older than retention.
            if a.status in (ApprovalStatus.EXECUTED, ApprovalStatus.REJECTED):
                terminal_ts = a.decided_at or a.requested_at
                if terminal_ts < cutoff:
                    continue
            entries.append(a)
        return entries

    def _save(self) -> None:
        try:
            self.queue_path.parent.mkdir(parents=True, exist_ok=True)
            self.queue_path.parent.chmod(0o700)
            payload = [Approval.from_dict(a.to_dict()).to_dict() for a in self._entries]
            if len(payload) > self.MAX_ENTRIES:
                raise ApprovalPersistenceError("approval queue exceeds its entry limit")
            encoded = json.dumps({"entries": payload}, allow_nan=False, separators=(",", ":")).encode("utf-8")
            if len(encoded) > self.MAX_QUEUE_BYTES:
                raise ApprovalPersistenceError("approval queue exceeds its size limit")
            atomic_write_json(self.queue_path, {"entries": payload}, mode=0o600)
        except ApprovalPersistenceError:
            raise
        except (OSError, TypeError, ValueError, UnicodeError) as exc:
            raise ApprovalPersistenceError("approval queue could not be persisted") from exc

    @contextmanager
    def _mutation(self) -> Iterator[None]:
        self.queue_path.parent.mkdir(parents=True, exist_ok=True)
        self.queue_path.parent.chmod(0o700)
        descriptor = os.open(
            self.queue_path.parent / "approvals.lock",
            os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                self._entries = self._load()
                yield
                self._save()
            finally:
                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)

    # --- lifecycle ----------------------------------------------------------

    def enqueue(
        self,
        kind: ApprovalKind,
        service: str,
        current: str,
        proposed: str,
        *,
        reason: str = "",
        requested_by: str = "system",
        payload: dict[str, Any] | None = None,
    ) -> Approval:
        """Enqueue a new approval request. Returns the created Approval (with id)."""
        with self._mutation():
            a = Approval(
                kind=kind,
                service=service,
                current=current,
                proposed=proposed,
                reason=reason,
                requested_by=requested_by,
                payload=dict(payload or {}),
            )
            self._entries.append(a)
        return a

    def approve(self, approval_id: str, *, decided_by: str = "") -> Approval | None:
        with self._mutation():
            for a in self._entries:
                if a.id == approval_id and a.status is ApprovalStatus.REQUESTED:
                    a.status = ApprovalStatus.APPROVED
                    a.decided_at = time.time()
                    a.decided_by = decided_by
                    return a
        return None

    def reject(self, approval_id: str, *, decided_by: str = "", reason: str = "") -> Approval | None:
        with self._mutation():
            for a in self._entries:
                if a.id == approval_id and a.status is ApprovalStatus.REQUESTED:
                    a.status = ApprovalStatus.REJECTED
                    a.decided_at = time.time()
                    a.decided_by = decided_by
                    a.decision_reason = reason
                    return a
        return None

    def record_outcome(self, approval_id: str, *, success: bool, detail: str = "") -> Approval | None:
        """Record the execution outcome (success or auto-rollback). Marks EXECUTED."""
        with self._mutation():
            for a in self._entries:
                if a.id == approval_id and a.status is ApprovalStatus.APPROVED:
                    a.status = ApprovalStatus.EXECUTED
                    a.outcome = {"success": success, "detail": detail}
                    return a
        return None

    # --- queries ------------------------------------------------------------

    def all(self) -> list[Approval]:
        return list(self._entries)

    def pending(self, *, kind: ApprovalKind | None = None) -> list[Approval]:
        return [a for a in self._entries if a.status is ApprovalStatus.REQUESTED and (kind is None or a.kind is kind)]

    def actionable(self) -> list[Approval]:
        return [
            approval
            for approval in self._entries
            if approval.status in (ApprovalStatus.REQUESTED, ApprovalStatus.APPROVED)
        ]

    def approved(self, *, kind: ApprovalKind | None = None) -> list[Approval]:
        return [a for a in self._entries if a.status is ApprovalStatus.APPROVED and (kind is None or a.kind is kind)]

    def rejected(self, *, kind: ApprovalKind | None = None) -> list[Approval]:
        return [a for a in self._entries if a.status is ApprovalStatus.REJECTED and (kind is None or a.kind is kind)]

    def executed(self, *, kind: ApprovalKind | None = None) -> list[Approval]:
        return [a for a in self._entries if a.status is ApprovalStatus.EXECUTED and (kind is None or a.kind is kind)]

    def find(self, approval_id: str) -> Approval | None:
        for a in self._entries:
            if a.id == approval_id:
                return a
        return None


__all__ = [
    "Approval",
    "ApprovalKind",
    "ApprovalPersistenceError",
    "ApprovalStatus",
    "ApprovalStore",
]
