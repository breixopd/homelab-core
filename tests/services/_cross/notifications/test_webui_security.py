from __future__ import annotations

import asyncio

import pytest
from toolkit.webui.security import CSRFMiddleware, RequestBodyLimitMiddleware, SetupRequestGuardMiddleware, csrf_token


def test_csrf_middleware_rejects_unsafe_request_without_token():
    called = False

    async def app(scope, receive, send):
        nonlocal called
        called = True

    middleware = CSRFMiddleware(app)
    session: dict[str, object] = {"authenticated": True}
    messages: list[dict] = []

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        messages.append(message)

    scope = {"type": "http", "method": "POST", "path": "/deploy/jobs/deploy", "headers": [], "session": session}
    asyncio.run(middleware(scope, receive, send))
    assert called is False
    assert messages[0]["status"] == 403


def test_csrf_middleware_replays_valid_form_body():
    seen = b""
    session: dict[str, object] = {"authenticated": True}
    token = csrf_token(session)
    body = f"csrf_token={token}&value=ok".encode()

    async def app(scope, receive, send):
        nonlocal seen
        seen = (await receive())["body"]

    middleware = CSRFMiddleware(app)

    async def receive():
        return {"type": "http.request", "body": body, "more_body": False}

    async def send(message):
        return None

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/settings",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "session": session,
    }
    asyncio.run(middleware(scope, receive, send))
    assert seen == body


def test_setup_posts_require_normal_csrf_validation() -> None:
    called = False

    async def app(scope, receive, send):
        nonlocal called
        called = True

    async def receive():
        return {"type": "http.request", "body": b"capability=secret", "more_body": False}

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/setup/session",
        "headers": [(b"content-type", b"application/x-www-form-urlencoded")],
        "session": {},
    }
    asyncio.run(CSRFMiddleware(app)(scope, receive, send))

    assert called is False
    assert messages[0]["status"] == 403


def test_setup_request_guard_limits_public_body_size() -> None:
    called = False

    async def app(scope, receive, send):
        nonlocal called
        called = True

    async def receive():
        return {"type": "http.request", "body": b"x" * (64 * 1024 + 1), "more_body": False}

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/setup/session",
        "headers": [],
        "client": ("127.0.0.1", 50000),
    }
    asyncio.run(SetupRequestGuardMiddleware(app)(scope, receive, send))

    assert called is False
    assert messages[0]["status"] == 413


def test_request_body_limit_rejects_oversized_public_webhook_before_routing() -> None:
    called = False

    async def app(scope, receive, send):
        nonlocal called
        called = True

    async def receive():
        return {"type": "http.request", "body": b"x" * (64 * 1024 + 1), "more_body": False}

    messages: list[dict] = []

    async def send(message):
        messages.append(message)

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/webhooks/grafana-alert",
        "headers": [],
    }
    asyncio.run(RequestBodyLimitMiddleware(app)(scope, receive, send))

    assert called is False
    assert messages[0]["status"] == 413


def test_setup_request_guard_rate_limits_capability_guesses() -> None:
    calls = 0

    async def app(scope, receive, send):
        nonlocal calls
        calls += 1

    middleware = SetupRequestGuardMiddleware(app)

    async def send(_message):
        return None

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/setup/session",
        "headers": [],
        "client": ("192.0.2.10", 50000),
    }
    statuses: list[int] = []
    for _ in range(11):
        sent: list[dict] = []

        async def receive():
            return {"type": "http.request", "body": b"capability=test", "more_body": False}

        async def capture(message):
            sent.append(message)

        asyncio.run(middleware(scope, receive, capture))
        statuses.extend(message["status"] for message in sent if message["type"] == "http.response.start")

    assert calls == 10
    assert statuses == [429]


@pytest.mark.parametrize(
    ("path", "should_pass"),
    [
        ("/api/webhooks/grafana-alert", True),
        ("/api/webhooks/grafana-alert/test", False),
        ("/api/webhooks/anything-else", False),
    ],
)
def test_only_exact_grafana_webhook_path_is_csrf_exempt(path: str, should_pass: bool) -> None:
    called = False

    async def app(scope, receive, send):
        nonlocal called
        called = True

    middleware = CSRFMiddleware(app)

    async def receive():
        return {"type": "http.request", "body": b"{}", "more_body": False}

    async def send(_message):
        return None

    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [(b"content-type", b"application/json")],
        "session": {"authenticated": False},
    }
    asyncio.run(middleware(scope, receive, send))

    assert called is should_pass


def test_plaintext_secret_export_route_does_not_exist() -> None:
    from toolkit.webui.routers.secrets import router

    assert "/secrets/export" not in {route.path for route in router.routes}
