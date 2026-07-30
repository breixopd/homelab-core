from __future__ import annotations

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from toolkit.webui import current_root
from toolkit.webui.auth import _client_ip, authelia_user, verify_password

router = APIRouter(tags=["auth"])


@router.get("/login", response_class=HTMLResponse)
async def login_page(request: Request):
    if authelia_user(request) or request.session.get("authenticated"):
        return RedirectResponse("/", status_code=303)
    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": None, "active": "login"},
    )


@router.post("/login")
async def login_submit(request: Request, password: str = Form(default="")):
    user = authelia_user(request)
    if user:
        request.session["authenticated"] = True
        request.session["authelia_user"] = user
        return RedirectResponse("/", status_code=303)

    from toolkit.core.config.config import load_config
    from toolkit.core.config.storage import config_path

    root = current_root()
    cfg = load_config(config_path(root))
    ok, message = verify_password(password, _client_ip(request), email=cfg.email or "")
    if ok:
        request.session["authenticated"] = True
        if cfg.email:
            request.session["authelia_user"] = cfg.email.split("@", 1)[0]
        return RedirectResponse("/", status_code=303)

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "login.html",
        {"error": message, "active": "login"},
        status_code=401,
    )


@router.post("/logout")
async def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)
