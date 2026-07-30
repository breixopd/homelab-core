"""Durable controller worker with leases, cancellation, and safe events."""

from __future__ import annotations

import logging
import multiprocessing as mp
import os
import signal
import threading
import time
from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from multiprocessing.connection import Connection
from multiprocessing.process import BaseProcess
from pathlib import Path
from typing import Any

from toolkit.controller.contracts import (
    ErrorBody,
    ErrorCode,
    EventLevel,
    JobKind,
    JobState,
    OperationPayload,
)
from toolkit.controller.sanitization import (
    MAX_EVENT_PAYLOAD_BYTES,
    MAX_RESULT_BYTES,
    sanitize_message,
    sanitize_object,
)
from toolkit.controller.store import ControllerStore, JobConflictError

OperationHandler = Callable[["OperationContext", OperationPayload], dict[str, Any]]
RegistryFactory = Callable[[], "OperationRegistry"]
logger = logging.getLogger(__name__)
_DEFAULT_OPERATION_TIMEOUT_SECONDS = 30 * 60
_LONG_OPERATION_TIMEOUT_SECONDS = 4 * 60 * 60
_LONG_RUNNING_KINDS = frozenset(
    {
        JobKind.DEPLOY,
        JobKind.RECOVER,
        JobKind.DESTROY_INFRA,
        JobKind.HOST_RECONCILE,
        JobKind.CONFIG_APPLY,
        JobKind.MAINTENANCE,
        JobKind.BACKUP_DRILL,
        JobKind.RESTORE_DRILL,
        JobKind.UPDATE,
        JobKind.SECRET_ROTATION,
    }
)


class OperationCancelledError(RuntimeError):
    pass


class OperationLeaseLostError(RuntimeError):
    pass


class OperationHandlerNotFoundError(RuntimeError):
    pass


class OperationWorkerShutdownError(RuntimeError):
    pass


class IsolatedOperationError(RuntimeError):
    def __init__(self, error_type: str):
        self.error_type = error_type
        super().__init__("isolated operation failed")


class SafeOperationError(RuntimeError):
    def __init__(self, code: ErrorCode, message: str):
        self.code = code
        self.safe_message = message
        super().__init__(message)


class OperationRegistry:
    def __init__(self) -> None:
        self._handlers: dict[JobKind, OperationHandler] = {}

    def register(self, kind: JobKind, handler: OperationHandler) -> None:
        if kind in self._handlers:
            raise ValueError(f"handler for {kind.value} is already registered")
        self._handlers[kind] = handler

    def resolve(self, kind: JobKind) -> OperationHandler:
        try:
            return self._handlers[kind]
        except KeyError as exc:
            raise OperationHandlerNotFoundError(f"no handler registered for {kind.value}") from exc

    @property
    def kinds(self) -> frozenset[JobKind]:
        return frozenset(self._handlers)


class OperationContext:
    def __init__(
        self,
        store: ControllerStore,
        job_id: str,
        worker_id: str,
        lease_generation: int,
        lease_seconds: int,
        heartbeat_callback: Callable[[], None] | None = None,
        *,
        actor: str = "controller",
    ):
        self.store = store
        self.job_id = job_id
        self.worker_id = worker_id
        self.lease_generation = lease_generation
        self.lease_seconds = lease_seconds
        self.heartbeat_callback = heartbeat_callback or (lambda: None)
        self.actor = actor
        self._lease_lost = threading.Event()

    def mark_lease_lost(self) -> None:
        self._lease_lost.set()

    def log(
        self,
        message: str,
        payload: dict[str, Any] | None = None,
        *,
        level: EventLevel = "INFO",
    ) -> None:
        try:
            self.store.append_event(
                self.job_id,
                level,
                sanitize_message(message),
                sanitize_object(payload or {}, max_bytes=MAX_EVENT_PAYLOAD_BYTES),
                worker_id=self.worker_id,
                lease_generation=self.lease_generation,
            )
        except JobConflictError as exc:
            self.mark_lease_lost()
            raise OperationLeaseLostError("operation lease was lost") from exc

    def check_cancelled(self) -> None:
        if self._lease_lost.is_set():
            raise OperationLeaseLostError("operation lease was lost")
        try:
            state = self.store.assert_lease(
                self.job_id,
                worker_id=self.worker_id,
                lease_generation=self.lease_generation,
            ).state
        except JobConflictError as exc:
            self.mark_lease_lost()
            raise OperationLeaseLostError("operation lease was lost") from exc
        if state is JobState.CANCEL_REQUESTED:
            raise OperationCancelledError("operation cancellation requested")


