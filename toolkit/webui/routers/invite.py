from __future__ import annotations

import urllib.parse

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from toolkit.controller.client import ControllerClientError, ControllerRejectedError
from toolkit.controller.read_models import InviteActivationRequest, InvitePreview

router = APIRouter(tags=["invite"])
_ACTIVATION_COOKIE = "homelab_invite_activation"


def _render(request: Request, name: str, **context):
    response = request.app.state.templates.TemplateResponse(request, name, context)
    return _protect(response)


def _protect(response):
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Cache-Control"] = "no-store"
    return response


def _redirect(location: str) -> RedirectResponse:
    return _protect(RedirectResponse(location, status_code=303))


def _clear_activation_cookie(response: RedirectResponse, *, secure: bool) -> None:
    response.delete_cookie(
        _ACTIVATION_COOKIE,
        path="/invite",
        secure=secure,
        httponly=True,
        samesite="strict",
    )


async def _preview(request: Request, token: str) -> InvitePreview:
    return await run_in_threadpool(request.app.state.controller.invite_preview, token)


def _invalid_context(preview: InvitePreview) -> dict:
    return {
        "page_title": "Invalid invite",
        "domain": preview.domain,
        "error": "This invite link is invalid or has expired. Ask the homelab owner to send a new invite.",
        "invite_valid": False,
        "activation_csrf": "",
        "sections": [],
        "display_name": "",
        "email": "",
    }


@router.get("/invite/activate", response_class=HTMLResponse)
async def invite_activate_get(request: Request, token: str = ""):
    cookie_token = request.cookies.get(_ACTIVATION_COOKIE, "")
    try:
        preview = await _preview(request, token or cookie_token)
    except ControllerClientError:
        return _protect(HTMLResponse("Invite service is temporarily unavailable", status_code=503))
    if token and preview.valid:
        response = _redirect("/invite/activate")
        response.set_cookie(
            _ACTIVATION_COOKIE,
            token,
            max_age=preview.cookie_max_age_seconds,
            path="/invite",
            secure=preview.secure_cookie,
            httponly=True,
            samesite="strict",
        )
        return response
    if not preview.valid:
        response = _render(request, "invite_activate.html", **_invalid_context(preview))
        if cookie_token:
            response.delete_cookie(
                _ACTIVATION_COOKIE,
                path="/invite",
                secure=preview.secure_cookie,
                httponly=True,
                samesite="strict",
            )
        return response
    return _render(
        request,
        "invite_activate.html",
        page_title="Activate your account",
        domain=preview.domain,
        error="",
        invite_valid=True,
        activation_csrf=preview.activation_csrf,
        display_name=preview.display_name,
        email=preview.email,
        sections=preview.sections,
    )


@router.get("/invite/activated", response_class=HTMLResponse)
async def invite_activated_get(request: Request):
    try:
        preview = await _preview(request, "")
    except ControllerClientError:
        return _protect(HTMLResponse("Invite service is temporarily unavailable", status_code=503))
    return _render(
        request,
        "invite_activate.html",
        page_title="You're all set",
        domain=preview.domain,
        error="",
        invite_valid=False,
        activation_csrf="",
        success=True,
        display_name="",
        email="",
        sections=[],
    )


@router.post("/invite/activate")
async def invite_activate_post(
    request: Request,
    activation_csrf: str = Form(...),
    password: str = Form(...),
    password_confirm: str = Form(...),
):
    token = request.cookies.get(_ACTIVATION_COOKIE, "")
    try:
        preview = await _preview(request, token)
    except ControllerClientError:
        return _redirect("/invite/activate?error=Invite+service+is+temporarily+unavailable.")
    if not token or not preview.valid:
        response = _redirect(
            "/invite/activate?error=" + urllib.parse.quote("This invite link is invalid, expired, or already used.")
        )
        _clear_activation_cookie(response, secure=preview.secure_cookie)
        return response
    if len(password) < 10:
        return _redirect("/invite/activate?error=" + urllib.parse.quote("Password must be at least 10 characters."))
    if len(password) > 128:
        return _redirect("/invite/activate?error=" + urllib.parse.quote("Password must be at most 128 characters."))
    if password != password_confirm:
        return _redirect("/invite/activate?error=" + urllib.parse.quote("Passwords do not match."))
    try:
        result = await run_in_threadpool(
            request.app.state.controller.activate_invite,
            InviteActivationRequest(
                token=token,
                activation_csrf=activation_csrf,
                origin=request.headers.get("origin", ""),
                password=password,
            ),
        )
    except ControllerRejectedError as exc:
        if exc.code == "FORBIDDEN":
            raise HTTPException(status_code=403, detail="Invite activation request was rejected") from exc
        return _redirect("/invite/activate?error=Account+activation+failed.+Ask+the+owner+for+a+new+invite.")
    except ControllerClientError:
        return _redirect("/invite/activate?error=Invite+service+is+temporarily+unavailable.")
    if result.outcome == "activated":
        response = _redirect("/invite/activated")
    else:
        response = _redirect("/invite/activate?error=Account+activation+failed.+Ask+the+owner+for+a+new+invite.")
    _clear_activation_cookie(response, secure=result.secure_cookie)
    return response
