from __future__ import annotations

import json
import re
import secrets
import uuid
from typing import Literal, cast
from urllib.parse import quote, quote_plus

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from toolkit.controller.client import ControllerClientError
from toolkit.controller.contracts import DestroyInfraOperation, DestroyPlanRequest, GenerateOperation, JobRequest
from toolkit.controller.read_models import MachineCreate, MachineRemove, MachineUpdate
from toolkit.core.machines import MachineSpec
from toolkit.webui.error_pages import render_error
from toolkit.webui.redirects import local_redirect_target as _local_redirect
from toolkit.webui.templates_ctx import page_context

router = APIRouter(tags=["machines"])


def _checked(form, key: str) -> bool:
    return form.get(key) == "on"


def _integer(form, key: str, default: int) -> int:
    raw = str(form.get(key) or "").strip()
    return int(raw) if raw else default


def _text(form, key: str, default: str = "") -> str:
    value = form.get(key)
    return str(value).strip() if value is not None else default


def _json_value(form, key: str, default):
    raw = _text(form, key)
    if not raw:
        return default
    value = json.loads(raw)
    if not isinstance(value, type(default)):
        raise ValueError(f"{key} has the wrong JSON type")
    return value


def _machine_spec(form) -> MachineSpec:
    kind = cast(Literal["lxc", "vm"], _text(form, "kind", "lxc"))
    labels = tuple(value.strip() for value in _text(form, "labels").split(",") if value.strip())
    vm = kind == "vm"
    return MachineSpec(
        kind=kind,
        provider="proxmox",
        enabled=_checked(form, "enabled"),
        managed=_checked(form, "managed"),
        hostname=_text(form, "hostname"),
        address=_text(form, "address"),
        vmid=_integer(form, "vmid", 900),
        description=_text(form, "description"),
        labels=labels,
        cores=_integer(form, "cores", 2),
        memory_mb=_integer(form, "memory_mb", 2_048),
        root_disk_gb=_integer(form, "root_disk_gb", 32),
        root_datastore=_text(form, "root_datastore"),
        data_disks=_json_value(form, "data_disks", []),
        private_bridge=_text(form, "private_bridge", "vmbr1"),
        public_bridge=_text(form, "public_bridge"),
        gateway=_text(form, "gateway"),
        cidr=_integer(form, "cidr", 24),
        startup_order=_integer(form, "startup_order", 20),
        nesting=_checked(form, "nesting"),
        keyctl=_checked(form, "keyctl"),
        fuse=_checked(form, "fuse"),
        template_file_id=_text(form, "template_file_id") if not vm else "",
        admin_user=_text(form, "admin_user") if vm else "",
        ssh_user=_text(form, "ssh_user"),
        ssh_port=_integer(form, "ssh_port", 22),
        cloud_image_datastore=_text(form, "cloud_image_datastore") if vm else "",
        cloud_image_format=cast(
            Literal["", "qcow2", "raw"],
            _text(form, "cloud_image_format") if vm else "",
        ),
        cloud_image_url=_text(form, "cloud_image_url") if vm else "",
        cloud_image_sha256=_text(form, "cloud_image_sha256") if vm else "",
        resource_limits=_json_value(form, "resource_limits", {}),
    )


async def _view(request: Request):
    return await run_in_threadpool(request.app.state.controller.machines_view)


async def _queue_generation(request: Request) -> RedirectResponse:
    job = await run_in_threadpool(
        request.app.state.controller.submit,
        JobRequest(idempotency_key=str(uuid.uuid4()), operation=GenerateOperation(validate_output=True)),
    )
    return RedirectResponse(_local_redirect(f"/jobs/{quote(job.job_id, safe='')}"), status_code=303)


def _flash(message: str) -> RedirectResponse:
    return RedirectResponse(f"/machines?flash={quote_plus(message[:500])}&ok=0", status_code=303)


def _rejected(action: str, exc: Exception) -> RedirectResponse:
    detail = str(exc).strip().splitlines()[0] if str(exc).strip() else "request was rejected"
    return _flash(f"Machine {action} failed: {detail}")


def _retirement_flash(machine_id: str, message: str) -> RedirectResponse:
    return RedirectResponse(
        _local_redirect(f"/machines/{quote(machine_id, safe='')}/retire?error={quote_plus(message[:500])}"),
        status_code=303,
    )


async def _queue_after_save(request: Request) -> RedirectResponse:
    try:
        return await _queue_generation(request)
    except ControllerClientError:
        return _flash("Machine was saved, but generation could not be queued. Retry generation from Deployments.")


def _document(spec: MachineSpec) -> dict:
    return spec.model_dump(mode="json")


@router.get("/machines", response_class=HTMLResponse)
async def machines_index(request: Request):
    try:
        view = await _view(request)
    except ControllerClientError:
        return render_error(
            request, title="Machines unavailable", message="Machine inventory is temporarily unavailable"
        )
    template_id = request.query_params.get("template", "")
    template = next((item for item in view.templates if item.template_id == template_id), None)
    draft = (
        template.spec
        if template is not None
        else MachineSpec(
            enabled=False,
            hostname="node-01",
            address="10.10.10.20",
            gateway="10.10.10.1",
            vmid=900,
            labels=("compute",),
        )
    )
    return request.app.state.templates.TemplateResponse(
        request,
        "machines.html",
        page_context(
            request,
            active="machines",
            page_title="Machines",
            view=view,
            draft=_document(draft),
            selected_template=template_id if template is not None else "",
            machine_documents={item.machine_id: _document(item.spec) for item in view.machines},
            flash=request.query_params.get("flash"),
            flash_ok=request.query_params.get("ok") == "1",
        ),
    )


