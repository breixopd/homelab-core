from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from toolkit.controller.client import ControllerClientError, ControllerRejectedError
from toolkit.controller.read_models import (
    BootstrapDesiredState,
    BootstrapInitializeRequest,
    BootstrapServiceSecretView,
    BootstrapServiceSettingView,
    BootstrapStatus,
    BootstrapView,
)

router = APIRouter(tags=["setup"])
_SETUP_HEADERS = {
    "Cache-Control": "no-store",
    "Pragma": "no-cache",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self'; script-src 'self'; "
        "img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
}


def _protect(response: Response) -> Response:
    response.headers.update(_SETUP_HEADERS)
    return response


def _html(message: str, *, status_code: int) -> Response:
    return _protect(HTMLResponse(message, status_code=status_code))


def _redirect(location: str) -> Response:
    return _protect(RedirectResponse(location, status_code=303))


def _complete_bootstrap_session(request: Request) -> Response:
    now = int(time.time())
    request.session.pop("bootstrap_grant", None)
    request.session.pop("bootstrap_initialization_pending", None)
    request.session["bootstrap_deploy_started_at"] = now
    request.session["bootstrap_deploy_expires_at"] = now + 30 * 60
    return _redirect("/deploy?setup=1")


def _template_context(
    request: Request,
    *,
    status: BootstrapStatus,
    view: BootstrapView | None = None,
    error: str = "",
) -> dict[str, Any]:
    return {
        "request": request,
        "page_title": "Homelab setup",
        "active": "setup",
        "user_label": None,
        "status": status,
        "needs_capability": view is None and status.phase == "uninitialized",
        "category_preview": view.categories if view else [],
        "service_settings": view.service_settings if view else [],
        "service_secrets": view.service_secrets if view else [],
        "error": error,
    }


def _render(
    request: Request,
    *,
    status: BootstrapStatus,
    view: BootstrapView | None = None,
    error: str = "",
    status_code: int = 200,
):
    return _protect(
        request.app.state.templates.TemplateResponse(
            request,
            "setup.html",
            _template_context(request, status=status, view=view, error=error),
            status_code=status_code,
        )
    )


async def _status(request: Request) -> BootstrapStatus | None:
    try:
        return await run_in_threadpool(request.app.state.controller.bootstrap_status)
    except ControllerClientError:
        return None


async def _view(request: Request, status: BootstrapStatus) -> BootstrapView | None:
    grant = str(request.session.get("bootstrap_grant") or "")
    if not grant:
        return None
    try:
        return await run_in_threadpool(request.app.state.controller.bootstrap_view, grant)
    except (ControllerClientError, ValueError):
        request.session.pop("bootstrap_grant", None)
        return None


@router.get("/setup", response_class=HTMLResponse)
async def setup_get(request: Request):
    status = await _status(request)
    if status is None:
        return _html("Setup status is temporarily unavailable", status_code=503)
    if status.phase == "ready":
        if request.session.get("bootstrap_initialization_pending") is True:
            return _complete_bootstrap_session(request)
        return _render(request, status=status, status_code=403)
    if status.phase == "recovery_required":
        return _render(request, status=status, status_code=409)
    return _render(request, status=status, view=await _view(request, status))


@router.post("/setup/session")
async def setup_session(request: Request):
    form = await request.form()
    capability = str(form.get("capability") or "").strip()
    try:
        grant = await run_in_threadpool(
            request.app.state.controller.exchange_bootstrap_capability,
            capability,
        )
    except (ControllerClientError, ValueError):
        status = await _status(request)
        if status is None:
            return _html("Setup status is temporarily unavailable", status_code=503)
        return _render(
            request,
            status=status,
            error="The setup capability is invalid or expired.",
            status_code=403,
        )
    request.session["bootstrap_grant"] = grant.session_token
    return _redirect("/setup")


def _text(form, name: str, default: str = "") -> str:
    return str(form.get(name) or default).strip()


def _checked(form, name: str) -> bool:
    return _text(form, name) == "on"


