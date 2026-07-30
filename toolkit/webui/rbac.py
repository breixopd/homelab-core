"""Role-based access for homelab-ui (operator vs family portal)."""

from __future__ import annotations

from fastapi import Request
from fastapi.responses import HTMLResponse, RedirectResponse
from starlette import status

from toolkit.core.identity.service_groups import ADMIN_SERVICE_GROUPS, HOMELAB_GROUP_NAMES
from toolkit.webui.auth import authelia_user, bootstrap_deploy_authorized, is_authenticated

ADMIN_GROUPS = frozenset({*ADMIN_SERVICE_GROUPS, "lldap_admin"})

# Family users: portal home, their apps, account settings.
FAMILY_ALLOWED_GET = frozenset({"/", "/services", "/account", "/api/portal/status"})
FAMILY_ALLOWED_POST = frozenset({"/logout"})

# Operator-only route prefixes (blocked for family users).
ADMIN_ONLY_PREFIXES = (
    "/deploy",
    "/jobs",
    "/dns",
    "/secrets",
    "/settings",
    "/machines",
    "/projects",
    "/people",
    "/api/",
    "/partials/dashboard",
    "/partials/services",
)


def authelia_groups(request: Request) -> list[str]:
    """LDAP/Authelia groups from Caddy forward-auth headers."""
    if not authelia_user(request):
        cached = request.session.get("authelia_groups")
        return list(cached) if isinstance(cached, list) else []
    raw = request.headers.get("Remote-Groups", "")
    groups = [g.strip() for g in raw.split(",") if g.strip()]
    request.session["authelia_groups"] = groups
    return groups


def homelab_tier_groups(request: Request) -> list[str]:
    """Return plugin-defined homelab access groups assigned to this user."""
    allowed = set(HOMELAB_GROUP_NAMES)
    return [group for group in authelia_groups(request) if group in allowed]


def is_toolkit_admin(request: Request) -> bool:
    """Return whether the request belongs to a toolkit operator."""
    if authelia_user(request):
        groups = authelia_groups(request)
        return bool(ADMIN_GROUPS.intersection(groups))
    if bootstrap_deploy_authorized(request):
        return True
    # Toolkit password session (owner / break-glass) is always admin.
    return bool(request.session.get("authenticated"))


def is_family_portal_user(request: Request) -> bool:
    return is_authenticated(request) and not is_toolkit_admin(request)


def family_route_allowed(method: str, path: str) -> bool:
    if method.upper() in {"GET", "HEAD"}:
        return path in FAMILY_ALLOWED_GET
    if method.upper() == "POST":
        return path in FAMILY_ALLOWED_POST
    return False


def admin_route_blocked(path: str) -> bool:
    return any(path.startswith(prefix) for prefix in ADMIN_ONLY_PREFIXES)


def check_portal_access(request: Request) -> RedirectResponse | HTMLResponse | None:
    """Return a response when access is denied; None when allowed."""
    if not is_authenticated(request):
        return None
    if is_toolkit_admin(request):
        return None
    path = request.url.path
    if family_route_allowed(request.method, path):
        return None
    if request.headers.get("HX-Request"):
        return HTMLResponse(
            '<p class="muted">This action requires operator access.</p>',
            status_code=status.HTTP_403_FORBIDDEN,
        )
    return RedirectResponse("/", status_code=status.HTTP_303_SEE_OTHER)