class _LeaseHeartbeat(AbstractContextManager[None]):
    def __init__(self, context: OperationContext):
        self.context = context
        self._stopped = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"controller-heartbeat-{context.job_id[:8]}",
            daemon=True,
        )

    def _run(self) -> None:
        interval = max(0.25, self.context.lease_seconds / 3)
        while not self._stopped.wait(interval):
            try:
                self.context.store.renew_lease(
                    self.context.job_id,
                    worker_id=self.context.worker_id,
                    lease_generation=self.context.lease_generation,
                    lease_seconds=self.context.lease_seconds,
                )
                self.context.heartbeat_callback()
            except JobConflictError:
                self.context.mark_lease_lost()
                return
            except Exception as exc:
                logger.error(
                    "Controller lease renewal failed job_id=%s error_type=%s",
                    self.context.job_id,
                    type(exc).__name__,
                )
                self.context.mark_lease_lost()
                return

    def __enter__(self) -> None:
        self._thread.start()

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self._stopped.set()
        self._thread.join(timeout=1)


def _isolated_operation_entry(
    store_path: str,
    job_id: str,
    worker_id: str,
    lease_generation: int,
    lease_seconds: int,
    actor: str,
    kind: JobKind,
    operation: OperationPayload,
    registry_factory: RegistryFactory,
    sender: Connection,
) -> None:
    """Execute one operation in a new session and return only bounded typed data."""
    try:
        os.setsid()
        sender.send(("ready",))
        store = ControllerStore(Path(store_path))
        context = OperationContext(
            store,
            job_id,
            worker_id,
            lease_generation,
            lease_seconds,
            actor=actor,
        )
        handler = registry_factory().resolve(kind)
        result = sanitize_object(handler(context, operation), max_bytes=MAX_RESULT_BYTES)
        context.check_cancelled()
        sender.send(("succeeded", result))
    except OperationCancelledError:
        sender.send(("cancelled",))
    except OperationLeaseLostError:
        sender.send(("lease_lost",))
    except SafeOperationError as exc:
        sender.send(("safe_error", exc.code, sanitize_message(exc.safe_message)[:500]))
    except BaseException as exc:
        sender.send(("error", sanitize_message(type(exc).__name__)[:128]))
    finally:
        sender.close()


