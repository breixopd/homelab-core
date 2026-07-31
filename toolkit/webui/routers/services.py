from __future__ import annotations

import uuid
from typing import Literal, cast
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from toolkit.controller.client import ControllerClientError
from toolkit.controller.contracts import (
    ConfigApplyOperation,
    ContainerActionOperation,
    JobRequest,
    ServiceActionOperation,
    ServiceVerifyOperation,
)
from toolkit.controller.read_models import SecretUpdateRequest, ServiceSettingsUpdate
from toolkit.webui.error_pages import render_error
from toolkit.webui.rbac import (
    homelab_tier_groups,
    is_family_portal_user,
    is_toolkit_admin,
)
from toolkit.webui.templates_ctx import page_context

router = APIRouter(tags=["services"])


@router.get("/services", response_class=HTMLResponse)
async def services_index(request: Request):
    family = is_family_portal_user(request)
    groups = homelab_tier_groups(request)
    try:
        view = await run_in_threadpool(
            request.app.state.controller.services_view,
            family=family,
            groups=groups,
        )
    except ControllerClientError:
        return render_error(
            request, title="Services unavailable", message="Service inventory is temporarily unavailable"
        )

    template = "family_services.html" if family else "services.html"
    return request.app.state.templates.TemplateResponse(
        request,
        template,
        page_context(
            request,
            active="services",
            page_title="My apps" if family else "Services",
            categories=view.categories,
            sections=view.family_sections,
            tier_labels=view.tier_labels,
        ),
    )


async def _render_containers(
    request: Request,
    *,
    flash: str = "",
    flash_ok: bool = False,
) -> HTMLResponse:
    try:
        inventory = await run_in_threadpool(request.app.state.controller.container_inventory)
    except ControllerClientError:
        inventory = None
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/containers.html",
        {
            "request": request,
            "containers": inventory.containers if inventory else [],
            "docker_ok": inventory.is_available if inventory else False,
            "unavailable_nodes": inventory.unavailable_nodes if inventory else [],
            "flash": flash,
            "flash_ok": flash_ok,
        },
    )


@router.get("/partials/services/containers", response_class=HTMLResponse)
async def services_containers_partial(request: Request):
    return await _render_containers(request)


@router.post("/services/docker/{action}/{name}", response_class=HTMLResponse)
async def docker_action(request: Request, action: str, name: str):
    if not is_toolkit_admin(request):
        return HTMLResponse("Operator access required", status_code=403)
    if action not in {"start", "stop", "restart"}:
        return await _render_containers(request, flash="Container action was rejected")
    try:
        operation = ContainerActionOperation(
            service=name,
            action=cast(Literal["start", "stop", "restart"], action),
        )
        job = await run_in_threadpool(
            request.app.state.controller.submit,
            JobRequest(idempotency_key=str(uuid.uuid4()), operation=operation),
        )
    except (ControllerClientError, ValidationError, ValueError):
        return await _render_containers(request, flash="Container action was rejected")
    return await _render_containers(
        request,
        flash=f"{operation.action.title()} queued as job {job.job_id}",
        flash_ok=True,
    )


def _service_url(service: str, **query: str) -> str:
    base = f"/services/{quote(service, safe='')}"
    return f"{base}?{urlencode(query)}" if query else base


async def _service_management(request: Request, service: str, *, collect_status: bool = True):
    return await run_in_threadpool(
        request.app.state.controller.service_management,
        service,
        collect_status=collect_status,
    )


@router.get("/services/{service}", response_class=HTMLResponse)
async def service_management(request: Request, service: str):
    try:
        view = await _service_management(request, service, collect_status=False)
        verification = await run_in_threadpool(request.app.state.controller.service_verification, service)
    except ControllerClientError:
        return render_error(
            request,
            title="Service management unavailable",
            message="Service management is temporarily unavailable",
        )
    return request.app.state.templates.TemplateResponse(
        request,
        "service_management.html",
        page_context(
            request,
            active="services",
            page_title=view.label,
            service=view,
            flash=request.query_params.get("flash"),
            error=request.query_params.get("error"),
            job_id=request.query_params.get("job"),
            verification=verification,
        ),
    )


@router.post("/services/{service}/verification")
async def service_verification_start(request: Request, service: str):
    if not is_toolkit_admin(request):
        return HTMLResponse("Operator access required", status_code=403)
    try:
        operation = ServiceVerifyOperation(service=service)
        request_model = JobRequest(
            idempotency_key=f"service-verify-{service}-{uuid.uuid4()}",
            operation=operation,
        )
        job = await run_in_threadpool(request.app.state.controller.submit, request_model)
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse(_service_url(service, error="Verification could not be queued"), status_code=303)
    return RedirectResponse(_service_url(service, flash="Verification queued", job=job.job_id), status_code=303)


