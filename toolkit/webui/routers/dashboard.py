from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from toolkit.controller.client import ControllerClientError
from toolkit.webui.error_pages import render_error
from toolkit.webui.rbac import homelab_tier_groups, is_family_portal_user, is_toolkit_admin
from toolkit.webui.templates_ctx import page_context

router = APIRouter(tags=["dashboard"])


async def _dashboard(request: Request, *, family: bool | None = None):
    is_family = is_family_portal_user(request) if family is None else family
    return await run_in_threadpool(
        request.app.state.controller.dashboard_view,
        family=is_family,
        groups=homelab_tier_groups(request),
    )


def _metric_context(metrics) -> dict:
    return {
        "cpu": metrics.cpu,
        "mem": metrics.memory,
        "disk": metrics.disk,
        "containers": metrics.containers,
        "targets_up": metrics.targets_up,
        "targets_down": metrics.targets_down,
        "cpu_history_data": [[point.timestamp_ms, point.value] for point in metrics.cpu_history],
        "memory_history_data": [[point.timestamp_ms, point.value] for point in metrics.memory_history],
        "disk_history_data": [[point.timestamp_ms, point.value] for point in metrics.disk_history],
    }


@router.get("/", response_class=HTMLResponse)
async def dashboard_index(request: Request):
    templates = request.app.state.templates
    family = is_family_portal_user(request)
    try:
        view = await _dashboard(request, family=family)
    except ControllerClientError:
        return render_error(request, title="Overview unavailable", message="Dashboard data is temporarily unavailable")

    if view.state == "uninitialized":
        if is_toolkit_admin(request):
            return RedirectResponse("/setup", status_code=303)
        if family:
            return templates.TemplateResponse(
                request,
                "family_dashboard.html",
                page_context(
                    request,
                    active="dashboard",
                    page_title="Home",
                    setup_in_progress=True,
                    bookmark_groups=[],
                    tier_labels=[],
                ),
            )
        return templates.TemplateResponse(
            request,
            "dashboard.html",
            page_context(
                request,
                active="dashboard",
                page_title="Overview",
                state=view.state,
                uninitialized=True,
            ),
        )

    if family:
        return templates.TemplateResponse(
            request,
            "family_dashboard.html",
            page_context(
                request,
                active="dashboard",
                page_title="Home",
                setup_in_progress=False,
                bookmark_groups=view.bookmark_groups,
                tier_labels=view.tier_labels,
            ),
        )

    status_text = "Configuration is ready for generation" if view.state == "config_only" else ""
    return templates.TemplateResponse(
        request,
        "dashboard.html",
        page_context(
            request,
            active="dashboard",
            page_title="Overview",
            state=view.state,
            uninitialized=False,
            last_verify=view.last_verify,
            next_action=view.next_action if is_toolkit_admin(request) else None,
            domain=view.domain,
            enabled_nodes=view.enabled_nodes,
            total_services=view.total_services,
            status_text=status_text,
            health=view.health,
            runtime=view.runtime,
            operations=view.operations,
            recent_jobs=view.recent_jobs,
            active_jobs=view.active_jobs,
            attention_jobs=view.attention_jobs,
            metrics=_metric_context(view.metrics),
            alerts=view.alerts,
            metrics_dashboard_href=view.metrics_dashboard_href,
        ),
    )


@router.get("/partials/dashboard/attention", response_class=HTMLResponse)
async def dashboard_attention_partial(request: Request):
    try:
        view = await _dashboard(request, family=False)
        alerts = view.alerts
        recent_jobs = view.recent_jobs
    except ControllerClientError:
        alerts = [{"severity": "warning", "message": "Dashboard status is unavailable", "href": ""}]
        recent_jobs = []
    return request.app.state.templates.TemplateResponse(
        request,
        "partials/dashboard_attention.html",
        {"request": request, "alerts": alerts, "recent_jobs": recent_jobs},
    )


@router.get("/api/dashboard/metrics")
async def dashboard_metrics_api(request: Request):
    try:
        metrics = await run_in_threadpool(request.app.state.controller.dashboard_metrics)
    except ControllerClientError:
        return JSONResponse({"error": "Dashboard metrics are unavailable"}, status_code=503)
    return {
        "cpu": metrics.cpu,
        "mem": metrics.memory,
        "disk": metrics.disk,
        "containers": metrics.containers,
        "targets_up": metrics.targets_up,
        "targets_down": metrics.targets_down,
        "cpuHistoryData": [[point.timestamp_ms, point.value] for point in metrics.cpu_history],
        "memoryHistoryData": [[point.timestamp_ms, point.value] for point in metrics.memory_history],
        "diskHistoryData": [[point.timestamp_ms, point.value] for point in metrics.disk_history],
    }


@router.get("/api/portal/status")
async def portal_status_api(request: Request):
    try:
        status = await run_in_threadpool(request.app.state.controller.portal_status)
    except ControllerClientError:
        return JSONResponse({"error": "Live service status is unavailable"}, status_code=503)
    return status.model_dump(mode="json")
