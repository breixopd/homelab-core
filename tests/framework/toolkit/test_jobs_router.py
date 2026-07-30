from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from toolkit.controller.client import ControllerClientError
from toolkit.controller.contracts import JobEvent, JobRecord, JobRequest, JobState, VerifyOperation
from toolkit.controller.read_models import JobSummaryView, JobsView

pytestmark = pytest.mark.anyio

_JOB_ID = "job-123456789012"
_NOW = datetime(2026, 7, 11, 10, 30, tzinfo=UTC)


class JobsController:
    def __init__(self) -> None:
        self.unavailable = False
        self.cancelled: list[str] = []
        self.record = JobRecord(
            job_id=_JOB_ID,
            request=JobRequest(
                idempotency_key="jobs-router-request",
                operation=VerifyOperation(targets=["apps"]),
            ),
            state=JobState.RUNNING,
            actor="ui:homelab-ui",
            created_at=_NOW,
            updated_at=_NOW,
        )

    def jobs(self, *, limit: int = 100) -> JobsView:
        assert limit == 100
        if self.unavailable:
            raise ControllerClientError("unavailable")
        return JobsView(
            jobs=[
                JobSummaryView(
                    job_id=_JOB_ID,
                    kind="VERIFY",
                    state="RUNNING",
                    created_at=_NOW,
                    updated_at=_NOW,
                    can_cancel=True,
                )
            ],
            queued=0,
            running=1,
            attention=0,
            succeeded=0,
        )

    def get_job(self, job_id: str) -> JobRecord:
        assert job_id == _JOB_ID
        return self.record

    def events(self, job_id: str, *, after: int = 0, limit: int = 200) -> list[JobEvent]:
        assert (job_id, after, limit) == (_JOB_ID, 0, 200)
        return [
            JobEvent(
                job_id=_JOB_ID,
                sequence=1,
                timestamp=_NOW,
                level="INFO",
                message="Checking the apps node",
                payload={"private_target": "10.0.0.8"},
            )
        ]

    def cancel(self, job_id: str) -> JobRecord:
        assert job_id == _JOB_ID
        self.cancelled.append(job_id)
        return self.record.model_copy(update={"state": JobState.CANCEL_REQUESTED})

    def close(self) -> None:
        return None


def _create_app(tmp_path: Path, monkeypatch, controller: JobsController) -> FastAPI:
    (tmp_path / "config.yaml").write_text(
        "domain: test.example.com\nemail: owner@example.com\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WEBUI_SESSION_SECRET", "jobs-router-test-session-secret")
    monkeypatch.setattr("toolkit.webui.app.controller_client_from_environment", lambda: controller)
    monkeypatch.setattr(
        "toolkit.webui.routers.auth.verify_password",
        lambda _password, _client_ip, email="": (True, "Authenticated"),
    )
    from toolkit.webui.app import create_app

    return create_app(root=tmp_path)


@asynccontextmanager
async def _admin_client(app: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as client:
        response = await client.post("/login", data={"password": ""}, follow_redirects=False)
        assert response.status_code == 303
        yield client


async def test_jobs_history_and_detail_render_safe_operational_views(tmp_path: Path, monkeypatch) -> None:
    app = _create_app(tmp_path, monkeypatch, JobsController())
    async with _admin_client(app) as client:
        history = await client.get("/jobs")
        detail = await client.get(f"/jobs/{_JOB_ID}")

    assert history.status_code == 200
    assert "1 active" in history.text
    assert _JOB_ID in history.text
    assert "Verify" in history.text
    assert 'data-table-filter="job-history-body"' in history.text
    assert detail.status_code == 200
    assert "Checking the apps node" in detail.text
    assert 'data-stream-url="/jobs/job-123456789012/stream?after=1"' in detail.text
    assert "private_target" not in detail.text
    assert "10.0.0.8" not in detail.text
    assert "jobs-router-request" not in detail.text
    assert "ui:homelab-ui" not in detail.text


async def test_job_cancellation_uses_csrf_and_redirects_with_feedback(tmp_path: Path, monkeypatch) -> None:
    controller = JobsController()
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        detail = await client.get(f"/jobs/{_JOB_ID}")
        match = re.search(r'<meta name="csrf-token" content="([^"]+)"', detail.text)
        assert match is not None
        response = await client.post(
            f"/jobs/{_JOB_ID}/cancel",
            data={"csrf_token": match.group(1)},
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert response.headers["location"] == f"/jobs/{_JOB_ID}?flash=Cancellation+requested"
    assert controller.cancelled == [_JOB_ID]


async def test_jobs_controller_failure_is_bounded(tmp_path: Path, monkeypatch) -> None:
    controller = JobsController()
    controller.unavailable = True
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        response = await client.get("/jobs", follow_redirects=False)

    assert response.status_code == 503
    assert response.headers.get("location") is None
    assert "temporarily unavailable" in response.text