@router.get("/partials/services/{service}/observability", response_class=HTMLResponse)
async def service_observability_partial(request: Request, service: str):
    try:
        view = await _service_management(request, service)
    except ControllerClientError:
        return HTMLResponse(
            '<div class="notice error" role="alert">Live service data is temporarily unavailable.</div>',
        )
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/service_observability.html",
        {"request": request, "service": view},
    )


def _setting_values(form, settings) -> dict[str, bool | int | float | str]:
    values: dict[str, bool | int | float | str] = {}
    for setting in settings:
        field = f"setting_{setting.key}"
        if setting.type == "boolean":
            values[setting.key] = field in form
            continue
        raw = str(form.get(field) or "").strip()
        if setting.type == "number":
            if not raw:
                fallback = setting.value if setting.value is not None else setting.default
                if fallback is None:
                    raise ValueError("numeric setting requires a value")
                values[setting.key] = fallback
            else:
                values[setting.key] = float(raw) if any(char in raw.lower() for char in (".", "e")) else int(raw)
        else:
            values[setting.key] = raw
    return values


def _changed_setting_keys(values, settings) -> set[str]:
    return {
        setting.key
        for setting in settings
        if values[setting.key] != (setting.value if setting.value is not None else setting.default)
    }


@router.post("/services/{service}/settings")
async def service_settings_save(request: Request, service: str):
    job = None
    try:
        view = await _service_management(request, service)
        form = await request.form()
        values = _setting_values(form, view.settings)
        changed_keys = _changed_setting_keys(values, view.settings)
        if not changed_keys:
            return RedirectResponse(
                _service_url(service, flash="No settings changed"),
                status_code=303,
            )
        update = ServiceSettingsUpdate(
            expected_revision=str(form.get("revision") or ""),
            values=values,
        )
        updated = await run_in_threadpool(
            request.app.state.controller.update_service_settings,
            service,
            update,
        )
        if any(setting.key in changed_keys and setting.requires_redeploy for setting in view.settings):
            job = await run_in_threadpool(
                request.app.state.controller.submit,
                JobRequest(
                    idempotency_key=str(uuid.uuid4()),
                    operation=ConfigApplyOperation(revision_hash=updated.revision, service=service),
                ),
            )
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse(_service_url(service, error="Settings update was rejected"), status_code=303)
    flash = "Settings saved; service reconciliation queued" if job else "Settings saved"
    query = {"flash": flash}
    if job is not None:
        query["job"] = job.job_id
    return RedirectResponse(
        _service_url(service, **query),
        status_code=303,
    )


@router.post("/services/{service}/secrets")
async def service_secrets_save(request: Request, service: str):
    try:
        view = await _service_management(request, service)
        allowed = {field.name for field in view.secrets}
        if not allowed:
            raise ValueError("service has no credential fields")
        form = await request.form()
        values = {
            name: str(form.get(f"secret_{name}") or "").strip()
            for name in allowed
            if str(form.get(f"secret_{name}") or "").strip()
        }
        if not values:
            return RedirectResponse(_service_url(service, flash="No credential changes submitted"), status_code=303)
        await run_in_threadpool(
            request.app.state.controller.update_secrets,
            SecretUpdateRequest(values=values),
        )
        job = await run_in_threadpool(
            request.app.state.controller.submit,
            JobRequest(
                idempotency_key=str(uuid.uuid4()),
                operation=ConfigApplyOperation(revision_hash=view.revision, service=service),
            ),
        )
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse(_service_url(service, error="Credential update was rejected"), status_code=303)
    return RedirectResponse(
        _service_url(service, flash="Credentials saved; service reconciliation queued", job=job.job_id),
        status_code=303,
    )


@router.post("/services/{service}/actions/{action}")
async def service_action(request: Request, service: str, action: str):
    try:
        view = await _service_management(request, service)
        capability = next((item for item in view.actions if item.id == action and item.can_run), None)
        if capability is None:
            raise ValueError("service action is unavailable")
        job = await run_in_threadpool(
            request.app.state.controller.submit,
            JobRequest(
                idempotency_key=str(uuid.uuid4()),
                operation=ServiceActionOperation(service=service, action=action),
            ),
        )
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse(_service_url(service, error="Service action was rejected"), status_code=303)
    return RedirectResponse(
        _service_url(service, flash=f"{capability.label} queued", job=job.job_id),
        status_code=303,
    )
