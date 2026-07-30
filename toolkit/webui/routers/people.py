from __future__ import annotations

import uuid
from typing import Annotated, cast

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from toolkit.controller.client import ControllerClientError, ControllerRejectedError
from toolkit.controller.contracts import (
    DeleteDirectoryIdentityCommand,
    IdentityOperation,
    InviteUserCommand,
    JobRequest,
    ReprovisionUserCommand,
    ServiceGroupName,
    SetUserGroupsCommand,
)
from toolkit.webui.controller_sse import controller_event_stream
from toolkit.webui.templates_ctx import page_context

router = APIRouter(tags=["people"])

IdentityFormCommand = InviteUserCommand | SetUserGroupsCommand | ReprovisionUserCommand | DeleteDirectoryIdentityCommand


def _people_page(request: Request, view=None, *, directory_unavailable: bool = False) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "people.html",
        page_context(
            request,
            active="people",
            page_title="People",
            directory=view,
            directory_unavailable=directory_unavailable,
        ),
    )


def _identity_job_partial(request: Request, job_id: str, state: str) -> HTMLResponse:
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/identity_job.html",
        {
            "request": request,
            "job_id": job_id,
            "job_state": state,
        },
    )


def _invalid_request() -> HTMLResponse:
    return HTMLResponse("Identity request was invalid", status_code=400)


async def _submit_identity_job(request: Request, command: IdentityFormCommand) -> HTMLResponse:
    job_request = JobRequest(
        idempotency_key=str(uuid.uuid4()),
        operation=IdentityOperation(command=command),
    )
    try:
        job = await run_in_threadpool(request.app.state.controller.submit, job_request)
    except ControllerRejectedError as exc:
        if exc.status_code in {409, 429}:
            return HTMLResponse("Another identity mutation is already active", status_code=exc.status_code)
        return HTMLResponse("Identity request was rejected", status_code=400)
    except ControllerClientError:
        return HTMLResponse("Identity request could not be queued", status_code=503)
    return _identity_job_partial(request, job.job_id, job.state.value.lower())


@router.get("/people", response_class=HTMLResponse)
async def people_index(request: Request):
    try:
        view = await run_in_threadpool(request.app.state.controller.directory_users)
    except ControllerClientError:
        return _people_page(request, directory_unavailable=True)
    return _people_page(request, view)


@router.post("/people/jobs/invite", response_class=HTMLResponse)
async def invite_person(
    request: Request,
    email: Annotated[str, Form()] = "",
    display_name: Annotated[str, Form()] = "",
    groups: Annotated[list[str] | None, Form()] = None,
):
    try:
        command = InviteUserCommand(
            email=email,
            display_name=display_name,
            groups=cast(list[ServiceGroupName], groups or []),
        )
    except ValidationError:
        return _invalid_request()
    return await _submit_identity_job(request, command)


@router.post("/people/jobs/{user_id}/groups", response_class=HTMLResponse)
async def set_person_groups(
    request: Request,
    user_id: str,
    groups: Annotated[list[str] | None, Form()] = None,
):
    try:
        command = SetUserGroupsCommand(user_id=user_id, groups=cast(list[ServiceGroupName], groups or []))
    except ValidationError:
        return _invalid_request()
    return await _submit_identity_job(request, command)


@router.post("/people/jobs/{user_id}/reprovision", response_class=HTMLResponse)
async def reprovision_person(request: Request, user_id: str):
    try:
        command = ReprovisionUserCommand(user_id=user_id)
    except ValidationError:
        return _invalid_request()
    return await _submit_identity_job(request, command)


@router.post("/people/jobs/{user_id}/delete-directory", response_class=HTMLResponse)
async def delete_directory_identity(
    request: Request,
    user_id: str,
    confirmation: Annotated[str, Form()] = "",
):
    try:
        command = DeleteDirectoryIdentityCommand(user_id=user_id, confirmation=confirmation)
    except ValidationError:
        return _invalid_request()
    return await _submit_identity_job(request, command)


@router.get("/people/stream/{job_id}")
async def people_stream(request: Request, job_id: str):
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
