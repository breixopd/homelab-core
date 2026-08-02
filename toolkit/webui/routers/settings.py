from __future__ import annotations

import uuid
from typing import Literal, cast
from urllib.parse import quote_plus

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from toolkit.controller.client import ControllerClientError, ControllerRejectedError
from toolkit.controller.contracts import GenerateOperation, JobRequest
from toolkit.controller.read_models import SettingsUpdate, SettingsValues
from toolkit.webui.error_pages import render_error
from toolkit.webui.templates_ctx import page_context

router = APIRouter(tags=["settings"])
_SMTP_FAILURE_MESSAGES = {
    "dns": "SMTP hostname could not be resolved.",
    "connect": "SMTP server could not be reached.",
    "ehlo": "SMTP server did not complete its handshake.",
    "tls": "SMTP TLS verification failed.",
    "auth": "SMTP authentication failed; check the username and app password.",
    "envelope": "SMTP rejected the From address; use the account address or a configured alias.",
    "config": "SMTP settings are incomplete or inconsistent.",
}


def _checked(form, key: str) -> bool:
    return form.get(key) == "on"


def _integer(form, key: str, default: int) -> int:
    raw = str(form.get(key) or "").strip()
    return int(raw) if raw else default


def _text(form, key: str, default: str) -> str:
    value = form.get(key)
    return str(value).strip() if value is not None else default


async def _view(request: Request):
    return await run_in_threadpool(request.app.state.controller.settings_view)


@router.get("/settings", response_class=HTMLResponse)
async def settings_index(request: Request):
    try:
        view = await _view(request)
    except ControllerClientError:
        return render_error(request, title="Settings unavailable", message="Settings are temporarily unavailable")
    return request.app.state.templates.TemplateResponse(
        request,
        "settings.html",
        page_context(
            request,
            active="settings",
            page_title="Settings",
            revision=view.revision,
            values=view.values,
            service_toggles=view.service_toggles,
            flash=request.query_params.get("flash"),
            flash_ok=request.query_params.get("ok") == "1",
        ),
    )


def _form_values(form, current: SettingsValues) -> SettingsValues:
    smtp_mode = cast(Literal["auto", "external", "disabled"], _text(form, "smtp_mode", current.smtp_mode))
    smtp_username = _text(form, "smtp_username", current.smtp_username)
    return SettingsValues(
        domain=str(form.get("domain") or current.domain).strip(),
        email=str(form.get("email") or current.email).strip(),
        timezone=str(form.get("timezone") or current.timezone).strip(),
        services={name: _checked(form, f"svc_{name}") for name in current.services},
        deploy_ntfy_url=_text(form, "deploy_ntfy_url", current.deploy_ntfy_url),
        smtp_mode=smtp_mode,
        smtp_host=_text(form, "smtp_host", current.smtp_host),
        smtp_port=_integer(form, "smtp_port", current.smtp_port),
        smtp_starttls=_checked(form, "smtp_starttls"),
        smtp_username=smtp_username,
        smtp_password_secret=current.smtp_password_secret,
        smtp_password_configured=current.smtp_password_configured,
        smtp_from_address=_text(form, "smtp_from_address", current.smtp_from_address),
        ssh_auth=cast(Literal["key", "password"], str(form.get("ssh_auth") or current.ssh_auth)),
        ssh_key_file=_text(form, "ssh_key_file", current.ssh_key_file),
        proxmox_api_url=str(form.get("proxmox_api_url") or current.proxmox_api_url).strip(),
        proxmox_control_host=str(form.get("proxmox_control_host") or "").strip(),
        proxmox_ssh_user=str(form.get("proxmox_ssh_user") or current.proxmox_ssh_user).strip(),
        proxmox_ssh_port=_integer(form, "proxmox_ssh_port", current.proxmox_ssh_port),
        proxmox_ssh_key_file=str(form.get("proxmox_ssh_key_file") or "").strip(),
        proxmox_ssh_connect_timeout=_integer(
            form,
            "proxmox_ssh_connect_timeout",
            current.proxmox_ssh_connect_timeout,
        ),
        proxmox_ssh_command_timeout=_integer(
            form,
            "proxmox_ssh_command_timeout",
            current.proxmox_ssh_command_timeout,
        ),
        proxmox_ssh_retries=_integer(form, "proxmox_ssh_retries", current.proxmox_ssh_retries),
        proxmox_node=str(form.get("proxmox_node") or current.proxmox_node).strip(),
        proxmox_storage=str(form.get("proxmox_storage") or current.proxmox_storage).strip(),
        proxmox_template_datastore=str(
            form.get("proxmox_template_datastore") or current.proxmox_template_datastore
        ).strip(),
        proxmox_template_url=str(form.get("proxmox_template_url") or current.proxmox_template_url).strip(),
        proxmox_template_checksum=str(
            form.get("proxmox_template_checksum") or current.proxmox_template_checksum
        ).strip(),
        proxmox_tls_ca_file=str(form.get("proxmox_tls_ca_file") or "").strip(),
        proxmox_provision_machines=_checked(form, "proxmox_provision_machines"),
        expose_internet=_checked(form, "expose_internet"),
        container_ipv4_cidr=str(form.get("container_ipv4_cidr") or current.container_ipv4_cidr).strip(),
        container_network_prefix=_integer(
            form,
            "container_network_prefix",
            current.container_network_prefix,
        ),
        dns_provider=str(form.get("dns_provider") or current.dns_provider).strip(),
        dns_public_ip=str(form.get("dns_public_ip") or "").strip(),
        dns_proxy=_checked(form, "dns_proxy"),
    )


async def _queue(request: Request, operation) -> None:
    await run_in_threadpool(
        request.app.state.controller.submit,
        JobRequest(idempotency_key=str(uuid.uuid4()), operation=operation),
    )


@router.post("/settings/save")
async def settings_save(request: Request):
    try:
        current = await _view(request)
        form = await request.form()
        values = _form_values(form, current.values)
        await run_in_threadpool(
            request.app.state.controller.update_settings,
            SettingsUpdate(
                expected_revision=str(form.get("revision") or ""),
                values=values,
                smtp_password=str(form.get("smtp_password") or ""),
            ),
        )
        await _queue(request, GenerateOperation(validate_output=True))
    except ControllerRejectedError as exc:
        if exc.details.get("field") == "smtp":
            message = _SMTP_FAILURE_MESSAGES.get(
                str(exc.details.get("stage") or ""),
                "SMTP settings could not be verified.",
            )
            return RedirectResponse(
                f"/settings?flash={quote_plus(message)}&ok=0",
                status_code=303,
            )
        return RedirectResponse("/settings?flash=Settings+update+was+rejected&ok=0", status_code=303)
    except (ControllerClientError, ValidationError, ValueError):
        return RedirectResponse("/settings?flash=Settings+update+was+rejected&ok=0", status_code=303)
    return RedirectResponse("/settings?flash=Saved;+generation+queued&ok=1", status_code=303)


@router.post("/settings/generate")
async def settings_generate(request: Request):
    try:
        await _queue(request, GenerateOperation(validate_output=True))
    except (ControllerClientError, ValueError):
        return RedirectResponse("/settings?flash=Generation+was+rejected&ok=0", status_code=303)
    return RedirectResponse("/settings?flash=Generation+queued&ok=1", status_code=303)
