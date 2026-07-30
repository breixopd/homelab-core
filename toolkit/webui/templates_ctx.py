from __future__ import annotations

from fastapi import Request

from toolkit.webui.auth import session_user_label
from toolkit.webui.rbac import homelab_tier_groups, is_family_portal_user, is_toolkit_admin


def page_context(request: Request, **extra) -> dict:
    """Build standard template context for authenticated pages."""
    ctx = {
        "request": request,
        "active": extra.pop("active", ""),
        "user_label": session_user_label(request),
        "homelab_root": str(request.app.state.homelab_root),
        "page_title": extra.pop("page_title", "Homelab"),
        "is_admin": is_toolkit_admin(request),
        "is_family_portal": is_family_portal_user(request),
        "user_groups": homelab_tier_groups(request),
    }
    ctx.update(extra)
    return ctx
