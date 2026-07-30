"""Transactional SQLite state for controller plans, jobs, events, and audit."""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import stat
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Literal, cast

from toolkit.controller.contracts import (
    ApprovalGrant,
    AuditRecord,
    DestroyInfraOperation,
    DestroyPlanSpec,
    ErrorBody,
    EventLevel,
    IdentityOperation,
    InviteUserCommand,
    JobEvent,
    JobKind,
    JobRecord,
    JobRequest,
    JobState,
    PlanRecord,
    SealedInviteUserCommand,
    job_can_cancel,
)
from toolkit.controller.payload_protection import (
    PayloadProtectionError,
    load_or_create_payload_key,
    open_invite_command,
    seal_invite_command,
)
from toolkit.controller.read_models import BootstrapCapabilityIssue, BootstrapSessionGrant
from toolkit.controller.sanitization import (
    MAX_EVENT_PAYLOAD_BYTES,
    MAX_RESULT_BYTES,
    sanitize_message,
    sanitize_object,
)


class ControllerStoreError(RuntimeError):
    """Base error for durable controller state."""


class JobNotFoundError(ControllerStoreError):
    pass


class JobConflictError(ControllerStoreError):
    pass


class JobQueueLimitError(JobConflictError):
    pass


class IdempotencyConflictError(JobConflictError):
    pass


class PlanNotFoundError(ControllerStoreError):
    pass


class ApprovalError(ControllerStoreError):
    pass


class BootstrapCapabilityError(ControllerStoreError):
    """A bootstrap capability or session grant is invalid or unavailable."""


_TRANSITIONS: dict[JobState, frozenset[JobState]] = {
    JobState.QUEUED: frozenset({JobState.RUNNING, JobState.CANCELLED}),
    JobState.RUNNING: frozenset(
        {JobState.CANCEL_REQUESTED, JobState.SUCCEEDED, JobState.PARTIAL_FAILURE, JobState.FAILED}
    ),
    JobState.CANCEL_REQUESTED: frozenset({JobState.CANCELLED, JobState.FAILED}),
    JobState.SUCCEEDED: frozenset(),
    JobState.PARTIAL_FAILURE: frozenset(),
    JobState.FAILED: frozenset(),
    JobState.CANCELLED: frozenset(),
}

_MAX_BOOTSTRAP_ATTEMPTS = 5
_MAX_BOOTSTRAP_TTL = timedelta(minutes=15)


