from __future__ import annotations

from types import SimpleNamespace

import pytest
from toolkit.controller.contracts import JobState
from toolkit.webui.controller_sse import controller_event_stream


@pytest.mark.anyio
async def test_controller_sse_replays_sequence_and_reports_terminal_truth() -> None:
    class Client:
        def events(self, _job_id, *, after: int, limit: int):
            assert after == 4
            assert limit == 200
            return [SimpleNamespace(sequence=5, message="DNS sync\ncomplete", payload={})]

        def get_job(self, _job_id):
            return SimpleNamespace(state=JobState.SUCCEEDED)

    chunks = [chunk async for chunk in controller_event_stream(Client(), "job-123", after=4)]

    assert chunks[0] == "id: 5\nevent: log\ndata: DNS sync complete\n\n"
    assert '"ok": true' in chunks[1]
    assert "Job completed" in chunks[1]


@pytest.mark.anyio
async def test_controller_sse_translates_structured_deploy_progress() -> None:
    class Client:
        def events(self, _job_id, *, after: int, limit: int):
            return [
                SimpleNamespace(
                    sequence=1,
                    message="Deployment step changed",
                    payload={"target": "apps", "step": "compose-apps", "state": "running"},
                ),
                SimpleNamespace(
                    sequence=2,
                    message="Waiting for compose health checks",
                    payload={"stage": "deploy"},
                ),
            ]

        def get_job(self, _job_id):
            return SimpleNamespace(state=JobState.SUCCEEDED)

    chunks = [chunk async for chunk in controller_event_stream(Client(), "job-123")]

    assert 'event: step\ndata: {"step": "compose-apps", "status": "running"}' in chunks[0]
    assert "event: progress" in chunks[1]
    assert '"step": "deploy"' in chunks[1]


@pytest.mark.anyio
async def test_controller_sse_can_suppress_structured_payloads_for_general_job_history() -> None:
    class Client:
        def events(self, _job_id, *, after: int, limit: int):
            return [
                SimpleNamespace(
                    sequence=1,
                    message="Checking the apps node",
                    payload={"private_target": "10.0.0.8", "stage": "verify"},
                )
            ]

        def get_job(self, _job_id):
            return SimpleNamespace(state=JobState.SUCCEEDED)

    chunks = [
        chunk
        async for chunk in controller_event_stream(
            Client(),
            "job-123",
            include_progress=False,
        )
    ]

    assert "Checking the apps node" in chunks[0]
    assert "private_target" not in "".join(chunks)
    assert "10.0.0.8" not in "".join(chunks)
    assert "event: progress" not in "".join(chunks)


@pytest.mark.anyio
async def test_controller_sse_reports_partial_failure_distinctly() -> None:
    class Client:
        def events(self, _job_id, *, after: int, limit: int):
            return []

        def get_job(self, _job_id):
            return SimpleNamespace(state=JobState.PARTIAL_FAILURE)

    chunks = [chunk async for chunk in controller_event_stream(Client(), "job-123")]

    assert '"ok": false' in chunks[-1]
    assert '"partial": true' in chunks[-1]
    assert "Job completed with partial failure" in chunks[-1]


@pytest.mark.anyio
async def test_terminal_controller_sse_drains_every_event_page_before_done() -> None:
    class Client:
        def __init__(self) -> None:
            self.job_reads = 0

        def events(self, _job_id, *, after: int, limit: int):
            assert limit == 200
            if after == 0:
                return [
                    SimpleNamespace(sequence=sequence, message=f"line {sequence}", payload={})
                    for sequence in range(1, 201)
                ]
            if after == 200:
                return [
                    SimpleNamespace(sequence=sequence, message=f"line {sequence}", payload={})
                    for sequence in range(201, 251)
                ]
            return []

        def get_job(self, _job_id):
            self.job_reads += 1
            return SimpleNamespace(state=JobState.SUCCEEDED)

    client = Client()
    chunks = [chunk async for chunk in controller_event_stream(client, "job-123")]

    assert len(chunks) == 251
    assert "line 250" in chunks[-2]
    assert "event: done" in chunks[-1]
    assert client.job_reads == 1
