from __future__ import annotations

import hashlib
import logging
import os
import secrets
import stat
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from toolkit.controller.client import ControllerClientError, controller_client_from_environment
from toolkit.core.compose.registry import load_all
from toolkit.webui import init_webui
from toolkit.webui.auth import AuthMiddleware
from toolkit.webui.security import CSRFMiddleware, RequestBodyLimitMiddleware, SetupRequestGuardMiddleware

logger = logging.getLogger(__name__)

_PKG_DIR = Path(__file__).resolve().parent
_TEMPLATES = Jinja2Templates(directory=str(_PKG_DIR / "templates"))


def _static_revision() -> str:
    digest = hashlib.sha256()
    for path in sorted((_PKG_DIR / "static").rglob("*")):
        if path.is_file():
            digest.update(path.relative_to(_PKG_DIR).as_posix().encode())
            digest.update(path.read_bytes())
    return digest.hexdigest()[:12]


_TEMPLATES.env.globals["static_revision"] = _static_revision()


def _session_secret() -> str:
    configured = os.environ.get("WEBUI_SESSION_SECRET")
    if configured:
        return configured
    configured_path = os.environ.get("WEBUI_SESSION_SECRET_FILE", "").strip()
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    secret_path = Path(configured_path) if configured_path else state_home / "homelab-toolkit" / "webui_secret"
    if not secret_path.is_absolute():
        raise RuntimeError("WEBUI_SESSION_SECRET_FILE must be an absolute path")
    secret_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_mode = secret_path.parent.lstat().st_mode
    if stat.S_ISLNK(parent_mode) or not stat.S_ISDIR(parent_mode):
        raise RuntimeError("Web UI session secret parent must be a directory")

    def read_existing() -> str:
        descriptor = os.open(secret_path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & 0o077:
                raise RuntimeError("Web UI session secret must be an owner-only regular file")
            value = os.read(descriptor, 4097).decode("ascii").strip()
        finally:
            os.close(descriptor)
        if len(value) < 32 or len(value) > 4096:
            raise RuntimeError("Web UI session secret has an invalid length")
        return value

    try:
        return read_existing()
    except FileNotFoundError:
        pass
    value = secrets.token_hex(32)
    try:
        descriptor = os.open(
            secret_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o600,
        )
    except FileExistsError:
        return read_existing()
    try:
        remaining = memoryview(value.encode("ascii"))
        while remaining:
            remaining = remaining[os.write(descriptor, remaining) :]
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    return read_existing()


def _secure_session_cookies() -> bool:
    value = os.environ.get("WEBUI_SECURE_COOKIES", "true").strip().lower()
    if value in {"1", "true"}:
        return True
    if value in {"0", "false"}:
        return False
    raise RuntimeError("WEBUI_SECURE_COOKIES must be true, false, 1, or 0")


def create_app(root: Path | None = None) -> FastAPI:
    resolved_root = init_webui(root)
    load_all()

    controller = controller_client_from_environment()

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        controller.close()

    app = FastAPI(title="Homelab Toolkit", docs_url=None, redoc_url=None, lifespan=lifespan)
    app.add_middleware(AuthMiddleware)
    app.add_middleware(CSRFMiddleware)
    app.add_middleware(SetupRequestGuardMiddleware)
    app.add_middleware(RequestBodyLimitMiddleware)
    app.add_middleware(
        SessionMiddleware,
        secret_key=_session_secret(),
        session_cookie="homelab_webui",
        max_age=60 * 60 * 24 * 7,
        same_site="strict",
        https_only=_secure_session_cookies(),
    )

    app.mount("/static", StaticFiles(directory=str(_PKG_DIR / "static")), name="static")
    app.state.templates = _TEMPLATES
    app.state.homelab_root = resolved_root
    app.state.controller = controller

    from toolkit.webui.routers import (
        account,
        auth,
        dashboard,
        deploy,
        dns,
        graph,
        invite,
        jobs,
        machines,
        operations,
        people,
        projects,
        secrets,
        services,
        settings,
        setup,
        webhooks,
    )

    app.include_router(auth.router)
    app.include_router(dashboard.router)
    app.include_router(services.router)
    app.include_router(projects.router)
    app.include_router(deploy.router)
    app.include_router(jobs.router)
    app.include_router(machines.router)
    app.include_router(operations.router)
    app.include_router(account.router)
    app.include_router(dns.router)
    app.include_router(secrets.router)
    app.include_router(settings.router)
    app.include_router(setup.router)
    app.include_router(invite.router)
    app.include_router(people.router)
    app.include_router(webhooks.router)
    app.include_router(graph.router)

    @app.get("/health")
    async def health():
        from starlette.concurrency import run_in_threadpool

        try:
            controller_health = await run_in_threadpool(app.state.controller.health)
        except ControllerClientError:
            return JSONResponse({"status": "unavailable"}, status_code=503)
        if controller_health.status != "ok":
            return JSONResponse({"status": "degraded"}, status_code=503)
        return {"status": "ok", "controller": "ok"}

    logger.info("Web UI ready at homelab root %s", resolved_root)
    return app
