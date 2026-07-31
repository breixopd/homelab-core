from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError
from toolkit.controller.contracts import (
    JobKind,
    JobRecord,
    JobRequest,
    JobState,
    ServiceVerifyOperation,
)
from toolkit.controller.operations import OperationExecutionError, build_operation_registry
from toolkit.controller.sanitization import sanitize_message
from toolkit.controller.service_management_api import (
    ServiceManagementNotFoundError,
    aggregate_verification_status,
    read_service_verification,
)
from toolkit.controller.store import ControllerStore, JobQueueLimitError
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.verify.models import HookVerifyResult, VerifyCheck, VerifyStatus


def _request(service: str = "grafana", *, suffix: str = "a") -> JobRequest:
    return JobRequest(
        idempotency_key=f"service-verify-{service}-{suffix * 16}",
        operation=ServiceVerifyOperation(service=service),
    )


def _job(
    *,
    state: JobState,
    suffix: str,
    updated_at: datetime,
    result: dict[str, object] | None = None,
) -> JobRecord:
    return JobRecord(
        job_id=f"job-{suffix}",
        request=_request(suffix=suffix),
        state=state,
        actor="mtls:homelab-ui",
        created_at=updated_at,
        updated_at=updated_at,
        result=result,
    )


def test_service_verify_contract_cannot_enable_framework_checks() -> None:
    request = _request()
    assert request.kind is JobKind.SERVICE_VERIFY
    assert request.operation.model_dump() == {"kind": JobKind.SERVICE_VERIFY, "service": "grafana"}

    with pytest.raises(ValidationError):
        ServiceVerifyOperation.model_validate({"service": "grafana", "include_framework": True})


def test_service_verify_handler_bounds_redacts_and_aggregates(tmp_path: Path, monkeypatch) -> None:
    save_config(Config(), config_path(tmp_path))
    checks = [
        VerifyCheck(
            "grafana",
            f"check-{index}",
            index > 0,
            ("token=super-secret https://admin:another-secret@example.test/ " + "x" * 300 if index == 0 else "healthy"),
            status=VerifyStatus.FAIL if index == 0 else VerifyStatus.PASS,
        )
        for index in range(70)
    ]
    plugin = SimpleNamespace(is_enabled=lambda _cfg: True)
    monkeypatch.setattr("toolkit.services.get_service_plugin", lambda _service: plugin)
    monkeypatch.setattr(
        "toolkit.core.ops.hook_verify.verify_hooks",
        lambda *_args, **kwargs: (
            pytest.fail("framework checks must be disabled")
            if kwargs.get("include_framework") is not False
            else HookVerifyResult(checks)
        ),
    )
    context = MagicMock()

    result = build_operation_registry(tmp_path).resolve(JobKind.SERVICE_VERIFY)(
        context,
        ServiceVerifyOperation(service="grafana"),
    )

    assert result["overall_status"] == "fail"
    assert len(result["checks"]) == 64
    assert result["checks"][-1] == {
        "service": "grafana",
        "check": "result_limit",
        "status": "degraded",
        "detail": "7 additional checks were omitted",
    }
    assert len(result["checks"][0]["detail"]) <= 200
    assert "super-secret" not in result["checks"][0]["detail"]
    assert "another-secret" not in result["checks"][0]["detail"]
    assert result["checks"][0]["detail"].count("[REDACTED]") == 2
    assert datetime.fromisoformat(result["observed_at"]).tzinfo is not None
    context.check_cancelled.assert_called_once()


@pytest.mark.parametrize(
    ("plugin", "checks", "message"),
    [
        (None, [VerifyCheck("missing", "health", True)], "service is not managed"),
        (
            SimpleNamespace(is_enabled=lambda _cfg: False),
            [VerifyCheck("grafana", "health", True)],
            "service is disabled",
        ),
        (SimpleNamespace(is_enabled=lambda _cfg: True), [], "service has no verification checks"),
    ],
)
def test_service_verify_handler_rejects_unmanaged_disabled_or_empty(
    tmp_path: Path,
    monkeypatch,
    plugin,
    checks: list[VerifyCheck],
    message: str,
) -> None:
    save_config(Config(), config_path(tmp_path))
    monkeypatch.setattr("toolkit.services.get_service_plugin", lambda _service: plugin)
    monkeypatch.setattr(
        "toolkit.core.ops.hook_verify.verify_hooks",
        lambda *_args, **_kwargs: HookVerifyResult(checks),
    )

    with pytest.raises(OperationExecutionError, match=message) as error:
        build_operation_registry(tmp_path).resolve(JobKind.SERVICE_VERIFY)(
            MagicMock(),
            ServiceVerifyOperation(service="missing" if plugin is None else "grafana"),
        )

    assert error.value.code == "OPERATION_REJECTED"


