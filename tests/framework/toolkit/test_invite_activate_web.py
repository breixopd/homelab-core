"""Web UI invite flow is a presentation-only controller proxy."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from fastapi import HTTPException, Request
from fastapi.responses import RedirectResponse
from toolkit.controller.client import ControllerRejectedError
from toolkit.controller.read_models import InviteActivationResult, InvitePreview


def _preview(*, valid: bool = True) -> InvitePreview:
    return InvitePreview(
        valid=valid,
        domain="test.example.com",
        secure_cookie=True,
        cookie_max_age_seconds=72 * 3600,
        activation_csrf="a" * 64 if valid else "",
        display_name="Family" if valid else "",
        email="family@test.example.com" if valid else "",
        sections=[],
    )


def _request(controller, *, token: str = "opaque-token", origin: str = "https://homelab.test.example.com") -> Request:
    request = MagicMock(spec=Request)
    request.cookies = {"homelab_invite_activation": token} if token else {}
    request.headers = {"origin": origin} if origin else {}
    request.app.state.controller = controller
    return request


@pytest.mark.anyio
async def test_invite_activate_post_forwards_one_typed_request_and_clears_cookie() -> None:
    from toolkit.webui.routers import invite

    controller = MagicMock()
    controller.invite_preview.return_value = _preview()
    controller.activate_invite.return_value = InviteActivationResult(outcome="activated", secure_cookie=True)

    response = await invite.invite_activate_post(
        _request(controller),
        activation_csrf="a" * 64,
        password="new-password-12",
        password_confirm="new-password-12",
    )

    assert isinstance(response, RedirectResponse)
    assert response.headers["location"] == "/invite/activated"
    activation = controller.activate_invite.call_args.args[0]
    assert activation.token == "opaque-token"
    assert activation.password == "new-password-12"
    assert activation.origin == "https://homelab.test.example.com"
    cookie = response.headers["set-cookie"]
    assert "homelab_invite_activation=" in cookie
    assert "Max-Age=0" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Secure" in cookie


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("password", "confirmation", "message"),
    [
        ("short", "short", "at%20least%2010"),
        ("x" * 129, "x" * 129, "at%20most%20128"),
        ("new-password-12", "different-password", "Passwords%20do%20not%20match"),
    ],
)
async def test_invite_form_validation_never_calls_activation(
    password: str,
    confirmation: str,
    message: str,
) -> None:
    from toolkit.webui.routers import invite

    controller = MagicMock()
    controller.invite_preview.return_value = _preview()

    response = await invite.invite_activate_post(
        _request(controller),
        activation_csrf="a" * 64,
        password=password,
        password_confirm=confirmation,
    )

    assert message in response.headers["location"]
    controller.activate_invite.assert_not_called()


@pytest.mark.anyio
async def test_invalid_or_terminal_invite_is_cleared_without_password_submission() -> None:
    from toolkit.webui.routers import invite

    controller = MagicMock()
    controller.invite_preview.return_value = _preview(valid=False)

    response = await invite.invite_activate_post(
        _request(controller),
        activation_csrf="invalid",
        password="new-password-12",
        password_confirm="new-password-12",
    )

    assert "invalid" in response.headers["location"].lower()
    assert "Max-Age=0" in response.headers["set-cookie"]
    controller.activate_invite.assert_not_called()


@pytest.mark.anyio
async def test_controller_origin_or_csrf_rejection_stays_forbidden() -> None:
    from toolkit.webui.routers import invite

    controller = MagicMock()
    controller.invite_preview.return_value = _preview()
    controller.activate_invite.side_effect = ControllerRejectedError(
        "FORBIDDEN",
        "rejected",
        {},
        "correlation-1234",
    )

    with pytest.raises(HTTPException) as caught:
        await invite.invite_activate_post(
            _request(controller, origin="https://attacker.example.com"),
            activation_csrf="a" * 64,
            password="new-password-12",
            password_confirm="new-password-12",
        )

    assert caught.value.status_code == 403


@pytest.mark.anyio
async def test_invite_get_exchanges_query_bearer_for_secure_cookie() -> None:
    from toolkit.webui.routers import invite

    controller = MagicMock()
    controller.invite_preview.return_value = _preview()

    response = await invite.invite_activate_get(_request(controller, token=""), token="opaque-token")

    assert response.headers["location"] == "/invite/activate"
    cookie = response.headers["set-cookie"]
    assert "opaque-token" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=strict" in cookie
    assert "Secure" in cookie
    assert response.headers["referrer-policy"] == "no-referrer"
    assert response.headers["cache-control"] == "no-store"