def _canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _parse_time(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


class ControllerStore:
    def __init__(self, path: Path, *, clock: Callable[[], datetime] | None = None):
        self.path = path
        self._clock = clock or (lambda: datetime.now(UTC))
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path.parent.chmod(0o700)
        self.payload_key_path, self._payload_key = load_or_create_payload_key(self.path)
        self._initialize()

    def _payload_hash(self, request_json: str) -> str:
        return hmac.new(self._payload_key, request_json.encode("utf-8"), hashlib.sha256).hexdigest()

    def _protect_request(self, request: JobRequest, principal: str) -> JobRequest:
        operation = request.operation
        if not isinstance(operation, IdentityOperation):
            return request
        command = operation.command
        if isinstance(command, SealedInviteUserCommand):
            raise PayloadProtectionError("sealed invite commands are internal controller records")
        if not isinstance(command, InviteUserCommand):
            return request
        sealed = seal_invite_command(
            command,
            key=self._payload_key,
            principal=principal,
            idempotency_key=request.idempotency_key,
        )
        return request.model_copy(update={"operation": operation.model_copy(update={"command": sealed})})

    def open_invite_command(
        self,
        command: SealedInviteUserCommand,
        *,
        principal: str,
        idempotency_key: str,
    ) -> InviteUserCommand:
        return open_invite_command(
            command,
            key=self._payload_key,
            principal=principal,
            idempotency_key=idempotency_key,
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=10, isolation_level=None)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        return connection

    def _initialize(self) -> None:
        with self._read() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS plans (
                    plan_id TEXT PRIMARY KEY,
                    actor TEXT NOT NULL,
                    spec_json TEXT NOT NULL,
                    plan_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS approvals (
                    approval_id TEXT PRIMARY KEY,
                    plan_id TEXT NOT NULL REFERENCES plans(plan_id),
                    actor TEXT NOT NULL,
                    token_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT
                );
                CREATE TABLE IF NOT EXISTS bootstrap_capabilities (
                    capability_id TEXT PRIMARY KEY,
                    issuer_principal TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    exchanged_at TEXT,
                    revoked_at TEXT,
                    failed_attempts INTEGER NOT NULL DEFAULT 0
                        CHECK (failed_attempts >= 0 AND failed_attempts <= 5)
                );
                CREATE TABLE IF NOT EXISTS bootstrap_sessions (
                    session_id TEXT PRIMARY KEY,
                    capability_id TEXT NOT NULL UNIQUE
                        REFERENCES bootstrap_capabilities(capability_id),
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL,
                    expires_at TEXT NOT NULL,
                    consumed_at TEXT,
                    revoked_at TEXT,
                    failed_attempts INTEGER NOT NULL DEFAULT 0
                        CHECK (failed_attempts >= 0 AND failed_attempts <= 5)
                );
                CREATE INDEX IF NOT EXISTS bootstrap_capabilities_active
                    ON bootstrap_capabilities(expires_at, exchanged_at, revoked_at);
                CREATE INDEX IF NOT EXISTS bootstrap_sessions_active
                    ON bootstrap_sessions(expires_at, consumed_at, revoked_at);
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    principal TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL,
                    payload_hash TEXT NOT NULL,
                    request_json TEXT NOT NULL,
                    state TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    cancel_requested INTEGER NOT NULL DEFAULT 0,
                    result_json TEXT,
                    error_json TEXT,
                    lease_owner TEXT,
                    lease_generation INTEGER NOT NULL DEFAULT 0 CHECK (lease_generation >= 0),
                    lease_expires_at TEXT,
                    UNIQUE(principal, idempotency_key)
                );
                CREATE TABLE IF NOT EXISTS job_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(job_id) ON DELETE CASCADE,
                    timestamp TEXT NOT NULL,
                    level TEXT NOT NULL,
                    message TEXT NOT NULL,
                    payload_json TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS job_events_job_sequence
                    ON job_events(job_id, sequence);
                CREATE TABLE IF NOT EXISTS audit_records (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    principal TEXT NOT NULL,
                    action TEXT NOT NULL,
                    resource TEXT NOT NULL,
                    outcome TEXT NOT NULL,
                    details_json TEXT NOT NULL
                );
                """
            )
        self._secure_artifacts()

    def _secure_artifacts(self) -> None:
        for artifact in self.path.parent.glob(f"{self.path.name}*"):
            try:
                mode = artifact.lstat().st_mode
            except FileNotFoundError:
                continue
            if stat.S_ISREG(mode):
                try:
                    os.chmod(artifact, 0o600, follow_symlinks=False)
                except FileNotFoundError:
                    # SQLite can checkpoint and remove its WAL between lstat and chmod.
                    continue

    @contextmanager
    def _read(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            yield connection
        finally:
            connection.close()
            self._secure_artifacts()

    @contextmanager
    def _write(self) -> Iterator[sqlite3.Connection]:
        connection = self._connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()
            self._secure_artifacts()

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise ValueError("controller clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def journal_mode(self) -> str:
        with self._read() as connection:
            row = connection.execute("PRAGMA journal_mode").fetchone()
        return str(row[0]).lower()

    def active_job_counts(self) -> tuple[int, int]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT state, COUNT(*) AS total FROM jobs WHERE state IN (?, ?, ?) GROUP BY state",
                (JobState.QUEUED.value, JobState.RUNNING.value, JobState.CANCEL_REQUESTED.value),
            ).fetchall()
        counts = {str(row["state"]): int(row["total"]) for row in rows}
        queued = counts.get(JobState.QUEUED.value, 0)
        running = counts.get(JobState.RUNNING.value, 0) + counts.get(JobState.CANCEL_REQUESTED.value, 0)
        return queued, running

    def active_jobs(
        self,
        *,
        principal: str | None,
        kinds: frozenset[JobKind],
        limit: int = 10,
    ) -> list[JobRecord]:
        if principal is not None:
            self._require_principal(principal)
        if not kinds or limit < 1 or limit > 50:
            raise ValueError("active job query is invalid")
        principal_clause = "principal = ? AND " if principal is not None else ""
        parameters: tuple[str, ...] = (
            *((principal,) if principal is not None else ()),
            JobState.QUEUED.value,
            JobState.RUNNING.value,
            JobState.CANCEL_REQUESTED.value,
        )
        with self._read() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE {principal_clause}state IN (?, ?, ?)
                ORDER BY created_at DESC
                LIMIT 100
                """,
                parameters,
            ).fetchall()
        jobs = (self._job_from_row(row) for row in rows)
        return [job for job in jobs if job.request.kind in kinds][:limit]

    def recent_jobs(self, *, principal: str | None, limit: int = 100) -> list[JobRecord]:
        if principal is not None:
            self._require_principal(principal)
        if limit < 1 or limit > 200:
            raise ValueError("recent job limit must be between 1 and 200")
        principal_clause = "WHERE principal = ?" if principal is not None else ""
        parameters = (principal,) if principal is not None else ()
        with self._read() as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM jobs
                {principal_clause}
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (*parameters, limit),
            ).fetchall()
        return [self._job_from_row(row) for row in rows]

    def issue_bootstrap_capability(self, *, principal: str, ttl: timedelta) -> BootstrapCapabilityIssue:
        """Issue the sole active bootstrap capability without persisting its plaintext."""
        self._require_bootstrap_ttl(ttl)
        self._require_principal(principal)
        now = self._now()
        expires_at = now + ttl
        capability_id, token = self._new_opaque_token()
        with self._write() as connection:
            connection.execute(
                """
                UPDATE bootstrap_capabilities
                SET revoked_at = ?
                WHERE exchanged_at IS NULL AND revoked_at IS NULL AND expires_at > ?
                """,
                (now.isoformat(), now.isoformat()),
            )
            connection.execute(
                """
                UPDATE bootstrap_sessions
                SET revoked_at = ?
                WHERE consumed_at IS NULL AND revoked_at IS NULL AND expires_at > ?
                """,
                (now.isoformat(), now.isoformat()),
            )
            connection.execute(
                """
                INSERT INTO bootstrap_capabilities(
                    capability_id, issuer_principal, token_hash, created_at, expires_at,
                    exchanged_at, revoked_at, failed_attempts
                ) VALUES (?, ?, ?, ?, ?, NULL, NULL, 0)
                """,
                (
                    capability_id,
                    principal,
                    _sha256(token),
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            self._append_audit_conn(
                connection,
                now,
                principal,
                "BOOTSTRAP_CAPABILITY_ISSUE",
                f"bootstrap-capability:{capability_id}",
                "ALLOWED",
                {"expires_at": expires_at.isoformat()},
            )
        return BootstrapCapabilityIssue(token=token, expires_at=expires_at)

    def exchange_bootstrap_capability(self, token: str, *, ttl: timedelta) -> BootstrapSessionGrant:
        """Atomically exchange a capability for exactly one opaque session grant."""
        self._require_bootstrap_ttl(ttl)
        now = self._now()
        capability_id = self._opaque_token_id(token)
        grant: BootstrapSessionGrant | None = None
        with self._write() as connection:
            row = (
                connection.execute(
                    "SELECT * FROM bootstrap_capabilities WHERE capability_id = ?",
                    (capability_id,),
                ).fetchone()
                if capability_id is not None
                else None
            )
            if row is not None and not hmac.compare_digest(str(row["token_hash"]), _sha256(token)):
                self._record_bootstrap_failure(
                    connection,
                    "bootstrap_capabilities",
                    "capability_id",
                    cast(str, capability_id),
                    now,
                )
                row = None
            if row is not None and self._capability_is_active(row, now):
                session_id, session_token = self._new_opaque_token()
                capability_expiry = cast(datetime, _parse_time(str(row["expires_at"])))
                expires_at = min(now + ttl, capability_expiry)
                connection.execute(
                    "UPDATE bootstrap_capabilities SET exchanged_at = ? WHERE capability_id = ?",
                    (now.isoformat(), capability_id),
                )
                connection.execute(
                    """
                    INSERT INTO bootstrap_sessions(
                        session_id, capability_id, token_hash, created_at, expires_at,
                        consumed_at, revoked_at, failed_attempts
                    ) VALUES (?, ?, ?, ?, ?, NULL, NULL, 0)
                    """,
                    (
                        session_id,
                        capability_id,
                        _sha256(session_token),
                        now.isoformat(),
                        expires_at.isoformat(),
                    ),
                )
                self._append_audit_conn(
                    connection,
                    now,
                    str(row["issuer_principal"]),
                    "BOOTSTRAP_CAPABILITY_EXCHANGE",
                    f"bootstrap-session:{session_id}",
                    "ALLOWED",
                    {"expires_at": expires_at.isoformat()},
                )
                grant = BootstrapSessionGrant(session_token=session_token, expires_at=expires_at)
        if grant is None:
            raise BootstrapCapabilityError("bootstrap capability is invalid")
        return grant

    def validate_bootstrap_grant(self, session_token: str) -> BootstrapSessionGrant:
        """Validate a bootstrap grant and account for failed secret guesses."""
        now = self._now()
        session_id = self._opaque_token_id(session_token)
        row: sqlite3.Row | None = None
        with self._write() as connection:
            row = self._authenticate_bootstrap_session(connection, session_id, session_token, now)
        if row is None:
            raise BootstrapCapabilityError("bootstrap session grant is invalid")
        return BootstrapSessionGrant(
            session_token=session_token,
            expires_at=cast(datetime, _parse_time(str(row["expires_at"]))),
        )

    def consume_bootstrap_grant(self, session_token: str, *, principal: str) -> None:
        """Consume a valid grant after bootstrap state was initialized successfully."""
        self._require_principal(principal)
        now = self._now()
        session_id = self._opaque_token_id(session_token)
        consumed = False
        with self._write() as connection:
            row = self._authenticate_bootstrap_session(connection, session_id, session_token, now)
            if row is not None:
                updated = connection.execute(
                    """
                    UPDATE bootstrap_sessions SET consumed_at = ?
                    WHERE session_id = ? AND consumed_at IS NULL AND revoked_at IS NULL
                    """,
                    (now.isoformat(), session_id),
                )
                consumed = updated.rowcount == 1
                if consumed:
                    self._append_audit_conn(
                        connection,
                        now,
                        principal,
                        "BOOTSTRAP_SESSION_CONSUME",
                        f"bootstrap-session:{session_id}",
                        "ALLOWED",
                        {},
                    )
        if not consumed:
            raise BootstrapCapabilityError("bootstrap session grant is invalid")

    def bootstrap_access_state(self) -> tuple[bool, bool]:
        """Return active capability/session flags without conflating filesystem readiness."""
        now = self._now().isoformat()
        with self._read() as connection:
            capability = connection.execute(
                """
                SELECT 1 FROM bootstrap_capabilities
                WHERE exchanged_at IS NULL AND revoked_at IS NULL AND expires_at > ?
                LIMIT 1
                """,
                (now,),
            ).fetchone()
            session = connection.execute(
                """
                SELECT 1 FROM bootstrap_sessions
                WHERE consumed_at IS NULL AND revoked_at IS NULL AND expires_at > ?
                LIMIT 1
                """,
                (now,),
            ).fetchone()
        return capability is not None, session is not None

    @staticmethod
    def _require_bootstrap_ttl(ttl: timedelta) -> None:
        if ttl <= timedelta(0) or ttl > _MAX_BOOTSTRAP_TTL:
            raise BootstrapCapabilityError("bootstrap credential lifetime must be between zero and fifteen minutes")

    @staticmethod
    def _require_principal(principal: str) -> None:
        if not principal or len(principal) > 200 or any(ord(character) < 32 for character in principal):
            raise BootstrapCapabilityError("bootstrap principal is invalid")

    @staticmethod
    def _new_opaque_token() -> tuple[str, str]:
        token_id = str(uuid.uuid4())
        return token_id, f"{token_id}.{secrets.token_urlsafe(32)}"

    @staticmethod
    def _opaque_token_id(token: str) -> str | None:
        if not isinstance(token, str) or len(token) > 512:
            return None
        token_id, separator, secret = token.partition(".")
        if separator != "." or not secret or "." in secret:
            return None
        try:
            parsed = uuid.UUID(token_id)
        except (ValueError, AttributeError):
            return None
        return token_id if str(parsed) == token_id else None

    @staticmethod
    def _record_bootstrap_failure(
        connection: sqlite3.Connection,
        table: Literal["bootstrap_capabilities", "bootstrap_sessions"],
        id_column: Literal["capability_id", "session_id"],
        record_id: str,
        now: datetime,
    ) -> None:
        # Table and column are closed internal literals; all external values stay parameterized.
        connection.execute(
            f"""
            UPDATE {table}
            SET failed_attempts = MIN(failed_attempts + 1, ?),
                revoked_at = CASE WHEN failed_attempts + 1 >= ? THEN ? ELSE revoked_at END
            WHERE {id_column} = ? AND revoked_at IS NULL
            """,
            (_MAX_BOOTSTRAP_ATTEMPTS, _MAX_BOOTSTRAP_ATTEMPTS, now.isoformat(), record_id),
        )

    @staticmethod
    def _capability_is_active(row: sqlite3.Row, now: datetime) -> bool:
        expires_at = _parse_time(str(row["expires_at"]))
        return (
            row["exchanged_at"] is None
            and row["revoked_at"] is None
            and int(row["failed_attempts"]) < _MAX_BOOTSTRAP_ATTEMPTS
            and expires_at is not None
            and expires_at > now
        )

    @classmethod
    def _authenticate_bootstrap_session(
        cls,
        connection: sqlite3.Connection,
        session_id: str | None,
        session_token: str,
        now: datetime,
    ) -> sqlite3.Row | None:
        if session_id is None:
            return None
        row = connection.execute(
            "SELECT * FROM bootstrap_sessions WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        if not hmac.compare_digest(str(row["token_hash"]), _sha256(session_token)):
            cls._record_bootstrap_failure(connection, "bootstrap_sessions", "session_id", session_id, now)
            return None
        expires_at = _parse_time(str(row["expires_at"]))
        if (
            row["consumed_at"] is not None
            or row["revoked_at"] is not None
            or int(row["failed_attempts"]) >= _MAX_BOOTSTRAP_ATTEMPTS
            or expires_at is None
            or expires_at <= now
        ):
            return None
        return row

    def create_plan(self, spec: DestroyPlanSpec, *, actor: str) -> PlanRecord:
        now = self._now()
        plan_id = str(uuid.uuid4())
        spec_json = _canonical_json(spec.model_dump(mode="json"))
        plan_hash = _sha256(spec_json)
        with self._write() as connection:
            connection.execute(
                "INSERT INTO plans(plan_id, actor, spec_json, plan_hash, created_at) VALUES (?, ?, ?, ?, ?)",
                (plan_id, actor, spec_json, plan_hash, now.isoformat()),
            )
            self._append_audit_conn(
                connection,
                now,
                actor,
                "PLAN_CREATE",
                f"plan:{plan_id}",
                "ALLOWED",
                {"scopes": spec.scopes, "plan_hash": plan_hash},
            )
        return PlanRecord(
            plan_id=plan_id,
            actor=actor,
            spec=spec,
            plan_hash=plan_hash,
            created_at=now,
        )

    def get_plan(self, plan_id: str) -> PlanRecord:
        with self._read() as connection:
            row = connection.execute("SELECT * FROM plans WHERE plan_id = ?", (plan_id,)).fetchone()
        if row is None:
            raise PlanNotFoundError("plan not found")
        return self._plan_from_row(row)

    def issue_approval(self, plan_id: str, *, actor: str, ttl: timedelta) -> ApprovalGrant:
        if ttl <= timedelta(0) or ttl > timedelta(minutes=15):
            raise ApprovalError("approval lifetime must be between zero and fifteen minutes")
        plan = self.get_plan(plan_id)
        if plan.actor != actor:
            raise ApprovalError("approval actor does not own the plan")
        now = self._now()
        expires_at = now + ttl
        token = secrets.token_urlsafe(32)
        with self._write() as connection:
            connection.execute(
                """
                INSERT INTO approvals(
                    approval_id, plan_id, actor, token_hash, created_at, expires_at, consumed_at
                ) VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (
                    str(uuid.uuid4()),
                    plan_id,
                    actor,
                    _sha256(token),
                    now.isoformat(),
                    expires_at.isoformat(),
                ),
            )
            self._append_audit_conn(
                connection,
                now,
                actor,
                "APPROVAL_ISSUE",
                f"plan:{plan_id}",
                "ALLOWED",
                {"expires_at": expires_at.isoformat()},
            )
        return ApprovalGrant(plan_id=plan_id, actor=actor, token=token, expires_at=expires_at)

    def create_job(self, request: JobRequest, *, principal: str) -> JobRecord:
        job, _created = self.submit_job(request, principal=principal)
        return job

    def submit_job(
        self,
        request: JobRequest,
        *,
        principal: str,
        active_limit: int | None = None,
        active_kinds: frozenset[JobKind] | None = None,
    ) -> tuple[JobRecord, bool]:
        if active_limit is not None and active_limit < 1:
            raise ValueError("active job limit must be positive")
        if active_kinds is not None and (not active_kinds or request.kind not in active_kinds):
            raise ValueError("active job kind scope is invalid")
        now = self._now()
        request_json = _canonical_json(request.model_dump(mode="json"))
        payload_hash = self._payload_hash(request_json)
        with self._write() as connection:
            existing = connection.execute(
                "SELECT * FROM jobs WHERE principal = ? AND idempotency_key = ?",
                (principal, request.idempotency_key),
            ).fetchone()
            if existing is not None:
                if not hmac.compare_digest(str(existing["payload_hash"]), payload_hash):
                    raise IdempotencyConflictError("idempotency key was already used for another request")
                return self._job_from_row(existing), False

            if active_limit is not None:
                active_rows = connection.execute(
                    "SELECT request_json FROM jobs WHERE state IN (?, ?, ?)",
                    (JobState.QUEUED.value, JobState.RUNNING.value, JobState.CANCEL_REQUESTED.value),
                ).fetchall()
                limited_kinds = active_kinds or frozenset({request.kind})
                active_same_kind = sum(
                    JobRequest.model_validate_json(str(row["request_json"])).kind in limited_kinds
                    for row in active_rows
                )
                if active_same_kind >= active_limit:
                    raise JobQueueLimitError("active job limit reached")

            if isinstance(request.operation, DestroyInfraOperation):
                self._consume_destructive_approval(connection, request.operation, principal, now)
                persisted = request.model_copy(
                    update={"operation": request.operation.model_copy(update={"approval_token": "consumed-approval"})}
                )
                persisted_json = _canonical_json(persisted.model_dump(mode="json"))
            else:
                persisted = self._protect_request(request, principal)
                persisted_json = _canonical_json(persisted.model_dump(mode="json"))

            job_id = str(uuid.uuid4())
            connection.execute(
                """
                INSERT INTO jobs(
                    job_id, principal, idempotency_key, payload_hash, request_json, state,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id,
                    principal,
                    request.idempotency_key,
                    payload_hash,
                    persisted_json,
                    JobState.QUEUED.value,
                    now.isoformat(),
                    now.isoformat(),
                ),
            )
            self._append_audit_conn(
                connection,
                now,
                principal,
                "JOB_CREATE",
                f"job:{job_id}",
                "ALLOWED",
                {"kind": request.kind.value, "payload_hash": payload_hash},
            )
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._job_from_row(row), True

    def submit_job_batch_limited(
        self,
        requests: list[JobRequest],
        *,
        principal: str,
        active_limit: int,
    ) -> list[tuple[JobRecord, bool]]:
        """Atomically replay or enqueue a same-kind batch under one active-job cap."""
        if not requests or len(requests) > active_limit or active_limit < 1:
            raise ValueError("limited job batch size is invalid")
        if len({request.idempotency_key for request in requests}) != len(requests):
            raise ValueError("limited job batch idempotency keys must be unique")
        kinds = {request.kind for request in requests}
        if len(kinds) != 1 or any(isinstance(request.operation, DestroyInfraOperation) for request in requests):
            raise ValueError("limited job batches must contain one non-destructive operation kind")

        now = self._now()
        prepared = [
            (
                request,
                _canonical_json(request.model_dump(mode="json")),
            )
            for request in requests
        ]
        results: dict[str, tuple[JobRecord, bool]] = {}
        pending: list[tuple[JobRequest, str, str]] = []
        with self._write() as connection:
            for request, request_json in prepared:
                payload_hash = self._payload_hash(request_json)
                existing = connection.execute(
                    "SELECT * FROM jobs WHERE principal = ? AND idempotency_key = ?",
                    (principal, request.idempotency_key),
                ).fetchone()
                if existing is not None:
                    if not hmac.compare_digest(str(existing["payload_hash"]), payload_hash):
                        raise IdempotencyConflictError("idempotency key was already used for another request")
                    results[request.idempotency_key] = (self._job_from_row(existing), False)
                else:
                    persisted = self._protect_request(request, principal)
                    pending.append(
                        (
                            request,
                            _canonical_json(persisted.model_dump(mode="json")),
                            payload_hash,
                        )
                    )

            active_rows = connection.execute(
                "SELECT request_json FROM jobs WHERE state IN (?, ?, ?)",
                (JobState.QUEUED.value, JobState.RUNNING.value, JobState.CANCEL_REQUESTED.value),
            ).fetchall()
            kind = requests[0].kind
            active_same_kind = sum(
                JobRequest.model_validate_json(str(row["request_json"])).kind is kind for row in active_rows
            )
            if active_same_kind + len(pending) > active_limit:
                raise JobQueueLimitError("active job limit reached")

            for request, request_json, payload_hash in pending:
                job_id = str(uuid.uuid4())
                connection.execute(
                    """
                    INSERT INTO jobs(
                        job_id, principal, idempotency_key, payload_hash, request_json, state,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        principal,
                        request.idempotency_key,
                        payload_hash,
                        request_json,
                        JobState.QUEUED.value,
                        now.isoformat(),
                        now.isoformat(),
                    ),
                )
                self._append_audit_conn(
                    connection,
                    now,
                    principal,
                    "JOB_CREATE",
                    f"job:{job_id}",
                    "ALLOWED",
                    {"kind": request.kind.value, "payload_hash": payload_hash},
                )
                row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
                results[request.idempotency_key] = (self._job_from_row(row), True)

        return [results[request.idempotency_key] for request in requests]

    def _consume_destructive_approval(
        self,
        connection: sqlite3.Connection,
        operation: DestroyInfraOperation,
        principal: str,
        now: datetime,
    ) -> None:
        plan_row = connection.execute("SELECT * FROM plans WHERE plan_id = ?", (operation.plan_id,)).fetchone()
        if plan_row is None:
            raise ApprovalError("destructive plan is unknown")
        plan = self._plan_from_row(plan_row)
        if plan.actor != principal:
            raise ApprovalError("destructive plan belongs to another actor")
        if not hmac.compare_digest(plan.plan_hash, operation.plan_hash):
            raise ApprovalError("destructive plan hash does not match")
        if plan.spec.action != operation.action:
            raise ApprovalError("destructive action does not match the plan")
        if plan.spec.scopes != operation.scopes:
            raise ApprovalError("destructive scopes do not match the plan")
        if not hmac.compare_digest(plan.spec.config_revision, operation.config_revision):
            raise ApprovalError("configuration revision does not match the plan")

        token_hash = _sha256(operation.approval_token)
        approval = connection.execute(
            """
            SELECT * FROM approvals
            WHERE plan_id = ? AND actor = ? AND token_hash = ?
            ORDER BY created_at DESC LIMIT 1
            """,
            (operation.plan_id, principal, token_hash),
        ).fetchone()
        if approval is None or not hmac.compare_digest(str(approval["token_hash"]), token_hash):
            raise ApprovalError("approval is invalid")
        if approval["consumed_at"] is not None:
            raise ApprovalError("approval was already consumed")
        expires_at = cast(datetime, _parse_time(str(approval["expires_at"])))
        if expires_at <= now:
            raise ApprovalError("approval has expired")
        updated = connection.execute(
            "UPDATE approvals SET consumed_at = ? WHERE approval_id = ? AND consumed_at IS NULL",
            (now.isoformat(), approval["approval_id"]),
        )
        if updated.rowcount != 1:
            raise ApprovalError("approval was already consumed")

    def get_job(self, job_id: str) -> JobRecord:
        with self._read() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError("job not found")
        return self._job_from_row(row)

    def claim_job(self, job_id: str, *, worker_id: str, lease_seconds: int) -> JobRecord:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = self._now()
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._write() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFoundError("job not found")
            state = JobState(str(row["state"]))
            lease_expiry = _parse_time(row["lease_expires_at"])
            can_claim = state is JobState.QUEUED or (
                state is JobState.RUNNING and lease_expiry is not None and lease_expiry <= now
            )
            if not can_claim:
                raise JobConflictError("job is not claimable")
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, lease_owner = ?, lease_generation = lease_generation + 1,
                    lease_expires_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (JobState.RUNNING.value, worker_id, expires_at.isoformat(), now.isoformat(), job_id),
            )
            claimed = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            self._append_audit_conn(
                connection,
                now,
                worker_id,
                "JOB_CLAIM",
                f"job:{job_id}",
                "ALLOWED",
                {
                    "lease_expires_at": expires_at.isoformat(),
                    "lease_generation": int(claimed["lease_generation"]),
                },
            )
        return self._job_from_row(claimed)

    def claim_next(self, *, worker_id: str, lease_seconds: int) -> JobRecord | None:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = self._now()
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._write() as connection:
            abandoned = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state = ?
                  AND (lease_expires_at IS NULL OR lease_expires_at <= ?)
                ORDER BY created_at, rowid
                """,
                (JobState.CANCEL_REQUESTED.value, now.isoformat()),
            ).fetchall()
            for cancelled in abandoned:
                cancelled_job_id = str(cancelled["job_id"])
                connection.execute(
                    """
                    UPDATE jobs
                    SET state = ?, updated_at = ?, lease_owner = NULL, lease_expires_at = NULL
                    WHERE job_id = ?
                    """,
                    (JobState.CANCELLED.value, now.isoformat(), cancelled_job_id),
                )
                connection.execute(
                    """
                    INSERT INTO job_events(job_id, timestamp, level, message, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        cancelled_job_id,
                        now.isoformat(),
                        "WARNING",
                        "Cancellation finalized after worker lease expired",
                        "{}",
                    ),
                )
                self._append_audit_conn(
                    connection,
                    now,
                    worker_id,
                    "JOB_CANCEL_RECOVER",
                    f"job:{cancelled_job_id}",
                    "ALLOWED",
                    {"from": JobState.CANCEL_REQUESTED.value, "to": JobState.CANCELLED.value},
                )
            row = connection.execute(
                """
                SELECT * FROM jobs
                WHERE state = ?
                   OR (state = ? AND lease_expires_at IS NOT NULL AND lease_expires_at <= ?)
                ORDER BY created_at, rowid
                LIMIT 1
                """,
                (JobState.QUEUED.value, JobState.RUNNING.value, now.isoformat()),
            ).fetchone()
            if row is None:
                return None
            job_id = str(row["job_id"])
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, lease_owner = ?, lease_generation = lease_generation + 1,
                    lease_expires_at = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (JobState.RUNNING.value, worker_id, expires_at.isoformat(), now.isoformat(), job_id),
            )
            claimed = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            self._append_audit_conn(
                connection,
                now,
                worker_id,
                "JOB_CLAIM",
                f"job:{job_id}",
                "ALLOWED",
                {
                    "lease_expires_at": expires_at.isoformat(),
                    "lease_generation": int(claimed["lease_generation"]),
                },
            )
        return self._job_from_row(claimed)

    def renew_lease(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_generation: int,
        lease_seconds: int,
    ) -> JobRecord:
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        now = self._now()
        expires_at = now + timedelta(seconds=lease_seconds)
        with self._write() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFoundError("job not found")
            self._require_live_lease(row, worker_id, lease_generation, now)
            connection.execute(
                "UPDATE jobs SET lease_expires_at = ?, updated_at = ? WHERE job_id = ?",
                (expires_at.isoformat(), now.isoformat(), job_id),
            )
            renewed = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._job_from_row(renewed)

    def assert_lease(self, job_id: str, *, worker_id: str, lease_generation: int) -> JobRecord:
        now = self._now()
        with self._read() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError("job not found")
        self._require_live_lease(row, worker_id, lease_generation, now)
        return self._job_from_row(row)

    @staticmethod
    def _require_live_lease(
        row: sqlite3.Row,
        worker_id: str,
        lease_generation: int,
        now: datetime,
    ) -> None:
        state = JobState(str(row["state"]))
        lease_expiry = _parse_time(row["lease_expires_at"])
        valid = (
            state in {JobState.RUNNING, JobState.CANCEL_REQUESTED}
            and row["lease_owner"] == worker_id
            and int(row["lease_generation"]) == lease_generation
            and lease_expiry is not None
            and lease_expiry > now
        )
        if not valid:
            raise JobConflictError("job lease is not current")

    def transition(
        self,
        job_id: str,
        *,
        expected: JobState,
        target: JobState,
        result: dict[str, Any] | None = None,
        error: ErrorBody | None = None,
        event: tuple[EventLevel, str, dict[str, Any]] | None = None,
        worker_id: str | None = None,
        lease_generation: int | None = None,
    ) -> JobRecord:
        if target not in _TRANSITIONS[expected]:
            raise JobConflictError(f"transition {expected.value} to {target.value} is not allowed")
        now = self._now()
        terminal = target in {
            JobState.SUCCEEDED,
            JobState.PARTIAL_FAILURE,
            JobState.FAILED,
            JobState.CANCELLED,
        }
        safe_result = sanitize_object(result, max_bytes=MAX_RESULT_BYTES) if result is not None else None
        safe_error = None
        if error is not None:
            safe_error = ErrorBody(
                code=error.code,
                message=sanitize_message(error.message),
                details=sanitize_object(error.details, max_bytes=MAX_EVENT_PAYLOAD_BYTES),
            )
        prepared_event = self._prepare_event(job_id, now, *event) if event is not None else None
        with self._write() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFoundError("job not found")
            if JobState(str(row["state"])) is not expected:
                raise JobConflictError("job state changed before transition")
            if expected in {JobState.RUNNING, JobState.CANCEL_REQUESTED}:
                if worker_id is None or lease_generation is None:
                    raise JobConflictError("worker lease is required for this transition")
                self._require_live_lease(row, worker_id, lease_generation, now)
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, updated_at = ?, result_json = ?, error_json = ?,
                    lease_owner = ?, lease_expires_at = ?
                WHERE job_id = ?
                """,
                (
                    target.value,
                    now.isoformat(),
                    _canonical_json(safe_result) if safe_result is not None else None,
                    _canonical_json(safe_error.model_dump(mode="json")) if safe_error is not None else None,
                    None if terminal else row["lease_owner"],
                    None if terminal else row["lease_expires_at"],
                    job_id,
                ),
            )
            if prepared_event is not None:
                connection.execute(
                    """
                    INSERT INTO job_events(job_id, timestamp, level, message, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        prepared_event.job_id,
                        prepared_event.timestamp.isoformat(),
                        prepared_event.level,
                        prepared_event.message,
                        _canonical_json(prepared_event.payload),
                    ),
                )
            self._append_audit_conn(
                connection,
                now,
                worker_id or str(row["principal"]),
                "JOB_TRANSITION",
                f"job:{job_id}",
                "ALLOWED",
                {"from": expected.value, "to": target.value},
            )
            transitioned = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._job_from_row(transitioned)

    def request_cancel(self, job_id: str, *, principal: str) -> JobRecord:
        now = self._now()
        with self._write() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFoundError("job not found")
            state = JobState(str(row["state"]))
            kind = JobRequest.model_validate_json(str(row["request_json"])).kind
            if not job_can_cancel(kind, state):
                raise JobConflictError("job cannot be cancelled in its current state")
            if state is JobState.QUEUED:
                target = JobState.CANCELLED
            else:
                target = JobState.CANCEL_REQUESTED
            connection.execute(
                """
                UPDATE jobs
                SET state = ?, cancel_requested = 1, updated_at = ?,
                    lease_owner = CASE WHEN ? THEN NULL ELSE lease_owner END,
                    lease_expires_at = CASE WHEN ? THEN NULL ELSE lease_expires_at END
                WHERE job_id = ?
                """,
                (target.value, now.isoformat(), target is JobState.CANCELLED, target is JobState.CANCELLED, job_id),
            )
            if target is JobState.CANCELLED:
                prepared_event = self._prepare_event(
                    job_id,
                    now,
                    "WARNING",
                    "Job cancelled before execution",
                    {},
                )
                connection.execute(
                    """
                    INSERT INTO job_events(job_id, timestamp, level, message, payload_json)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        now.isoformat(),
                        prepared_event.level,
                        prepared_event.message,
                        _canonical_json(prepared_event.payload),
                    ),
                )
            self._append_audit_conn(
                connection,
                now,
                principal,
                "JOB_CANCEL_REQUEST",
                f"job:{job_id}",
                "ALLOWED",
                {"from": state.value, "to": target.value},
            )
            cancelled = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return self._job_from_row(cancelled)

    def append_event(
        self,
        job_id: str,
        level: EventLevel,
        message: str,
        payload: dict[str, Any] | None = None,
        *,
        worker_id: str | None = None,
        lease_generation: int | None = None,
    ) -> JobEvent:
        now = self._now()
        prepared = self._prepare_event(job_id, now, level, message, payload or {})
        with self._write() as connection:
            row = connection.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if row is None:
                raise JobNotFoundError("job not found")
            if (worker_id is None) != (lease_generation is None):
                raise JobConflictError("worker ID and lease generation must be supplied together")
            if worker_id is not None and lease_generation is not None:
                self._require_live_lease(row, worker_id, lease_generation, now)
            cursor = connection.execute(
                "INSERT INTO job_events(job_id, timestamp, level, message, payload_json) VALUES (?, ?, ?, ?, ?)",
                (
                    prepared.job_id,
                    prepared.timestamp.isoformat(),
                    prepared.level,
                    prepared.message,
                    _canonical_json(prepared.payload),
                ),
            )
            if cursor.lastrowid is None:
                raise ControllerStoreError("event sequence was not allocated")
            sequence = int(cursor.lastrowid)
        return prepared.model_copy(
            update={
                "job_id": job_id,
                "sequence": sequence,
            }
        )

    @staticmethod
    def _prepare_event(
        job_id: str,
        timestamp: datetime,
        level: EventLevel,
        message: str,
        payload: dict[str, Any],
    ) -> JobEvent:
        return JobEvent(
            job_id=job_id,
            sequence=1,
            timestamp=timestamp,
            level=level,
            message=sanitize_message(message),
            payload=sanitize_object(payload, max_bytes=MAX_EVENT_PAYLOAD_BYTES),
        )

    def events_after(self, job_id: str, sequence: int, *, limit: int = 200) -> list[JobEvent]:
        if limit < 1 or limit > 500:
            raise ValueError("event replay limit must be between 1 and 500")
        with self._read() as connection:
            if connection.execute("SELECT 1 FROM jobs WHERE job_id = ?", (job_id,)).fetchone() is None:
                raise JobNotFoundError("job not found")
            rows = connection.execute(
                "SELECT * FROM job_events WHERE job_id = ? AND sequence > ? ORDER BY sequence LIMIT ?",
                (job_id, sequence, limit),
            ).fetchall()
        return [self._event_from_row(row) for row in rows]

    def append_audit(
        self,
        principal: str,
        action: str,
        resource: str,
        outcome: Literal["ALLOWED", "DENIED", "FAILED"],
        details: dict[str, Any] | None = None,
    ) -> AuditRecord:
        now = self._now()
        with self._write() as connection:
            return self._append_audit_conn(
                connection,
                now,
                principal,
                action,
                resource,
                outcome,
                details or {},
            )

    @staticmethod
    def _append_audit_conn(
        connection: sqlite3.Connection,
        timestamp: datetime,
        principal: str,
        action: str,
        resource: str,
        outcome: Literal["ALLOWED", "DENIED", "FAILED"],
        details: dict[str, Any],
    ) -> AuditRecord:
        cursor = connection.execute(
            """
            INSERT INTO audit_records(timestamp, principal, action, resource, outcome, details_json)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (timestamp.isoformat(), principal, action, resource, outcome, _canonical_json(details)),
        )
        if cursor.lastrowid is None:
            raise ControllerStoreError("audit sequence was not allocated")
        return AuditRecord(
            sequence=int(cursor.lastrowid),
            timestamp=timestamp,
            principal=principal,
            action=action,
            resource=resource,
            outcome=outcome,
            details=details,
        )

    def audit_after(self, sequence: int) -> list[AuditRecord]:
        with self._read() as connection:
            rows = connection.execute(
                "SELECT * FROM audit_records WHERE sequence > ? ORDER BY sequence",
                (sequence,),
            ).fetchall()
        return [self._audit_from_row(row) for row in rows]

    @staticmethod
    def _plan_from_row(row: sqlite3.Row) -> PlanRecord:
        return PlanRecord(
            plan_id=str(row["plan_id"]),
            actor=str(row["actor"]),
            spec=DestroyPlanSpec.model_validate_json(str(row["spec_json"])),
            plan_hash=str(row["plan_hash"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
        )

    @staticmethod
    def _job_from_row(row: sqlite3.Row) -> JobRecord:
        error_raw = json.loads(str(row["error_json"])) if row["error_json"] is not None else None
        return JobRecord(
            job_id=str(row["job_id"]),
            request=JobRequest.model_validate_json(str(row["request_json"])),
            state=JobState(str(row["state"])),
            actor=str(row["principal"]),
            created_at=datetime.fromisoformat(str(row["created_at"])),
            updated_at=datetime.fromisoformat(str(row["updated_at"])),
            cancel_requested=bool(row["cancel_requested"]),
            result=json.loads(str(row["result_json"])) if row["result_json"] is not None else None,
            error=ErrorBody.model_validate(error_raw) if error_raw is not None else None,
            lease_owner=str(row["lease_owner"]) if row["lease_owner"] is not None else None,
            lease_generation=int(row["lease_generation"]),
            lease_expires_at=_parse_time(row["lease_expires_at"]),
        )

    @staticmethod
    def _event_from_row(row: sqlite3.Row) -> JobEvent:
        return JobEvent(
            job_id=str(row["job_id"]),
            sequence=int(row["sequence"]),
            timestamp=datetime.fromisoformat(str(row["timestamp"])),
            level=cast(EventLevel, str(row["level"])),
            message=str(row["message"]),
            payload=json.loads(str(row["payload_json"])),
        )

    @staticmethod
    def _audit_from_row(row: sqlite3.Row) -> AuditRecord:
        return AuditRecord(
            sequence=int(row["sequence"]),
            timestamp=datetime.fromisoformat(str(row["timestamp"])),
            principal=str(row["principal"]),
            action=str(row["action"]),
            resource=str(row["resource"]),
            outcome=cast(Literal["ALLOWED", "DENIED", "FAILED"], str(row["outcome"])),
            details=json.loads(str(row["details_json"])),
        )
