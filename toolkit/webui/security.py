"""Request-boundary security for the Web UI."""

from __future__ import annotations

import hmac
import secrets
import time
from collections.abc import MutableMapping
from urllib.parse import parse_qs

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_EXEMPT_PATHS = frozenset({"/login", "/api/webhooks/grafana-alert"})
_EXEMPT_PREFIXES = ("/invite/",)
_MAX_REQUEST_BODY_BYTES = 64 * 1024
_SETUP_BODY_LIMIT = 64 * 1024
_SETUP_RATE_LIMITS = {
    "/setup/session": (10, 5 * 60),
    "/setup": (5, 15 * 60),
}


def csrf_token(session: MutableMapping[str, object]) -> str:
    token = str(session.get("csrf_token") or "")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


class RequestBodyLimitMiddleware:
    """Bound every mutating Web UI request before form or JSON parsing."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        method = str(scope.get("method", "GET")).upper()
        if scope["type"] != "http" or method not in _UNSAFE_METHODS:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            content_length = _MAX_REQUEST_BODY_BYTES + 1
        if content_length < 0 or content_length > _MAX_REQUEST_BODY_BYTES:
            await self._reject(scope, receive, send)
            return

        body = bytearray()
        more = True
        while more:
            message = await receive()
            body.extend(message.get("body", b""))
            if len(body) > _MAX_REQUEST_BODY_BYTES:
                await self._reject(scope, receive, send)
                return
            more = bool(message.get("more_body", False))

        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send) -> None:
        response = JSONResponse(
            {"detail": "Request body is too large"},
            status_code=413,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
        await response(scope, receive, send)


class SetupRequestGuardMiddleware:
    """Bound public bootstrap forms before parsing or controller work."""

    def __init__(self, app: ASGIApp):
        self.app = app
        self._windows: dict[tuple[str, str], tuple[int, float]] = {}

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = str(scope.get("path", ""))
        method = str(scope.get("method", "GET")).upper()
        if scope["type"] != "http" or method != "POST" or path not in _SETUP_RATE_LIMITS:
            await self.app(scope, receive, send)
            return

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        try:
            content_length = int(headers.get(b"content-length", b"0"))
        except ValueError:
            content_length = _SETUP_BODY_LIMIT + 1
        if content_length > _SETUP_BODY_LIMIT:
            await self._reject(scope, receive, send, 413, "Setup request body is too large")
            return

        client = scope.get("client")
        client_host = str(client[0]) if isinstance(client, tuple) and client else "unknown"
        if not self._allow(path, client_host):
            await self._reject(scope, receive, send, 429, "Setup request rate limit exceeded")
            return

        body = bytearray()
        more = True
        while more:
            message = await receive()
            body.extend(message.get("body", b""))
            if len(body) > _SETUP_BODY_LIMIT:
                await self._reject(scope, receive, send, 413, "Setup request body is too large")
                return
            more = bool(message.get("more_body", False))

        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)

    def _allow(self, path: str, client_host: str) -> bool:
        now = time.monotonic()
        maximum, window_seconds = _SETUP_RATE_LIMITS[path]
        key = (path, client_host)
        count, reset_at = self._windows.get(key, (0, now + window_seconds))
        if now >= reset_at:
            count, reset_at = 0, now + window_seconds
        count += 1
        self._windows[key] = (count, reset_at)
        if len(self._windows) > 2048:
            self._windows = {existing_key: window for existing_key, window in self._windows.items() if window[1] > now}
        return count <= maximum

    @staticmethod
    async def _reject(scope: Scope, receive: Receive, send: Send, status_code: int, message: str) -> None:
        response = JSONResponse(
            {"detail": message},
            status_code=status_code,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
        await response(scope, receive, send)


class CSRFMiddleware:
    """Require a session-bound token on every authenticated unsafe request."""

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        session = scope.get("session")
        if not isinstance(session, MutableMapping):
            await self.app(scope, receive, send)
            return
        expected = csrf_token(session)
        path = str(scope.get("path", ""))
        method = str(scope.get("method", "GET")).upper()
        if method not in _UNSAFE_METHODS or path in _EXEMPT_PATHS or path.startswith(_EXEMPT_PREFIXES):
            await self.app(scope, receive, send)
            return

        body = bytearray()
        more = True
        while more:
            message = await receive()
            body.extend(message.get("body", b""))
            more = bool(message.get("more_body", False))

        headers = {key.lower(): value for key, value in scope.get("headers", [])}
        provided = headers.get(b"x-csrf-token", b"").decode("utf-8", errors="replace")
        if not provided and b"application/x-www-form-urlencoded" in headers.get(b"content-type", b""):
            values = parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
            provided = values.get("csrf_token", [""])[0]

        if not provided or not hmac.compare_digest(provided, expected):
            await send(
                {
                    "type": "http.response.start",
                    "status": 403,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": b'{"detail":"CSRF validation failed"}'})
            return

        sent = False

        async def replay() -> Message:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": bytes(body), "more_body": False}

        await self.app(scope, replay, send)
