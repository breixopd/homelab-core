from __future__ import annotations

import uuid
from typing import Literal

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from toolkit.controller.client import ControllerClientError
from toolkit.controller.contracts import DnsSyncOperation, JobRequest
from toolkit.controller.read_models import DnsIpUpdate
from toolkit.webui.controller_sse import controller_event_stream
from toolkit.webui.error_pages import render_error
from toolkit.webui.templates_ctx import page_context

router = APIRouter(tags=["dns"])


@router.get("/dns", response_class=HTMLResponse)
async def dns_index(request: Request):
    try:
        view = await run_in_threadpool(request.app.state.controller.dns_view)
    except ControllerClientError:
        return render_error(
            request, title="Network unavailable", message="DNS desired state is temporarily unavailable"
        )
    return request.app.state.templates.TemplateResponse(
        request,
        "dns.html",
        page_context(
            request,
            active="dns",
            page_title="Network",
            revision=view.revision,
            public_ip=view.public_ip,
            ip_source=view.ip_source,
            records=view.records,
            has_cloudflare=view.has_cloudflare_credentials,
            flash=request.query_params.get("flash"),
            error=request.query_params.get("error"),
        ),
    )


@router.post("/dns/save-ip")
async def dns_save_ip(
    request: Request,
    public_ip: str = Form(default=""),
    revision: str = Form(default=""),
):
    try:
        await run_in_threadpool(
            request.app.state.controller.update_dns_public_ip,
            DnsIpUpdate(expected_revision=revision, public_ip=public_ip),
        )
    except (ControllerClientError, ValueError):
        return RedirectResponse("/dns?error=Public+IP+update+was+rejected", status_code=303)
    return RedirectResponse("/dns?flash=Public+IP+saved", status_code=303)


async def _start_dns_job(request: Request, action: Literal["sync", "cleanup"]):
    operation = DnsSyncOperation(action=action, dry_run=False)
    try:
        job = await run_in_threadpool(
            request.app.state.controller.submit,
            JobRequest(idempotency_key=str(uuid.uuid4()), operation=operation),
        )
    except (ControllerClientError, ValueError):
        return HTMLResponse("DNS operation was rejected", status_code=503)
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/dns_job.html",
        {"request": request, "job_id": job.job_id, "job_label": f"DNS {action}"},
    )


@router.post("/dns/sync")
async def dns_sync_start(request: Request):
    return await _start_dns_job(request, "sync")


@router.post("/dns/cleanup")
async def dns_cleanup(request: Request):
    return await _start_dns_job(request, "cleanup")


@router.get("/dns/stream/{job_id}")
async def dns_stream(request: Request, job_id: str):
    raw_after = request.query_params.get("after") or request.headers.get("last-event-id") or "0"
    try:
        after = max(0, int(raw_after))
    except ValueError:
        after = 0
    return StreamingResponse(
        controller_event_stream(request.app.state.controller, job_id, after=after),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
