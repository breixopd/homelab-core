"""SSE replay adapter for durable controller jobs."""

from __future__ import annotations

import asyncio
import json

from starlette.concurrency import run_in_threadpool

from toolkit.controller.contracts import TERMINAL_JOB_STATES


async def controller_event_stream(
    client,
    job_id: str,
    *,
    after: int = 0,
    include_progress: bool = True,
):
    sequence = max(0, after)
    page_limit = 200
    while True:
        events = await run_in_threadpool(client.events, job_id, after=sequence, limit=page_limit)
        for event in events:
            sequence = max(sequence, event.sequence)
            message = event.message.replace("\r", " ").replace("\n", " ")
            chunk = f"id: {event.sequence}\nevent: log\ndata: {message}\n\n"
            payload = event.payload
            if include_progress and {"step", "state"}.issubset(payload):
                step = {"step": payload["step"], "status": payload["state"]}
                chunk += f"event: step\ndata: {json.dumps(step)}\n\n"
            elif include_progress and (stage := payload.get("stage")):
                progress = {"step": stage}
                chunk += f"event: progress\ndata: {json.dumps(progress)}\n\n"
            elif include_progress and payload:
                chunk += f"event: progress\ndata: {json.dumps(payload)}\n\n"
            yield chunk
        if len(events) == page_limit:
            continue
        job = await run_in_threadpool(client.get_job, job_id)
        if job.state in TERMINAL_JOB_STATES:
            partial = job.state.value == "PARTIAL_FAILURE"
            if partial:
                message = "Job completed with partial failure"
            elif job.state.value == "SUCCEEDED":
                message = "Job completed"
            else:
                message = "Job failed"
            payload = {
                "ok": job.state.value == "SUCCEEDED",
                "partial": partial,
                "message": message,
            }
            yield f"event: done\ndata: {json.dumps(payload)}\n\n"
            return
        await asyncio.sleep(1)
