"""Shared CLI progress rendering for durable controller jobs."""

from __future__ import annotations

import time
from collections.abc import Callable

from toolkit.controller.client import ControllerClient
from toolkit.controller.contracts import TERMINAL_JOB_STATES, JobEvent, JobRecord


class ControllerJobTimeoutError(RuntimeError):
    pass


def wait_for_controller_job(
    client: ControllerClient,
    job_id: str,
    *,
    on_event: Callable[[JobEvent], None],
    poll_interval: float = 1.0,
    timeout_seconds: float = 1800,
) -> JobRecord:
    """Replay ordered events until a controller job reaches a terminal state."""
    if poll_interval < 0 or timeout_seconds <= 0:
        raise ValueError("controller polling intervals must be valid")
    after = 0
    deadline = time.monotonic() + timeout_seconds
    try:
        while True:
            for event in client.events(job_id, after=after, limit=200):
                on_event(event)
                after = max(after, event.sequence)
            job = client.get_job(job_id)
            if job.state in TERMINAL_JOB_STATES:
                return job
            if time.monotonic() >= deadline:
                client.cancel(job_id)
                raise ControllerJobTimeoutError("Controller job exceeded its execution deadline and was cancelled")
            time.sleep(poll_interval)
    except KeyboardInterrupt:
        client.cancel(job_id)
        raise