def test_service_verify_queue_is_bounded_but_completed_checks_can_rerun(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    first, created = store.submit_job(_request(suffix="a"), principal="mtls:homelab-ui", active_limit=1)
    assert created is True

    with pytest.raises(JobQueueLimitError):
        store.submit_job(_request(suffix="b"), principal="local:operator", active_limit=1)

    claimed = store.claim_job(first.job_id, worker_id="worker", lease_seconds=60)
    store.transition(
        first.job_id,
        expected=JobState.RUNNING,
        target=JobState.SUCCEEDED,
        result={"service": "grafana", "checks": []},
        worker_id="worker",
        lease_generation=claimed.lease_generation,
    )
    rerun, rerun_created = store.submit_job(
        _request(suffix="b"),
        principal="mtls:homelab-ui",
        active_limit=1,
    )
    assert rerun_created is True
    assert rerun.job_id != first.job_id


def test_service_verification_projection_keeps_stale_previous_result_while_running(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.setattr("toolkit.services.get_service_plugin", lambda _service: object())
    old = datetime.now(UTC) - timedelta(minutes=20)
    terminal = _job(
        state=JobState.SUCCEEDED,
        suffix="a",
        updated_at=old,
        result={
            "service": "grafana",
            "observed_at": old.isoformat(),
            "checks": [
                {"service": "grafana", "check": "health", "status": "degraded", "detail": "slow"},
                {"service": "grafana", "check": "", "status": "pass", "detail": "invalid"},
            ],
        },
    )
    active = _job(state=JobState.RUNNING, suffix="b", updated_at=datetime.now(UTC))
    store = SimpleNamespace(recent_jobs=lambda **_kwargs: [active, terminal])

    view = read_service_verification(tmp_path, store, "grafana")

    assert view.state == "running"
    assert view.overall_status == "degraded"
    assert [check.check for check in view.checks] == ["health"]
    assert view.observed_at == old
    assert view.stale is True
    assert view.job_id == active.job_id


def test_service_verification_projection_rejects_unknown_service(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr("toolkit.services.get_service_plugin", lambda _service: None)
    with pytest.raises(ServiceManagementNotFoundError):
        read_service_verification(tmp_path, SimpleNamespace(recent_jobs=lambda **_kwargs: []), "missing")


@pytest.mark.parametrize(
    ("job_state", "view_state"),
    [(JobState.CANCEL_REQUESTED, "running"), (JobState.CANCELLED, "complete")],
)
def test_service_verification_projection_handles_cancellation_states(
    tmp_path: Path,
    monkeypatch,
    job_state: JobState,
    view_state: str,
) -> None:
    monkeypatch.setattr("toolkit.services.get_service_plugin", lambda _service: object())
    job = _job(state=job_state, suffix="a", updated_at=datetime.now(UTC))

    view = read_service_verification(
        tmp_path,
        SimpleNamespace(recent_jobs=lambda **_kwargs: [job]),
        "grafana",
    )

    assert view.state == view_state
    assert view.overall_status == ("not_ready" if job_state is JobState.CANCELLED else None)


def test_status_precedence_and_all_not_applicable() -> None:
    assert aggregate_verification_status(["pass", "degraded", "not_ready"]) == "not_ready"
    assert aggregate_verification_status(["pass", "fail", "not_ready"]) == "fail"
    assert aggregate_verification_status(["not_applicable", "not_applicable"]) == "not_applicable"
    assert aggregate_verification_status([]) == "not_applicable"


def test_sanitizer_redacts_authorization_assignments_and_url_userinfo() -> None:
    detail = sanitize_message("Authorization: Bearer abc password=hunter2 https://admin:secret@example.test/path")
    assert "abc" not in detail
    assert "hunter2" not in detail
    assert "admin:secret" not in detail
    assert detail.count("[REDACTED]") == 3


def test_webui_submissions_use_fresh_idempotency_keys(monkeypatch) -> None:
    import toolkit.webui.routers.services as services_router

    submitted: list[JobRequest] = []

    class Controller:
        def submit(self, request: JobRequest):
            submitted.append(request)
            return SimpleNamespace(job_id=f"job-{len(submitted)}")

    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(controller=Controller())))
    monkeypatch.setattr(services_router, "is_toolkit_admin", lambda _request: True)

    first = asyncio.run(services_router.service_verification_start(request, "grafana"))
    second = asyncio.run(services_router.service_verification_start(request, "grafana"))

    assert first.status_code == second.status_code == 303
    assert submitted[0].idempotency_key != submitted[1].idempotency_key
    assert all(item.idempotency_key.startswith("service-verify-grafana-") for item in submitted)
