from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from functools import partial
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from toolkit.controller.contracts import (
    ContainerActionOperation,
    DeployOperation,
    ErrorBody,
    GenerateOperation,
    JobKind,
    JobRequest,
    JobState,
    SecretRotationOperation,
    UpdateOperation,
    VerifyOperation,
)
from toolkit.controller.operations import _workflow_log, build_operation_registry
from toolkit.controller.store import ControllerStore
from toolkit.controller.worker import (
    ControllerWorker,
    OperationCancelledError,
    OperationContext,
    OperationRegistry,
)
from toolkit.core.config.config import Config, ProjectEntry, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.deploy.operation_lease import OperationLease

PINNED_IMAGE = "docker.io/library/nginx:1@sha256:" + "a" * 64


def _blocking_process_registry(pid_path: str) -> OperationRegistry:
    registry = OperationRegistry()

    def block(_context: OperationContext, _operation):
        child = subprocess.Popen(
            [
                sys.executable,
                "-c",
                (
                    "import os,signal,sys,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
                    "open(sys.argv[1], 'w').write(str(os.getpid())); time.sleep(60)"
                ),
                pid_path,
            ]
        )
        child.wait()
        return {"ok": True}

    registry.register(JobKind.VERIFY, block)
    return registry


def _slow_process_registry() -> OperationRegistry:
    registry = OperationRegistry()

    def block(_context: OperationContext, _operation):
        time.sleep(60)
        return {"ok": True}

    registry.register(JobKind.VERIFY, block)
    return registry


def _cooperative_rotation_registry(started_path: str, completed_path: str) -> OperationRegistry:
    registry = OperationRegistry()

    def rotate(context: OperationContext, _operation):
        Path(started_path).write_text("started\n")
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            try:
                context.check_cancelled()
            except OperationCancelledError:
                break
            time.sleep(0.02)
        time.sleep(0.2)
        Path(completed_path).write_text("rollback-complete\n")
        raise OperationCancelledError("rotation cancelled after rollback")

    registry.register(JobKind.SECRET_ROTATION, rotate)
    return registry


def _graceful_rotation_registry(started_path: str, completed_path: str) -> OperationRegistry:
    registry = OperationRegistry()

    def rotate(_context: OperationContext, _operation):
        Path(started_path).write_text("started\n")
        time.sleep(0.2)
        Path(completed_path).write_text("complete\n")
        return {"ok": True}

    registry.register(JobKind.SECRET_ROTATION, rotate)
    return registry


def _queued_job(store: ControllerStore, key: str = "request-12345678"):
    return store.create_job(
        JobRequest(idempotency_key=key, operation=VerifyOperation()),
        principal="owner",
    )


def test_registry_rejects_duplicate_handler() -> None:
    registry = OperationRegistry()
    registry.register(JobKind.VERIFY, lambda _context, _operation: {"ok": True})

    with pytest.raises(ValueError, match="already registered"):
        registry.register(JobKind.VERIFY, lambda _context, _operation: {"ok": False})


