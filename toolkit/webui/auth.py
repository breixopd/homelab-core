from __future__ import annotations

import ipaddress
import os
import re
import time
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from toolkit.webui import current_root

_login_attempts: dict[str, tuple[int, float]] = {}


def _linux_default_gateway_cidrs(route_table: str) -> tuple[ipaddress.IPv4Network, ...]:
    """Return exact IPv4 gateway addresses from a Linux route table."""
    networks: list[ipaddress.IPv4Network] = []
    for line in route_table.splitlines()[1:]:
        fields = line.split()
        if len(fields) < 4 or fields[1] != "00000000":
            continue
        try:
            flags = int(fields[3], 16)
            gateway = ipaddress.IPv4Address(int.from_bytes(bytes.fromhex(fields[2]), "little"))
        except (ValueError, ipaddress.AddressValueError):
            continue
        if flags & 0x2 and int(gateway):
            networks.append(ipaddress.IPv4Network(f"{gateway}/32"))
    return tuple(dict.fromkeys(networks))


def _trusted_proxy_cidrs() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = [
        ipaddress.ip_network(value.strip())
        for value in os.environ.get("HOMELAB_TRUSTED_PROXY_CIDRS", "").split(",")
        if value.strip()
    ]
    if os.environ.get("HOMELAB_TRUST_CONTAINER_GATEWAY", "").strip().lower() in {"1", "true", "yes"}:
        try:
            networks.extend(_linux_default_gateway_cidrs(Path("/proc/net/route").read_text(encoding="ascii")))
        except OSError:
            pass
    return tuple(dict.fromkeys(networks))


_TRUSTED_PROXY_CIDRS = _trusted_proxy_cidrs()

PUBLIC_PATHS = frozenset({"/login", "/setup", "/setup/session", "/static", "/health", "/api/webhooks/grafana-alert"})
PUBLIC_PREFIXES = ("/invite/",)
_BOOTSTRAP_DEPLOY_IDLE_SECONDS = 30 * 60
_BOOTSTRAP_DEPLOY_START_MAX_SECONDS = 60 * 60
_BOOTSTRAP_DEPLOY_JOB_MAX_SECONDS = 6 * 60 * 60
_BOOTSTRAP_DEPLOY_EXACT_ROUTES = frozenset(
    {
        ("GET", "/deploy"),
        ("GET", "/deploy/status"),
        ("GET", "/partials/deploy/preflight"),
        ("POST", "/logout"),
    }
)
_BOOTSTRAP_DEPLOY_JOB_PATH = re.compile(r"^/deploy/jobs/(?:deploy|recover|generate|verify)$")
_BOOTSTRAP_DEPLOY_CANCEL_PATH = re.compile(r"^/deploy/jobs/[0-9a-f-]{36}/cancel$")
_BOOTSTRAP_DEPLOY_STREAM_PATH = re.compile(r"^/deploy/stream/[A-Za-z0-9-]{1,128}$")


def _is_trusted_proxy(client_host: str) -> bool:
    try:
        ip = ipaddress.ip_address(client_host)
        return any(ip in network for network in _TRUSTED_PROXY_CIDRS)
    except ValueError:
        return False


def _client_ip(request: Request) -> str:
    client_host = request.client.host if request.client else "127.0.0.1"
    xff = request.headers.get("X-Forwarded-For", "")
    if xff and _is_trusted_proxy(client_host):
        return xff.split(",")[-1].strip()
    return client_host


def authelia_user(request: Request) -> str | None:
    """Return Authelia username when forwarded by a trusted proxy."""
    client_host = request.client.host if request.client else ""
    remote_user = request.headers.get("Remote-User") or request.headers.get("X-Authenticated-User")
    if remote_user and _is_trusted_proxy(client_host):
        return remote_user
    return None


def is_authenticated(request: Request) -> bool:
    if authelia_user(request):
        return True
    return bool(request.session.get("authenticated"))


def bootstrap_deploy_authorized(request: Request) -> bool:
    try:
        started_at = int(request.session.get("bootstrap_deploy_started_at") or 0)
        job_started_at = int(request.session.get("bootstrap_deploy_job_started_at") or 0)
        expires_at = int(request.session.get("bootstrap_deploy_expires_at") or 0)
    except (TypeError, ValueError):
        started_at = 0
        job_started_at = 0
        expires_at = 0
    now = int(time.time())
    absolute_expiry = (
        job_started_at + _BOOTSTRAP_DEPLOY_JOB_MAX_SECONDS
        if job_started_at > 0
        else started_at + _BOOTSTRAP_DEPLOY_START_MAX_SECONDS
    )
    if started_at > 0 and expires_at > now and absolute_expiry > now:
        return True
    request.session.pop("bootstrap_deploy_started_at", None)
    request.session.pop("bootstrap_deploy_job_started_at", None)
    request.session.pop("bootstrap_deploy_expires_at", None)
    request.session.pop("bootstrap_deploy_job_ids", None)
    return False