def _service_setting_values(
    form,
    definitions: list[BootstrapServiceSettingView],
) -> dict[str, dict[str, bool | int | float | str]]:
    values: dict[str, dict[str, bool | int | float | str]] = {}
    for setting in definitions:
        field = f"service_setting__{setting.service}__{setting.key}"
        if setting.type == "boolean":
            value: bool | int | float | str = _checked(form, field)
        else:
            raw = _text(form, field, str(setting.default))
            if setting.type == "number":
                number = float(raw)
                value = int(number) if number.is_integer() else number
            else:
                value = raw
            if setting.type == "select" and value not in setting.choices:
                raise ValueError(f"{setting.label} has an invalid selection.")
        values.setdefault(setting.service, {})[setting.key] = value
    return values


def _initialize_request(
    form,
    grant: str,
    service_settings: list[BootstrapServiceSettingView],
    service_secrets: list[BootstrapServiceSecretView],
) -> BootstrapInitializeRequest:
    owner_password = _text(form, "owner_password")
    owner_password_confirm = _text(form, "owner_password_confirm")
    if owner_password != owner_password_confirm:
        raise ValueError("Passwords do not match.")

    desired_state = BootstrapDesiredState.model_validate(
        {
            "deployment_mode": _text(form, "deployment_mode", "provision"),
            "domain": _text(form, "domain"),
            "email": _text(form, "email"),
            "timezone": _text(form, "timezone", "Europe/Madrid"),
            "proxmox_api_url": _text(form, "proxmox_api_url"),
            "proxmox_node": _text(form, "proxmox_node", "pve"),
            "proxmox_storage": _text(form, "proxmox_storage", "local-zfs"),
            "service_settings": _service_setting_values(form, service_settings),
        }
    )
    fields = {
        "CLOUDFLARE_API_TOKEN": "cloudflare_api_token",
        "CLOUDFLARE_ZONE_ID": "cloudflare_zone_id",
        "PROXMOX_API_TOKEN_ID": "proxmox_api_token_id",
        "PROXMOX_API_TOKEN_SECRET": "proxmox_api_token_secret",
        "SSO_USER_PASSWORD": "owner_password",
    }
    credential_values = {
        secret_name: value for secret_name, form_name in fields.items() if (value := _text(form, form_name))
    }
    credential_values.update(
        {secret.name: value for secret in service_secrets if (value := _text(form, f"bootstrap_secret__{secret.name}"))}
    )
    return BootstrapInitializeRequest(
        session_token=grant,
        desired_state=desired_state,
        credential_values=credential_values,
    )


async def _render_form_error(request: Request, message: str):
    status = await _status(request)
    if status is None:
        return _html("Setup status is temporarily unavailable", status_code=503)
    view = await _view(request, status)
    if view is None:
        return _redirect("/setup")
    return _render(request, status=status, view=view, error=message, status_code=400)


@router.post("/setup")
async def setup_post(request: Request):
    grant = str(request.session.get("bootstrap_grant") or "")
    if not grant:
        return _redirect("/setup")
    status = await _status(request)
    if status is None:
        return _html("Setup status is temporarily unavailable", status_code=503)
    view = await _view(request, status)
    if view is None:
        return _redirect("/setup")
    form = await request.form()
    try:
        initialize_request = _initialize_request(form, grant, view.service_settings, view.service_secrets)
    except (ValidationError, ValueError) as exc:
        message = "Setup values are invalid." if isinstance(exc, ValidationError) else str(exc)
        return await _render_form_error(request, message)

    try:
        request.session["bootstrap_initialization_pending"] = True
        await run_in_threadpool(
            request.app.state.controller.initialize_bootstrap,
            initialize_request,
        )
    except ControllerRejectedError:
        request.session.pop("bootstrap_initialization_pending", None)
        return await _render_form_error(request, "Initialization was rejected. Check the values and try again.")
    except ControllerClientError:
        status = await _status(request)
        if status is None or status.phase != "ready":
            return await _render_form_error(
                request,
                "The controller is still reconciling initialization. Reload this page to check completion.",
            )

    return _complete_bootstrap_session(request)
