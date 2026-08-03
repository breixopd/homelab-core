from __future__ import annotations

import uuid
from typing import Literal, cast
from urllib.parse import quote

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from toolkit.controller.client import ControllerClientError
from toolkit.controller.contracts import (
    BackupDrillOperation,
    HostReconcileOperation,
    HostRemoveOperation,
    JobRequest,
    MaintenanceOperation,
    RestoreDrillOperation,
    UpdateOperation,
)
from toolkit.controller.read_models import ManagedHostCreate, ManagedHostSpec, ManagedHostUpdate
from toolkit.webui.error_pages import render_error
from toolkit.webui.redirects import local_redirect_target
from toolkit.webui.templates_ctx import page_context

router = APIRouter(tags=["operations"])


async def _view(request: Request):
    return await run_in_threadpool(request.app.state.controller.operations_view)


async def _submit(request: Request, operation) -> RedirectResponse:
    job = await run_in_threadpool(
        request.app.state.controller.submit,
        JobRequest(idempotency_key=str(uuid.uuid4()), operation=operation),
    )
    return RedirectResponse(local_redirect_target(f"/jobs/{quote(job.job_id, safe='')}"), status_code=303)


@router.get("/operations", response_class=HTMLResponse)
async def operations_index(request: Request):
    try:
        view = await _view(request)
    except ControllerClientError:
        return render_error(
            request, title="Operations unavailable", message="Operations status is temporarily unavailable"
        )
    return request.app.state.templates.TemplateResponse(
        request,
        "operations.html",
        page_context(request, active="operations", page_title="Operations", operations=view),
    )


@router.post("/operations/maintenance")
async def run_maintenance(request: Request):
    try:
        return await _submit(request, MaintenanceOperation())
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse("/operations?error=Maintenance+was+rejected", status_code=303)


@router.post("/operations/backups/drill")
async def run_backup_drill(request: Request):
    try:
        return await _submit(request, BackupDrillOperation())
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse("/operations?error=Backup+drill+was+rejected", status_code=303)


@router.post("/operations/dumps/{dump_id}/drill")
async def run_restore_drill(request: Request, dump_id: str):
    try:
        return await _submit(request, RestoreDrillOperation(dump_id=dump_id))
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse("/operations?error=Restore+drill+was+rejected", status_code=303)


@router.post("/operations/updates/refresh")
async def refresh_updates(request: Request):
    try:
        return await _submit(request, UpdateOperation(action="refresh"))
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse("/operations?error=Update+check+was+rejected", status_code=303)


@router.post("/operations/updates/apply")
async def apply_updates(request: Request):
    try:
        form = await request.form()
        return await _submit(
            request,
            UpdateOperation(
                action="apply",
                revision=str(form.get("revision") or ""),
                services=[str(value) for value in form.getlist("services")],
            ),
        )
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse("/operations?error=Update+was+rejected", status_code=303)


@router.post("/operations/updates/rollback")
async def rollback_updates(request: Request):
    try:
        form = await request.form()
        return await _submit(
            request,
            UpdateOperation(action="rollback", revision=str(form.get("revision") or "")),
        )
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse("/operations?error=Rollback+was+rejected", status_code=303)


@router.post("/operations/updates/recover")
async def recover_updates(request: Request):
    try:
        return await _submit(request, UpdateOperation(action="recover"))
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse("/operations?error=Release+recovery+was+rejected", status_code=303)


def _host_spec(form, *, name: str | None = None) -> ManagedHostSpec:
    kind = cast(Literal["plain", "fleet"], str(form.get("kind") or "fleet"))
    tags = [value.strip() for value in str(form.get("headscale_tags") or "").split(",") if value.strip()]
    services = [str(value) for value in form.getlist("services")]
    from toolkit.controller.managed_hosts_api import parse_managed_host_integrations

    integrations = parse_managed_host_integrations(services, form.get)
    return ManagedHostSpec(
        name=name or str(form.get("name") or "").strip(),
        ip=str(form.get("ip") or "").strip(),
        kind=kind,
        ssh_user=str(form.get("ssh_user") or "root").strip(),
        ssh_port=int(str(form.get("ssh_port") or "22")),
        cluster_group=str(form.get("cluster_group") or "").strip() if kind == "fleet" else "",
        lldap_email=str(form.get("lldap_email") or "").strip() if kind == "fleet" else "",
        headscale_tags=tags if kind == "fleet" else [],
        services=services,
        integrations=integrations,
    )


@router.post("/operations/hosts")
async def create_managed_host(request: Request):
    try:
        form = await request.form()
        spec = _host_spec(form)
        await run_in_threadpool(
            request.app.state.controller.create_managed_host,
            ManagedHostCreate(
                expected_revision=str(form.get("revision") or ""),
                host=spec,
            ),
        )
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse("/operations?error=Managed+host+creation+was+rejected", status_code=303)
    try:
        return await _submit(request, HostReconcileOperation(host_name=spec.name))
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse(
            "/operations?error=Managed+host+was+saved+but+reconciliation+could+not+be+queued",
            status_code=303,
        )


@router.post("/operations/hosts/{host_name}/edit")
async def edit_managed_host(request: Request, host_name: str):
    try:
        form = await request.form()
        await run_in_threadpool(
            request.app.state.controller.update_managed_host,
            host_name,
            ManagedHostUpdate(
                expected_revision=str(form.get("revision") or ""),
                host=_host_spec(form, name=host_name),
            ),
        )
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse("/operations?error=Managed+host+update+was+rejected", status_code=303)
    try:
        return await _submit(request, HostReconcileOperation(host_name=host_name))
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse(
            "/operations?error=Managed+host+was+saved+but+reconciliation+could+not+be+queued",
            status_code=303,
        )


@router.post("/operations/hosts/{host_name}/reconcile")
async def reconcile_managed_host(request: Request, host_name: str):
    try:
        return await _submit(request, HostReconcileOperation(host_name=host_name))
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse("/operations?error=Managed+host+reconciliation+was+rejected", status_code=303)


@router.post("/operations/hosts/{host_name}/remove")
async def remove_managed_host(request: Request, host_name: str):
    try:
        form = await request.form()
        return await _submit(
            request,
            HostRemoveOperation(
                host_name=host_name,
                expected_fingerprint=str(form.get("fingerprint") or ""),
                confirmation=host_name,
            ),
        )
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse("/operations?error=Managed+host+removal+was+rejected", status_code=303)
