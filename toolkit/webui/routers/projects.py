from __future__ import annotations

import uuid
from typing import Literal, cast

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from toolkit.controller.client import ControllerClientError
from toolkit.controller.contracts import ContainerActionOperation, DeployOperation, JobRequest
from toolkit.controller.read_models import ProjectCreate, ProjectDefinition, ProjectRemove
from toolkit.webui.templates_ctx import page_context

router = APIRouter(tags=["projects"])


@router.get("/projects", response_class=HTMLResponse)
async def projects_index(request: Request):
    try:
        view = await run_in_threadpool(request.app.state.controller.projects_view)
    except ControllerClientError:
        return HTMLResponse("Projects are temporarily unavailable", status_code=503)
    try:
        inventory = await run_in_threadpool(request.app.state.controller.container_inventory)
    except ControllerClientError:
        inventory = None
    runtime = {container.name: container for container in (inventory.containers if inventory else [])}
    return request.app.state.templates.TemplateResponse(
        request,
        "projects.html",
        page_context(
            request,
            active="projects",
            page_title="Projects",
            revision=view.revision,
            domain=view.domain,
            projects=view.projects,
            available_placements=view.available_placements,
            available_databases=view.available_databases,
            project_runtime=runtime,
            flash=request.query_params.get("flash"),
            flash_ok=request.query_params.get("ok") == "1",
        ),
    )


async def _queue_reconcile(request: Request) -> None:
    await run_in_threadpool(
        request.app.state.controller.submit,
        JobRequest(
            idempotency_key=str(uuid.uuid4()),
            operation=DeployOperation(skip_infrastructure=True),
        ),
    )


@router.post("/projects/add")
async def project_add(
    request: Request,
    revision: str = Form(...),
    name: str = Form(""),
    subdomain: str = Form(...),
    auth_mode: str = Form("forward_auth"),
    exposure: str = Form("private"),
    description: str = Form(""),
    show_on_portal: str = Form("true"),
    image: str = Form(...),
    port: int = Form(80),
    placement: str = Form(...),
    health_endpoint: str = Form(""),
    read_only: str = Form("true"),
    database_service: str = Form(""),
):
    try:
        normalized_subdomain = subdomain.strip()
        project = ProjectDefinition(
            name=name.strip() or subdomain.strip(),
            subdomain=normalized_subdomain,
            auth_mode=cast(Literal["forward_auth", "native"], auth_mode),
            exposure=cast(Literal["public", "private"], exposure),
            description=description.strip(),
            show_on_portal=show_on_portal == "true",
            docker_image=image.strip(),
            container_port=port,
            placement=placement,
            health_endpoint=health_endpoint.strip(),
            read_only=read_only == "true",
            database_service=database_service,
        )
        await run_in_threadpool(
            request.app.state.controller.create_project,
            ProjectCreate(expected_revision=revision, project=project),
        )
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse("/projects?flash=Project+was+rejected&ok=0", status_code=303)
    try:
        await _queue_reconcile(request)
    except ControllerClientError:
        return RedirectResponse("/projects?flash=Project+saved;+deployment+queue+unavailable&ok=0", status_code=303)
    return RedirectResponse("/projects?flash=Project+saved;+deployment+queued&ok=1", status_code=303)


@router.post("/projects/remove/{subdomain}")
async def project_remove(request: Request, subdomain: str, revision: str = Form(...)):
    try:
        await run_in_threadpool(
            request.app.state.controller.remove_project,
            ProjectRemove(expected_revision=revision, subdomain=subdomain),
        )
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse("/projects?flash=Project+removal+was+rejected&ok=0", status_code=303)
    try:
        await _queue_reconcile(request)
    except ControllerClientError:
        return RedirectResponse("/projects?flash=Project+removed;+deployment+queue+unavailable&ok=0", status_code=303)
    return RedirectResponse("/projects?flash=Project+removed;+deployment+queued&ok=1", status_code=303)


@router.post("/projects/action/{subdomain}/{action}")
async def project_action(request: Request, subdomain: str, action: str):
    if action not in {"start", "stop", "restart"}:
        return RedirectResponse("/projects?flash=Project+action+was+rejected&ok=0", status_code=303)
    try:
        view = await run_in_threadpool(request.app.state.controller.projects_view)
        if not any(project.subdomain == subdomain for project in view.projects):
            raise ValueError("project is not registered")
        operation = ContainerActionOperation(
            service=f"project-{subdomain}",
            action=cast(Literal["start", "stop", "restart"], action),
        )
        await run_in_threadpool(
            request.app.state.controller.submit,
            JobRequest(idempotency_key=str(uuid.uuid4()), operation=operation),
        )
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse("/projects?flash=Project+action+was+rejected&ok=0", status_code=303)
    return RedirectResponse(f"/projects?flash={action.title()}+queued&ok=1", status_code=303)
