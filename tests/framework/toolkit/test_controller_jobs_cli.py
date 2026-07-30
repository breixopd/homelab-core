from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import MagicMock

import pytest
from toolkit.cli.controller_jobs import wait_for_controller_job
from toolkit.controller.contracts import JobEvent, JobRecord, JobRequest, JobState, VerifyOperation


def _job(state: JobState) -> JobRecord:
    now = datetime(2026, 7, 10, tzinfo=UTC)
    return JobRecord(
        job_id="job-123456789012",
        request=JobRequest(idempotency_key="request-12345678", operation=VerifyOperation()),
        state=state,
        actor="local:operator",
        created_at=now,
        updated_at=now,
    )


def test_wait_for_controller_job_replays_events_once_and_returns_terminal_job() -> None:
    client = MagicMock()
    client.events.side_effect = [
        [
            JobEvent(
                job_id="job-123456789012",
                sequence=1,
                timestamp=datetime(2026, 7, 10, tzinfo=UTC),
                level="INFO",
                message="Job started",
            )
        ],
        [],
    ]
    client.get_job.side_effect = [_job(JobState.RUNNING), _job(JobState.SUCCEEDED)]
    observed: list[str] = []

    result = wait_for_controller_job(
        client,
        "job-123456789012",
        on_event=lambda event: observed.append(event.message),
        poll_interval=0,
    )

    assert result.state is JobState.SUCCEEDED
    assert observed == ["Job started"]
    assert client.events.call_args_list[1].kwargs["after"] == 1


def test_wait_for_controller_job_cancels_on_interrupt(monkeypatch) -> None:
    client = MagicMock()
    client.events.return_value = []
    client.get_job.return_value = _job(JobState.RUNNING)
    monkeypatch.setattr("toolkit.cli.controller_jobs.time.sleep", MagicMock(side_effect=KeyboardInterrupt))

    with pytest.raises(KeyboardInterrupt):
        wait_for_controller_job(client, "job-123456789012", on_event=lambda _event: None)

    client.cancel.assert_called_once_with("job-123456789012")