@router.post("/machines")
async def create_machine(request: Request):
    try:
        form = await request.form()
        await run_in_threadpool(
            request.app.state.controller.create_machine,
            MachineCreate(
                expected_revision=_text(form, "revision"),
                machine_id=_text(form, "machine_id"),
                spec=_machine_spec(form),
            ),
        )
    except (ControllerClientError, ValidationError, ValueError) as exc:
        return _rejected("creation", exc)
    return await _queue_after_save(request)


@router.post("/machines/{machine_id}")
async def edit_machine(request: Request, machine_id: str):
    try:
        form = await request.form()
        await run_in_threadpool(
            request.app.state.controller.update_machine,
            machine_id,
            MachineUpdate(expected_revision=_text(form, "revision"), spec=_machine_spec(form)),
        )
    except (ControllerClientError, ValidationError, ValueError) as exc:
        return _rejected("update", exc)
    return await _queue_after_save(request)


@router.post("/machines/{machine_id}/remove")
async def remove_machine(request: Request, machine_id: str):
    try:
        form = await request.form()
        await run_in_threadpool(
            request.app.state.controller.remove_machine,
            MachineRemove(
                expected_revision=_text(form, "revision"),
                machine_id=machine_id,
                confirmation=_text(form, "confirmation"),
            ),
        )
    except (ControllerClientError, ValidationError, ValueError) as exc:
        return _rejected("removal", exc)
    return await _queue_after_save(request)


@router.get("/machines/{machine_id}/retire", response_class=HTMLResponse)
async def retirement_review(request: Request, machine_id: str):
    try:
        view = await _view(request)
        machine = next((item for item in view.machines if item.machine_id == machine_id), None)
        if machine is None:
            return HTMLResponse("Machine was not found", status_code=404)
        plan_id = request.query_params.get("plan", "")
        plan = None
        if plan_id:
            if not re.fullmatch(r"[A-Za-z0-9-]{16,128}", plan_id):
                return _retirement_flash(machine_id, "Retirement plan ID is invalid")
            plan = await run_in_threadpool(request.app.state.controller.get_plan, plan_id)
            if plan.spec.action != "retire_machine" or plan.spec.scopes != [machine_id]:
                return _retirement_flash(machine_id, "Retirement plan does not match this machine")
    except ControllerClientError as exc:
        return _retirement_flash(machine_id, str(exc))
    return request.app.state.templates.TemplateResponse(
        request,
        "machine_retire.html",
        page_context(
            request,
            active="machines",
            page_title=f"Retire {machine_id}",
            machine=machine,
            plan=plan,
            expected_confirmation=f"RETIRE MACHINE {machine_id}",
            error=request.query_params.get("error"),
        ),
    )


@router.post("/machines/{machine_id}/retirement-plan")
async def create_retirement_plan(request: Request, machine_id: str):
    try:
        plan = await run_in_threadpool(
            request.app.state.controller.create_destruction_plan,
            DestroyPlanRequest(action="retire_machine", scopes=[machine_id]),
        )
    except (ControllerClientError, ValidationError, ValueError) as exc:
        return _retirement_flash(machine_id, str(exc) or "Retirement plan was rejected")
    return RedirectResponse(
        _local_redirect(f"/machines/{quote(machine_id, safe='')}/retire?plan={quote(plan.plan_id, safe='')}"),
        status_code=303,
    )


@router.post("/machines/{machine_id}/retire")
async def retire_machine(request: Request, machine_id: str):
    try:
        form = await request.form()
        plan_id = _text(form, "plan_id")
        if not re.fullmatch(r"[A-Za-z0-9-]{16,128}", plan_id):
            raise ValueError("Retirement plan ID is invalid")
        plan = await run_in_threadpool(request.app.state.controller.get_plan, plan_id)
        if plan.spec.action != "retire_machine" or plan.spec.scopes != [machine_id]:
            raise ValueError("Retirement plan does not match this machine")
        confirmation = _text(form, "confirmation")
        expected = f"RETIRE MACHINE {machine_id}"
        if not secrets.compare_digest(confirmation, expected):
            raise ValueError("Typed retirement confirmation does not match")
        approval = await run_in_threadpool(
            request.app.state.controller.approve_plan,
            plan.plan_id,
            plan_hash=plan.plan_hash,
            confirmation=expected,
        )
        job = await run_in_threadpool(
            request.app.state.controller.submit,
            JobRequest(
                idempotency_key=f"retire-{uuid.uuid4().hex}",
                operation=DestroyInfraOperation(
                    action=plan.spec.action,
                    scopes=plan.spec.scopes,
                    config_revision=plan.spec.config_revision,
                    plan_id=plan.plan_id,
                    plan_hash=plan.plan_hash,
                    approval_token=approval.token,
                ),
            ),
        )
    except (ControllerClientError, ValidationError, ValueError) as exc:
        return _retirement_flash(machine_id, str(exc) or "Machine retirement was rejected")
    return RedirectResponse(_local_redirect(f"/jobs/{quote(job.job_id, safe='')}"), status_code=303)