def _terminate_process_tree(
    process: BaseProcess,
    *,
    process_group_ready: bool,
    grace_seconds: float = 2.0,
) -> None:
    """Terminate the isolated worker and every descendant in its process group."""
    pid = process.pid
    if pid is None:
        return
    if process_group_ready:
        try:
            os.killpg(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    elif process.is_alive():
        process.terminate()
    process.join(timeout=grace_seconds)
    if process_group_ready:
        deadline = time.monotonic() + grace_seconds
        while time.monotonic() < deadline:
            try:
                os.killpg(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.05)
        try:
            os.killpg(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    elif process.is_alive():
        process.kill()
    process.join(timeout=2)


class ControllerWorker:
    def __init__(
        self,
        store: ControllerStore,
        registry: OperationRegistry,
        *,
        worker_id: str,
        lease_seconds: int = 30,
        registry_factory: RegistryFactory | None = None,
        process_poll_interval: float = 0.2,
        operation_timeout_seconds: float | None = None,
    ):
        if lease_seconds <= 0:
            raise ValueError("lease_seconds must be positive")
        if process_poll_interval <= 0 or (operation_timeout_seconds is not None and operation_timeout_seconds <= 0):
            raise ValueError("worker process intervals must be positive")
        if registry_factory is not None and "forkserver" not in mp.get_all_start_methods():
            raise RuntimeError("isolated controller operations require multiprocessing forkserver support")
        self.store = store
        self.registry = registry
        self.worker_id = worker_id
        self.lease_seconds = lease_seconds
        self.registry_factory = registry_factory
        self.process_poll_interval = process_poll_interval
        self.operation_timeout_seconds = operation_timeout_seconds
        self._health_lock = threading.Lock()
        self._running = False
        self._last_heartbeat: datetime | None = None
        self._last_heartbeat_monotonic = 0.0
        self._consecutive_failures = 0

    def run_once(self, *, stop: threading.Event | None = None) -> bool:
        self._record_success()
        job = self.store.claim_next(worker_id=self.worker_id, lease_seconds=self.lease_seconds)
        if job is None:
            return False

        context = OperationContext(
            self.store,
            job.job_id,
            self.worker_id,
            job.lease_generation,
            self.lease_seconds,
            self._record_success,
            actor=job.actor,
        )
        try:
            context.log("Job started", {"kind": job.request.kind.value})
            if self.registry_factory is None:
                handler = self.registry.resolve(job.request.kind)
                with _LeaseHeartbeat(context):
                    result = handler(context, job.request.operation)
                    context.check_cancelled()
            else:
                result = self._run_isolated(context, job.request.kind, job.request.operation, stop=stop)
            context.check_cancelled()
            partial = result.get("outcome") == "partial_failure"
            self.store.transition(
                job.job_id,
                expected=JobState.RUNNING,
                target=JobState.PARTIAL_FAILURE if partial else JobState.SUCCEEDED,
                result=result,
                event=(
                    "WARNING" if partial else "INFO",
                    "Job completed with partial failure" if partial else "Job succeeded",
                    {},
                ),
                worker_id=self.worker_id,
                lease_generation=job.lease_generation,
            )
        except OperationCancelledError:
            try:
                self.store.transition(
                    job.job_id,
                    expected=JobState.CANCEL_REQUESTED,
                    target=JobState.CANCELLED,
                    event=("WARNING", "Job cancelled", {}),
                    worker_id=self.worker_id,
                    lease_generation=job.lease_generation,
                )
            except JobConflictError:
                return True
        except OperationLeaseLostError:
            return True
        except JobConflictError:
            self._finalize_won_cancellation(context)
            return True
        except OperationWorkerShutdownError:
            return True
        except IsolatedOperationError as exc:
            self._fail(context, exc.error_type)
        except SafeOperationError as exc:
            self._fail(context, type(exc).__name__, code=exc.code, message=exc.safe_message)
        except Exception as exc:
            self._fail(context, type(exc).__name__)
        return True

    def _finalize_won_cancellation(self, context: OperationContext) -> None:
        try:
            current = self.store.assert_lease(
                context.job_id,
                worker_id=context.worker_id,
                lease_generation=context.lease_generation,
            )
            if current.state is JobState.CANCEL_REQUESTED:
                self.store.transition(
                    context.job_id,
                    expected=JobState.CANCEL_REQUESTED,
                    target=JobState.CANCELLED,
                    event=("WARNING", "Job cancelled", {}),
                    worker_id=context.worker_id,
                    lease_generation=context.lease_generation,
                )
        except JobConflictError:
            pass

    def run_forever(self, stop: threading.Event, *, poll_interval: float = 1.0) -> None:
        if poll_interval <= 0:
            raise ValueError("poll_interval must be positive")
        with self._health_lock:
            self._running = True
        self._record_success()
        try:
            while not stop.is_set():
                try:
                    worked = self.run_once(stop=stop)
                except Exception as exc:
                    failures = self._record_failure()
                    if failures & (failures - 1) == 0:
                        logger.error(
                            "Controller worker loop failed error_type=%s consecutive_failures=%d",
                            type(exc).__name__,
                            failures,
                        )
                    worked = False
                if not worked:
                    stop.wait(poll_interval)
        finally:
            with self._health_lock:
                self._running = False

    def _run_isolated(
        self,
        context: OperationContext,
        kind: JobKind,
        operation: OperationPayload,
        *,
        stop: threading.Event | None,
    ) -> dict[str, Any]:
        if self.registry_factory is None:
            raise RuntimeError("isolated operation registry is unavailable")
        process_context = mp.get_context("forkserver")
        receiver, sender = process_context.Pipe(duplex=False)
        process = process_context.Process(
            target=_isolated_operation_entry,
            args=(
                str(self.store.path),
                context.job_id,
                context.worker_id,
                context.lease_generation,
                context.lease_seconds,
                context.actor,
                kind,
                operation,
                self.registry_factory,
                sender,
            ),
            name=f"controller-operation-{context.job_id[:8]}",
        )
        process.start()
        sender.close()
        deadline = time.monotonic() + self._operation_timeout(kind)
        process_group_ready = False
        outcome: tuple[Any, ...] | None = None
        try:
            with _LeaseHeartbeat(context):
                while outcome is None:
                    if receiver.poll(self.process_poll_interval):
                        try:
                            message = receiver.recv()
                        except EOFError:
                            message = None
                        if message and message[0] == "ready":
                            process_group_ready = True
                            continue
                        outcome = message
                        break
                    if not process.is_alive():
                        if receiver.poll(0.1):
                            try:
                                outcome = receiver.recv()
                            except EOFError:
                                outcome = None
                        break
                    if stop is not None and stop.is_set() and kind is not JobKind.SECRET_ROTATION:
                        raise OperationWorkerShutdownError("controller worker is shutting down")
                    # Secret rotation owns cooperative cancellation in the child
                    # so its credential rollback cannot be killed mid-flight.
                    if kind is not JobKind.SECRET_ROTATION:
                        context.check_cancelled()
                    if time.monotonic() >= deadline:
                        raise SafeOperationError("OPERATION_FAILED", "Operation exceeded its execution deadline")
        finally:
            _terminate_process_tree(process, process_group_ready=process_group_ready)
            receiver.close()

        if not outcome:
            raise IsolatedOperationError("OperationProcessExit")
        status = outcome[0]
        if status == "succeeded" and len(outcome) == 2 and isinstance(outcome[1], dict):
            return outcome[1]
        if status == "cancelled":
            raise OperationCancelledError("operation cancellation requested")
        if status == "lease_lost":
            raise OperationLeaseLostError("operation lease was lost")
        if status == "safe_error" and len(outcome) == 3:
            raise SafeOperationError(outcome[1], outcome[2])
        if status == "error" and len(outcome) == 2:
            raise IsolatedOperationError(str(outcome[1]))
        raise IsolatedOperationError("OperationProtocolError")

    def _operation_timeout(self, kind: JobKind) -> float:
        if self.operation_timeout_seconds is not None:
            return self.operation_timeout_seconds
        if kind in _LONG_RUNNING_KINDS:
            return _LONG_OPERATION_TIMEOUT_SECONDS
        return _DEFAULT_OPERATION_TIMEOUT_SECONDS

    @property
    def last_heartbeat(self) -> datetime | None:
        with self._health_lock:
            return self._last_heartbeat

    def is_healthy(self, *, stale_after: float | None = None) -> bool:
        # Lease renewal is intentionally bounded to one database write per
        # lease_seconds / 3. Readiness must allow more than that interval or a
        # healthy worker oscillates to 503 during long-running operations.
        threshold = stale_after if stale_after is not None else max(5.0, self.lease_seconds / 2)
        with self._health_lock:
            age = time.monotonic() - self._last_heartbeat_monotonic
            return self._running and self._consecutive_failures == 0 and age <= threshold

    def _record_success(self) -> None:
        with self._health_lock:
            self._last_heartbeat = datetime.now(UTC)
            self._last_heartbeat_monotonic = time.monotonic()
            self._consecutive_failures = 0

    def _record_failure(self) -> int:
        with self._health_lock:
            self._consecutive_failures += 1
            return self._consecutive_failures

    def _fail(
        self,
        context: OperationContext,
        error_type: str,
        *,
        code: ErrorCode = "INTERNAL_ERROR",
        message: str = "Operation failed",
    ) -> bool:
        try:
            current = self.store.assert_lease(
                context.job_id,
                worker_id=context.worker_id,
                lease_generation=context.lease_generation,
            )
            self.store.transition(
                context.job_id,
                expected=current.state,
                target=JobState.FAILED,
                error=ErrorBody(code=code, message=message),
                event=("ERROR", f"Job failed ({error_type})", {}),
                worker_id=context.worker_id,
                lease_generation=context.lease_generation,
            )
        except JobConflictError:
            return False
        return True
