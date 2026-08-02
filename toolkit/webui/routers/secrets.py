from __future__ import annotations

import re
import urllib.parse
import uuid

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from toolkit.controller.client import ControllerClientError
from toolkit.controller.contracts import JobRequest, SecretRotationOperation
from toolkit.controller.read_models import SecretUpdateRequest
from toolkit.core.async_utils import run_blocking
from toolkit.webui.error_pages import render_error
from toolkit.webui.redirects import local_redirect_target
from toolkit.webui.templates_ctx import page_context

router = APIRouter(tags=["secrets"])
_SECRET_FIELD = re.compile(r"^[A-Z][A-Z0-9_]{1,127}$")


def _redirect_with_error(message: str) -> RedirectResponse:
    return RedirectResponse(f"/secrets?error={urllib.parse.quote_plus(message)}", status_code=303)


def _redirect_with_flash(message: str) -> RedirectResponse:
    return RedirectResponse(f"/secrets?flash={urllib.parse.quote_plus(message)}", status_code=303)


def _change_message(action: str, changed_names: list[str]) -> str:
    count = len(changed_names)
    noun = "secret" if count == 1 else "secrets"
    return f"{action} {count} {noun}."


@router.get("/secrets", response_class=HTMLResponse)
async def secrets_index(request: Request):
    try:
        inventory = await run_blocking(request.app.state.controller.secret_inventory)
    except ControllerClientError:
        return render_error(request, title="Secrets unavailable", message="Secret inventory is temporarily unavailable")
    user_specs = [entry for entry in inventory.entries if entry.tier == "user"]
    generated_specs = [entry for entry in inventory.entries if entry.tier == "gen"]
    configured = {entry.name: entry.is_configured for entry in inventory.entries}
    return request.app.state.templates.TemplateResponse(
        request,
        "secrets.html",
        page_context(
            request,
            active="secrets",
            page_title="Secrets",
            owner_email=inventory.owner_email,
            user_specs=user_specs,
            gen_specs=generated_specs,
            configured=configured,
            storage_mode=inventory.storage_mode,
            encryption_available=inventory.encryption_available,
            flash=request.query_params.get("flash"),
            error=request.query_params.get("error"),
        ),
    )


@router.post("/secrets/save")
async def secrets_save(request: Request):
    form = await request.form()
    values = {
        str(name): str(value)
        for name, value in form.multi_items()
        if _SECRET_FIELD.fullmatch(str(name)) and str(value).strip()
    }
    if not values:
        return _redirect_with_flash("No credential changes submitted.")
    try:
        result = await run_blocking(request.app.state.controller.update_secrets, SecretUpdateRequest(values=values))
    except (ControllerClientError, ValueError):
        return _redirect_with_error("The secret update was rejected.")
    return _redirect_with_flash(_change_message("Saved", result.changed_names))


@router.post("/secrets/generate")
async def secrets_generate(request: Request):
    try:
        result = await run_blocking(request.app.state.controller.generate_secrets)
    except ControllerClientError:
        return _redirect_with_error("Secret generation could not be completed.")
    return _redirect_with_flash(_change_message("Generated", result.changed_names))


@router.post("/secrets/rotate")
async def secrets_rotate(request: Request):
    try:
        inventory = await run_blocking(request.app.state.controller.secret_inventory)
        names = [
            entry.name for entry in inventory.entries if entry.tier == "gen" and entry.rotation_policy != "persistent"
        ]
        if not names:
            return _redirect_with_flash("No automatically rotatable generated secrets are configured.")
        job = await run_blocking(
            request.app.state.controller.submit,
            JobRequest(
                idempotency_key=str(uuid.uuid4()),
                operation=SecretRotationOperation(secret_names=names),
            ),
        )
    except ControllerClientError:
        return _redirect_with_error("Secret rotation could not be completed.")
    return RedirectResponse(
        local_redirect_target(f"/jobs/{urllib.parse.quote(job.job_id, safe='')}"),
        status_code=303,
    )
