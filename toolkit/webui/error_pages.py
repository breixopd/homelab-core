"""Consistent, privacy-safe error responses for the Web UI."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse


def render_error(
    request: Request,
    *,
    title: str,
    message: str,
    status_code: int = 503,
    retry_url: str | None = None,
) -> HTMLResponse:
    """Render a themed page or HTMX fragment without exposing exception details."""
    retry = retry_url or request.url.path
    if request.url.query:
        retry = f"{retry}?{request.url.query}"
    is_htmx = request.headers.get("HX-Request", "").lower() == "true"
    context = {
        "request": request,
        "title": title,
        "message": message,
        "status_code": status_code,
        "retry_url": retry,
        "status_label": "Temporarily unavailable" if status_code >= 500 else "Request could not be completed",
    }
    template = "partials/error_fragment.html" if is_htmx else "error.html"
    if not is_htmx:
        from toolkit.webui.templates_ctx import page_context

        context = page_context(
            request,
            page_title=title,
            **{key: value for key, value in context.items() if key != "request"},
        )
    response = request.app.state.templates.TemplateResponse(request, template, context)
    response.status_code = status_code
    return response