def test_worker_persists_success_and_events(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    job = _queued_job(store)
    registry = OperationRegistry()

    def verify(context: OperationContext, _operation):
        context.log("verification running", {"step": "health"})
        return {"ok": True, "nodes": 3}

    registry.register(JobKind.VERIFY, verify)
    worker = ControllerWorker(store, registry, worker_id="worker-a", lease_seconds=30)

    assert worker.run_once() is True
    finished = store.get_job(job.job_id)
    assert finished.state is JobState.SUCCEEDED
    assert finished.result == {"ok": True, "nodes": 3}
    assert [event.message for event in store.events_after(job.job_id, 0)] == [
        "Job started",
        "verification running",
        "Job succeeded",
    ]


def test_worker_passes_authenticated_job_actor_to_operation_context(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    job = store.create_job(
        JobRequest(idempotency_key="actor-request-1234", operation=VerifyOperation()),
        principal="mtls:fleet-node-1",
    )
    registry = OperationRegistry()
    registry.register(JobKind.VERIFY, lambda context, _operation: {"actor": context.actor})

    ControllerWorker(store, registry, worker_id="worker-a").run_once()

    assert store.get_job(job.job_id).result == {"actor": "mtls:fleet-node-1"}


def test_worker_failure_is_terminal_and_redacted(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    job = _queued_job(store)
    registry = OperationRegistry()

    def fail(_context: OperationContext, _operation):
        raise RuntimeError("PASSWORD=super-secret /root/keys/age.key")

    registry.register(JobKind.VERIFY, fail)
    worker = ControllerWorker(store, registry, worker_id="worker-a", lease_seconds=30)

    worker.run_once()

    finished = store.get_job(job.job_id)
    assert finished.state is JobState.FAILED
    assert finished.error == ErrorBody(code="INTERNAL_ERROR", message="Operation failed", details={})
    serialized = " ".join(event.message for event in store.events_after(job.job_id, 0))
    assert "super-secret" not in serialized
    assert "age.key" not in serialized


def test_worker_consumes_cancellation(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    job = _queued_job(store)
    registry = OperationRegistry()

    def cancel(context: OperationContext, _operation):
        store.request_cancel(context.job_id, principal="owner")
        context.check_cancelled()
        raise AssertionError("cancellation checkpoint returned")

    registry.register(JobKind.VERIFY, cancel)
    worker = ControllerWorker(store, registry, worker_id="worker-a", lease_seconds=30)

    worker.run_once()

    finished = store.get_job(job.job_id)
    assert finished.state is JobState.CANCELLED
    assert finished.cancel_requested is True


def test_worker_without_handler_fails_closed(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    job = _queued_job(store)
    worker = ControllerWorker(store, OperationRegistry(), worker_id="worker-a", lease_seconds=30)

    worker.run_once()

    assert store.get_job(job.job_id).state is JobState.FAILED


def test_worker_returns_false_when_queue_is_empty(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    worker = ControllerWorker(store, OperationRegistry(), worker_id="worker-a", lease_seconds=30)

    assert worker.run_once() is False


def test_worker_health_allows_for_the_lease_renewal_interval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = [100.0]
    monkeypatch.setattr("toolkit.controller.worker.time.monotonic", lambda: clock[0])
    worker = ControllerWorker(
        ControllerStore(tmp_path / "controller.db"),
        OperationRegistry(),
        worker_id="worker-a",
        lease_seconds=30,
    )
    with worker._health_lock:
        worker._running = True
    worker._record_success()

    clock[0] = 114.9
    assert worker.is_healthy() is True
    assert worker.is_healthy(stale_after=5.0) is False
    clock[0] = 115.1
    assert worker.is_healthy() is False


def test_worker_cannot_finish_after_its_lease_is_reclaimed(tmp_path: Path) -> None:
    now = datetime(2026, 7, 10, 0, 0, tzinfo=UTC)

    def clock() -> datetime:
        return now

    store = ControllerStore(tmp_path / "controller.db", clock=clock)
    job = _queued_job(store)
    registry = OperationRegistry()

    def reclaim(_context: OperationContext, _operation):
        nonlocal now
        now += timedelta(seconds=31)
        store.claim_job(job.job_id, worker_id="worker-a", lease_seconds=30)
        return {"stale": True}

    registry.register(JobKind.VERIFY, reclaim)
    worker = ControllerWorker(store, registry, worker_id="worker-a", lease_seconds=30)

    assert worker.run_once() is True
    current = store.get_job(job.job_id)
    assert current.state is JobState.RUNNING
    assert current.lease_generation == 2
    assert [event.message for event in store.events_after(job.job_id, 0)] == ["Job started"]


def test_operation_cancelled_is_a_typed_control_flow_error() -> None:
    assert issubclass(OperationCancelledError, RuntimeError)


def test_operation_context_redacts_messages_and_structured_payloads(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    job = _queued_job(store)
    claimed = store.claim_job(job.job_id, worker_id="worker-a", lease_seconds=30)
    context = OperationContext(store, job.job_id, "worker-a", claimed.lease_generation, 30)

    context.log(
        "CF_DNS_API_TOKEN=message-secret Authorization: Bearer bearer-secret client_secret='quoted secret'",
        {
            "password": "payload-secret",
            "nested": {"api_key": "key-secret", "safe": "visible"},
            "cookie": "session-secret",
        },
    )

    event = store.events_after(job.job_id, 0)[0]
    assert "message-secret" not in event.message
    assert "bearer-secret" not in event.message
    assert "quoted secret" not in event.message
    assert event.payload == {
        "password": "[REDACTED]",
        "nested": {"api_key": "[REDACTED]", "safe": "visible"},
        "cookie": "[REDACTED]",
    }


def test_workflow_adapter_preserves_sanitized_progress_lines() -> None:
    context = MagicMock()

    _workflow_log(context, "deploy")("Waiting for service health checks")

    context.check_cancelled.assert_called_once_with()
    context.log.assert_called_once_with(
        "Waiting for service health checks",
        {"stage": "deploy"},
    )


def test_workflow_adapter_ignores_blank_subprocess_lines() -> None:
    context = MagicMock()

    _workflow_log(context, "destroy")("   ")

    context.check_cancelled.assert_called_once_with()
    context.log.assert_not_called()


def test_deploy_operation_runs_one_coordinated_workflow(tmp_path: Path, monkeypatch) -> None:
    calls: list[dict] = []

    async def deploy(_root, _cfg, **kwargs):
        calls.append(kwargs)
        kwargs["on_log"]("Pulling release images")
        kwargs["on_step"]("compose-apps", "running")
        kwargs["on_progress"]({"step": "compose-apps", "percent": "40"})
        return MagicMock(success=True)

    monkeypatch.setattr(
        "toolkit.core.config.config.load_config",
        lambda _path: MagicMock(enabled_nodes=["infra", "apps", "media"]),
    )
    monkeypatch.setattr("toolkit.core.deploy.deploy_workflow.run_deploy_workflow", deploy)
    context = MagicMock()
    handler = build_operation_registry(tmp_path).resolve(JobKind.DEPLOY)

    result = handler(
        context,
        DeployOperation(skip_infrastructure=False, skip_dns=True),
    )

    assert result == {"ok": True, "target": "all"}
    assert len(calls) == 1
    assert calls[0]["targets"] is None
    assert calls[0]["skip_infra"] is False
    assert calls[0]["skip_dns"] is True
    context.log.assert_any_call("Pulling release images", {"stage": "deploy"})


def test_worker_assigns_long_deadlines_only_to_long_running_operations(tmp_path: Path) -> None:
    worker = ControllerWorker(
        ControllerStore(tmp_path / "controller.db"),
        build_operation_registry(tmp_path),
        worker_id="worker-a",
    )

    assert worker._operation_timeout(JobKind.DEPLOY) == 4 * 60 * 60
    assert worker._operation_timeout(JobKind.RECOVER) == 4 * 60 * 60
    assert worker._operation_timeout(JobKind.HOST_RECONCILE) == 4 * 60 * 60
    assert worker._operation_timeout(JobKind.CONFIG_APPLY) == 4 * 60 * 60
    assert worker._operation_timeout(JobKind.MAINTENANCE) == 4 * 60 * 60
    assert worker._operation_timeout(JobKind.BACKUP_DRILL) == 4 * 60 * 60
    assert worker._operation_timeout(JobKind.RESTORE_DRILL) == 4 * 60 * 60
    assert worker._operation_timeout(JobKind.SECRET_ROTATION) == 4 * 60 * 60
    assert worker._operation_timeout(JobKind.VERIFY) == 30 * 60


def test_worker_redacts_structured_result_before_persistence(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    job = _queued_job(store)
    registry = OperationRegistry()
    registry.register(
        JobKind.VERIFY,
        lambda _context, _operation: {"ok": True, "authorization": "Bearer secret", "nested": {"safe": 1}},
    )

    ControllerWorker(store, registry, worker_id="worker-a").run_once()

    assert store.get_job(job.job_id).result == {
        "ok": True,
        "authorization": "[REDACTED]",
        "nested": {"safe": 1},
    }


def test_default_registry_handles_every_typed_job_kind(tmp_path: Path) -> None:
    registry = build_operation_registry(tmp_path)
    assert registry.kinds == frozenset(JobKind)


def test_update_with_a_missing_plan_fails_closed(tmp_path: Path) -> None:
    save_config(Config(domain="example.test"), config_path(tmp_path))
    store = ControllerStore(tmp_path / "controller.db")
    job = store.create_job(
        JobRequest(
            idempotency_key="update-request-1234",
            operation=UpdateOperation(action="apply", services=["grafana"], revision="a" * 64),
        ),
        principal="owner",
    )
    worker = ControllerWorker(
        store,
        build_operation_registry(tmp_path),
        worker_id="worker-a",
        lease_seconds=30,
    )

    worker.run_once()

    finished = store.get_job(job.job_id)
    assert finished.state is JobState.FAILED
    assert finished.error is not None
    assert finished.error.code == "CONFLICT"


def test_container_action_rejects_service_outside_managed_catalog(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    job = store.create_job(
        JobRequest(
            idempotency_key="container-action-1234",
            operation=ContainerActionOperation(service="unmanaged", action="restart"),
        ),
        principal="mtls:homelab-ui",
    )

    ControllerWorker(
        store,
        build_operation_registry(tmp_path),
        worker_id="worker-a",
    ).run_once()

    finished = store.get_job(job.job_id)
    assert finished.state is JobState.FAILED
    assert finished.error is not None
    assert finished.error.code == "OPERATION_REJECTED"


def test_container_action_accepts_declared_project(monkeypatch, tmp_path: Path) -> None:
    cfg = Config(domain="example.test")
    cfg.projects.entries = [
        ProjectEntry(
            subdomain="demo",
            auth_mode="forward_auth",
            exposure="private",
            docker_image=PINNED_IMAGE,
            placement="apps",
            container_port=45678,
        )
    ]
    save_config(cfg, config_path(tmp_path))
    commands: list[str] = []

    def fake_ssh(_cfg, _ip, command, **_kwargs):
        commands.append(command)
        return 0, "demo", ""

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)
    store = ControllerStore(tmp_path / "controller.db")
    job = store.create_job(
        JobRequest(
            idempotency_key="project-action-1234",
            operation=ContainerActionOperation(service="project-demo", action="restart"),
        ),
        principal="mtls:homelab-ui",
    )

    ControllerWorker(store, build_operation_registry(tmp_path), worker_id="worker-a").run_once()

    assert store.get_job(job.job_id).state is JobState.SUCCEEDED
    assert commands == ["docker restart demo"]


def test_controller_handler_respects_shared_operation_lease(tmp_path: Path, monkeypatch) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    job = store.create_job(
        JobRequest(idempotency_key="generate-request-1234", operation=GenerateOperation()),
        principal="owner",
    )
    generate = MagicMock()
    monkeypatch.setattr("toolkit.core.generate.generate.run_full_generate", generate)
    lease = OperationLease.acquire(tmp_path, "direct-cli")
    try:
        ControllerWorker(
            store,
            build_operation_registry(tmp_path),
            worker_id="worker-a",
        ).run_once()
    finally:
        lease.release()

    finished = store.get_job(job.job_id)
    assert finished.state is JobState.FAILED
    assert finished.error is not None
    assert finished.error.code == "CONFLICT"
    generate.assert_not_called()


def test_isolated_worker_cancellation_terminates_descendant_process_group(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    job = _queued_job(store)
    pid_path = tmp_path / "descendant.pid"
    factory = partial(_blocking_process_registry, str(pid_path))
    worker = ControllerWorker(
        store,
        factory(),
        worker_id="worker-a",
        registry_factory=factory,
        process_poll_interval=0.05,
    )

    def cancel_when_started() -> None:
        deadline = time.monotonic() + 10
        while not pid_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert pid_path.exists()
        store.request_cancel(job.job_id, principal="local:operator")

    canceller = threading.Thread(target=cancel_when_started)
    canceller.start()
    worker.run_once()
    canceller.join(timeout=10)

    assert not canceller.is_alive()
    assert store.get_job(job.job_id).state is JobState.CANCELLED
    descendant_pid = int(pid_path.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(descendant_pid, 0)


def test_isolated_rotation_cancellation_allows_rollback_to_finish(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    job = store.create_job(
        JobRequest(
            idempotency_key="rotation-cancel-1234",  # gitleaks:allow - deterministic test identifier
            operation=SecretRotationOperation(secret_names=["GRAFANA_WEBHOOK_HMAC_SECRET"]),
        ),
        principal="owner",
    )
    started = tmp_path / "rotation-started"
    completed = tmp_path / "rotation-rollback-complete"
    factory = partial(_cooperative_rotation_registry, str(started), str(completed))
    worker = ControllerWorker(
        store,
        factory(),
        worker_id="worker-a",
        registry_factory=factory,
        process_poll_interval=0.02,
    )

    def cancel_when_started() -> None:
        deadline = time.monotonic() + 10
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started.exists()
        store.request_cancel(job.job_id, principal="local:operator")

    canceller = threading.Thread(target=cancel_when_started)
    canceller.start()
    worker.run_once()
    canceller.join(timeout=10)

    assert completed.read_text() == "rollback-complete\n"
    assert store.get_job(job.job_id).state is JobState.CANCELLED


def test_worker_shutdown_allows_rotation_child_to_finish(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    job = store.create_job(
        JobRequest(
            idempotency_key="rotation-shutdown-1234",
            operation=SecretRotationOperation(secret_names=["GRAFANA_WEBHOOK_HMAC_SECRET"]),
        ),
        principal="owner",
    )
    started = tmp_path / "rotation-started"
    completed = tmp_path / "rotation-complete"
    factory = partial(_graceful_rotation_registry, str(started), str(completed))
    worker = ControllerWorker(
        store,
        factory(),
        worker_id="worker-a",
        registry_factory=factory,
        process_poll_interval=0.02,
    )
    stop = threading.Event()

    def stop_when_started() -> None:
        deadline = time.monotonic() + 10
        while not started.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert started.exists()
        stop.set()

    stopper = threading.Thread(target=stop_when_started)
    stopper.start()
    worker.run_once(stop=stop)
    stopper.join(timeout=10)

    assert completed.read_text() == "complete\n"
    assert store.get_job(job.job_id).state is JobState.SUCCEEDED


def test_isolated_worker_enforces_operation_deadline(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    job = _queued_job(store)
    worker = ControllerWorker(
        store,
        _slow_process_registry(),
        worker_id="worker-a",
        registry_factory=_slow_process_registry,
        process_poll_interval=0.05,
        operation_timeout_seconds=0.2,
    )

    worker.run_once()

    finished = store.get_job(job.job_id)
    assert finished.state is JobState.FAILED
    assert finished.error is not None
    assert finished.error.code == "OPERATION_FAILED"


def test_isolated_worker_reconstructs_production_registry_in_child(tmp_path: Path) -> None:
    save_config(Config(domain="example.test"), config_path(tmp_path))
    store = ControllerStore(tmp_path / "controller.db")
    job = store.create_job(
        JobRequest(
            idempotency_key="isolated-update-1234",
            operation=UpdateOperation(action="apply", services=["grafana"], revision="a" * 64),
        ),
        principal="local:operator",
    )
    factory = partial(build_operation_registry, tmp_path)
    worker = ControllerWorker(
        store,
        factory(),
        worker_id="worker-a",
        registry_factory=factory,
        process_poll_interval=0.05,
    )

    worker.run_once()

    finished = store.get_job(job.job_id)
    assert finished.state is JobState.FAILED
    assert finished.error is not None
    assert finished.error.code == "CONFLICT"
