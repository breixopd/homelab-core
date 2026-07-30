from __future__ import annotations

from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from toolkit.controller.client import ControllerClientError, ControllerRejectedError
from toolkit.controller.contracts import TERMINAL_JOB_STATES
from toolkit.webui.controller_sse import controller_event_stream
from toolkit.webui.error_pages import render_error
from toolkit.webui.templates_ctx import page_context

router = APIRouter(tags=["jobs"])


@router.get("/jobs", response_class=HTMLResponse)
async def jobs_index(request: Request):
    try:
        view = await run_in_threadpool(request.app.state.controller.jobs, limit=100)
    except ControllerClientError:
        return render_error(request, title="Jobs unavailable", message="Job history is temporarily unavailable")
    return request.app.state.templates.TemplateResponse(
        request,
        "jobs.html",
        page_context(
            request,
            active="jobs",
            page_title="Jobs",
            jobs=view.jobs,
            queued=view.queued,
            running=view.running,
            attention=view.attention,
            succeeded=view.succeeded,
        ),
    )


@router.get("/jobs/{job_id}", response_class=HTMLResponse)
async def job_detail(request: Request, job_id: str):
    try:
        job, events = await run_in_threadpool(
            lambda: (
                request.app.state.controller.get_job(job_id),
                request.app.state.controller.events(job_id, after=0, limit=200),
            )
        )
    except ControllerRejectedError as exc:
        return HTMLResponse("Job was not found", status_code=404 if exc.code == "NOT_FOUND" else 409)
    except ControllerClientError:
        return render_error(request, title="Job details unavailable", message="Job details are temporarily unavailable")
    return request.app.state.templates.TemplateResponse(
        request,
        "job_detail.html",
        page_context(
            request,
            active="jobs",
            page_title=f"Job {job.job_id}",
            job_id=job.job_id,
            kind=job.request.kind,
            state=job.state,
            created_at=job.created_at,
            updated_at=job.updated_at,
            can_cancel=job.state not in TERMINAL_JOB_STATES,
            error_code=job.error.code if job.error else "",
            events=events,
            last_sequence=events[-1].sequence if events else 0,
            flash=request.query_params.get("flash"),
            error=request.query_params.get("error"),
        ),
    )


@router.post("/jobs/{job_id}/cancel")
async def cancel_job(request: Request, job_id: str):
    target = f"/jobs/{quote(job_id, safe='')}"
    try:
        await run_in_threadpool(request.app.state.controller.cancel, job_id)
    except ControllerRejectedError as exc:
        message = "Job was not found" if exc.code == "NOT_FOUND" else "Cancellation was rejected"
        return RedirectResponse(f"{target}?error={quote(message)}", status_code=303)
    except ControllerClientError:
        return RedirectResponse(f"{target}?error=Controller+is+temporarily+unavailable", status_code=303)
    return RedirectResponse(f"{target}?flash=Cancellation+requested", status_code=303)


@router.get("/jobs/{job_id}/stream")
async def job_stream(request: Request, job_id: str):
    raw_after = request.query_params.get("after") or request.headers.get("last-event-id") or "0"
    try:
        after = max(0, int(raw_after))
    except ValueError:
        after = 0
    return StreamingResponse(
        controller_event_stream(
            request.app.state.controller,
            job_id,
            after=after,
            include_progress=False,
        ),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