def _bootstrap_deploy_route_allowed(method: str, path: str) -> bool:
    method = method.upper()
    if (method, path) in _BOOTSTRAP_DEPLOY_EXACT_ROUTES:
        return True
    return bool(
        (method == "POST" and _BOOTSTRAP_DEPLOY_JOB_PATH.fullmatch(path))
        or (method == "POST" and _BOOTSTRAP_DEPLOY_CANCEL_PATH.fullmatch(path))
        or (method == "GET" and _BOOTSTRAP_DEPLOY_STREAM_PATH.fullmatch(path))
    )


def _touch_bootstrap_deploy_session(request: Request) -> None:
    started_at = int(request.session["bootstrap_deploy_started_at"])
    job_started_at = int(request.session.get("bootstrap_deploy_job_started_at") or 0)
    absolute_expiry = (
        job_started_at + _BOOTSTRAP_DEPLOY_JOB_MAX_SECONDS
        if job_started_at > 0
        else started_at + _BOOTSTRAP_DEPLOY_START_MAX_SECONDS
    )
    request.session["bootstrap_deploy_expires_at"] = min(
        int(time.time()) + _BOOTSTRAP_DEPLOY_IDLE_SECONDS,
        absolute_expiry,
    )


def require_auth(request: Request) -> None:
    if not is_authenticated(request):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")


AuthRequired = Annotated[None, Depends(require_auth)]


class AuthMiddleware:
    """Redirect unauthenticated browser requests to /login."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        from toolkit.webui.rbac import check_portal_access

        request = Request(scope, receive=receive)

        async def continue_request() -> None:
            await self.app(scope, receive, send)

        async def send_response(response: Response) -> None:
            await response(scope, receive, send)

        path = request.url.path
        if (
            path.startswith("/static")
            or path in PUBLIC_PATHS
            or any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)
        ):
            await continue_request()
            return
        if not is_authenticated(request) and bootstrap_deploy_authorized(request):
            if _bootstrap_deploy_route_allowed(request.method, path):
                _touch_bootstrap_deploy_session(request)
                await continue_request()
                return
            await send_response(RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER))
            return
        if path.startswith("/deploy/stream/") or path.startswith("/dns/stream/"):
            if not is_authenticated(request):
                await send_response(RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER))
                return
            denied = check_portal_access(request)
            if denied is not None:
                await send_response(denied)
                return
            await continue_request()
            return
        if request.method == "GET" and path.startswith("/api/"):
            if not is_authenticated(request):
                # Return a clean JSON 401 (not a raised HTTPException, which
                # propagates as a 500 server error in the middleware) so API
                # clients get a structured response + browsers can handle it.
                await send_response(
                    JSONResponse({"detail": "Not authenticated"}, status_code=status.HTTP_401_UNAUTHORIZED)
                )
                return
            denied = check_portal_access(request)
            if denied is not None:
                await send_response(
                    JSONResponse({"detail": "Admin access required"}, status_code=status.HTTP_403_FORBIDDEN)
                )
                return
            await continue_request()
            return
        user = authelia_user(request)
        if user:
            request.session["authenticated"] = True
            request.session["authelia_user"] = user
            if email := request.headers.get("Remote-Email"):
                request.session["authelia_email"] = email.strip()
        if not is_authenticated(request):
            await send_response(RedirectResponse("/login", status_code=status.HTTP_303_SEE_OTHER))
            return
        denied = check_portal_access(request)
        if denied is not None:
            await send_response(denied)
            return
        await continue_request()


def verify_password(password: str, client_ip: str, email: str = "") -> tuple[bool, str]:
    """Authenticate against LLDAP (the same directory as SSO + SSH).

    The owner's email comes from config.yaml. Wizard/localhost access is still
    allowed before SOPS/LLDAP exist (first-boot setup). When behind Authelia
    forward-auth, the Remote-User header short-circuits to auto-login before
    this is ever called.
    """
    now = time.time()
    attempts, last = _login_attempts.get(client_ip, (0, 0.0))
    if attempts >= 5 and now - last < 300:
        return False, "Too many attempts. Wait 5 minutes."

    if not email:
        return False, "No owner email configured — set config.yaml email or access via Authelia SSO."

    from toolkit.core.identity.lldap_client import LLDAPClient

    root = current_root()
    ok, msg = LLDAPClient.verify_user_password(email, password, root=root)
    if ok:
        _login_attempts.pop(client_ip, None)
        return True, msg
    _login_attempts[client_ip] = (attempts + 1, now)
    return False, "Invalid credentials"


def session_user_label(request: Request) -> str:
    return request.session.get("authelia_user") or "admin"
