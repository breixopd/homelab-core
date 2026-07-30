from __future__ import annotations

import time
import uuid
from typing import cast

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from toolkit.controller.client import ControllerClientError, ControllerRejectedError
from toolkit.controller.contracts import (
    DeployOperation,
    GenerateOperation,
    JobRequest,
    MachineId,
    RecoverOperation,
    VerifyOperation,
)
from toolkit.webui.auth import bootstrap_deploy_authorized, is_authenticated
from toolkit.webui.controller_sse import controller_event_stream
from toolkit.webui.error_pages import render_error
from toolkit.webui.templates_ctx import page_context

router = APIRouter(tags=["deploy"])
_ACTIONS = frozenset({"deploy", "recover", "generate", "verify"})


def _bootstrap_job_scope(request: Request) -> set[str] | None:
    if is_authenticated(request) or not bootstrap_deploy_authorized(request):
        return None
    values = request.session.get("bootstrap_deploy_job_ids")
    return {str(value) for value in values} if isinstance(values, list) else set()


def _bind_bootstrap_job(request: Request, job_id: str) -> None:
    scope = _bootstrap_job_scope(request)
    if scope is None:
        return
    existing = request.session.get("bootstrap_deploy_job_ids")
    ordered = [str(value) for value in existing] if isinstance(existing, list) else []
    if job_id not in scope:
        ordered.append(job_id)
    request.session["bootstrap_deploy_job_ids"] = ordered[-4:]
    request.session.setdefault("bootstrap_deploy_job_started_at", int(time.time()))


async def _deployment_view(request: Request):
    return await run_in_threadpool(request.app.state.controller.deployment_view)


def _target(value: str, enabled: list[MachineId]) -> MachineId | None:
    stripped = value.strip()
    if not stripped:
        return None
    if stripped not in enabled:
        raise ValueError("Deployment target is not enabled")
    return cast(MachineId, stripped)


@router.get("/deploy", response_class=HTMLResponse)
async def deploy_index(request: Request):
    try:
        view = await _deployment_view(request)
    except ControllerClientError:
        return render_error(
            request, title="Deployments unavailable", message="Deployment status is temporarily unavailable"
        )
    bootstrap_scope = _bootstrap_job_scope(request)
    active_jobs = (
        [job for job in view.active_jobs if job.job_id in bootstrap_scope]
        if bootstrap_scope is not None
        else view.active_jobs
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "deploy.html",
        page_context(
            request,
            active="deploy",
            page_title="Deployments",
            enabled_targets=view.enabled_targets,
            step_labels=view.step_labels,
            preflight=view.preflight,
            items=view.preflight,
            preflight_ok=view.preflight_ok,
            total_services=view.total_services,
            envs_ready=view.generated_config_count,
            node_count=view.node_count,
            category_count=view.category_count,
            last_verify=view.last_verify,
            active_jobs=active_jobs,
        ),
    )


@router.get("/deploy/status")
async def deploy_status(request: Request):
    try:
        view = await _deployment_view(request)
    except ControllerClientError:
        return JSONResponse({"error": "Deployment status is unavailable"}, status_code=503)
    bootstrap_scope = _bootstrap_job_scope(request)
    active_jobs = (
        [job for job in view.active_jobs if job.job_id in bootstrap_scope]
        if bootstrap_scope is not None
        else view.active_jobs
    )
    return {
        "active": [job.model_dump(mode="json") for job in active_jobs],
        "preflight_ok": view.preflight_ok,
    }


@router.get("/partials/deploy/preflight", response_class=HTMLResponse)
async def deploy_preflight_partial(request: Request):
    try:
        view = await _deployment_view(request)
    except ControllerClientError:
        return render_error(
            request, title="Preflight unavailable", message="Preflight status is temporarily unavailable"
        )
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/preflight.html",
        {
            "request": request,
            "items": view.preflight,
            "preflight_ok": view.preflight_ok,
            "preflight_oob": True,
        },
    )


def _operation(
    action: str,
    *,
    target: MachineId | None,
    enabled_targets: list[MachineId],
    skip_infrastructure: bool,
    skip_dns: bool,
):
    if action == "deploy":
        return DeployOperation(
            target=target,
            skip_infrastructure=skip_infrastructure,
            skip_dns=skip_dns,
        )
    if action == "recover":
        return RecoverOperation(target=target)
    if action == "generate":
        return GenerateOperation(validate_output=True)
    if action == "verify":
        return VerifyOperation(targets=[target] if target else enabled_targets)
    raise ValueError("Unknown deployment action")


@router.post("/deploy/jobs/{action}", response_class=HTMLResponse)
async def start_deploy_job(
    request: Request,
    action: str,
    skip_infra: bool = Form(default=False),
    skip_dns: bool = Form(default=False),
    target_vm: str = Form(default=""),
):
    if action not in _ACTIONS:
        return HTMLResponse("Unknown deployment action", status_code=404)
    try:
        view = await _deployment_view(request)
        enabled_targets = list(view.enabled_targets)
        target = _target(target_vm, enabled_targets)
        operation = _operation(
            action,
            target=target,
            enabled_targets=enabled_targets,
            skip_infrastructure=skip_infra,
            skip_dns=skip_dns,
        )
        job = await run_in_threadpool(
            request.app.state.controller.submit,
            JobRequest(idempotency_key=str(uuid.uuid4()), operation=operation),
        )
    except ControllerRejectedError as exc:
        status_code = exc.status_code if exc.status_code in {409, 429} else 400
        return HTMLResponse("Deployment request was rejected", status_code=status_code)
    except (ControllerClientError, ValidationError, ValueError):
        return HTMLResponse("Deployment request could not be queued", status_code=503)

    _bind_bootstrap_job(request, job.job_id)
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/deploy_job.html",
        {
            "request": request,
            "job_id": job.job_id,
            "action": action,
            "step_labels": view.step_labels,
            "cancellable": True,
        },
    )


@router.post("/deploy/jobs/{job_id}/cancel", response_class=HTMLResponse)
async def cancel_deploy_job(request: Request, job_id: str):
    bootstrap_scope = _bootstrap_job_scope(request)
    if bootstrap_scope is not None and job_id not in bootstrap_scope:
        return HTMLResponse("Deployment job was not found", status_code=404)
    try:
        job = await run_in_threadpool(request.app.state.controller.cancel, job_id)
    except ControllerRejectedError as exc:
        status_code = 404 if exc.code == "NOT_FOUND" else 409
        return HTMLResponse("Cancellation was rejected", status_code=status_code)
    except ControllerClientError:
        return HTMLResponse("Cancellation could not reach the controller", status_code=503)
    return HTMLResponse(f'<span class="badge warn">{job.state.value.lower()}</span>')


@router.get("/deploy/stream/{job_id}")
async def deploy_stream(request: Request, job_id: str):
    bootstrap_scope = _bootstrap_job_scope(request)
    if bootstrap_scope is not None and job_id not in bootstrap_scope:
        return HTMLResponse("Deployment job was not found", status_code=404)
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
