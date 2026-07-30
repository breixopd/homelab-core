from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse
from starlette.concurrency import run_in_threadpool

from toolkit.controller.client import ControllerClientError
from toolkit.webui.error_pages import render_error

router = APIRouter(tags=["account"])


@router.get("/account", response_class=HTMLResponse)
async def account_index(request: Request):
    from toolkit.webui.rbac import homelab_tier_groups
    from toolkit.webui.templates_ctx import page_context

    try:
        view = await run_in_threadpool(
            request.app.state.controller.account_view,
            groups=homelab_tier_groups(request),
        )
    except ControllerClientError:
        return render_error(request, title="Account unavailable", message="Account details are temporarily unavailable")
    email = request.session.get("authelia_email") or ""

    templates = request.app.state.templates
    return templates.TemplateResponse(
        request,
        "account.html",
        page_context(
            request,
            active="account",
            page_title="My account",
            email=email,
            tier_labels=view.tier_labels,
            sections=view.sections,
            auth_url=view.auth_url,
        ),
    )
