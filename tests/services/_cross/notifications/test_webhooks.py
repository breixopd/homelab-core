"""Web UI Grafana webhook raw-proxy tests."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import Request
from toolkit.controller.client import ControllerRejectedError
from toolkit.controller.read_models import GrafanaWebhookReceipt, WebhookJobReceipt


def _request(controller, body: bytes = b'{"status":"firing"}') -> Request:
    request = MagicMock(spec=Request)
    request.body = AsyncMock(return_value=body)
    request.headers = {
        "Content-Type": "application/json",
        "X-Grafana-Alerting-Signature": "a" * 64,
        "X-Grafana-Alerting-Signature-Timestamp": "1800000000",
        "X-Untrusted": "must-not-forward",
    }
    request.app.state.controller = controller
    return request


@pytest.mark.anyio
async def test_webhook_forwards_raw_body_and_only_grafana_auth_headers() -> None:
    from toolkit.webui.routers.webhooks import grafana_alert_webhook

    controller = MagicMock()
    controller.accept_grafana_alert.return_value = GrafanaWebhookReceipt(
        outcome="queued",
        jobs=[WebhookJobReceipt(service="sonarr", job_id="job-1", replayed=False)],
    )
    body = b'{"status": "firing", "raw": true}'

    response = await grafana_alert_webhook(_request(controller, body))

    assert response.status_code == 202
    args, kwargs = controller.accept_grafana_alert.call_args
    assert args == (body,)
    assert kwargs == {
        "signature": "a" * 64,
        "timestamp": "1800000000",
        "content_type": "application/json",
    }


@pytest.mark.anyio
async def test_webhook_rejects_oversized_body_before_controller_call() -> None:
    from toolkit.webui.routers.webhooks import grafana_alert_webhook

    controller = MagicMock()
    response = await grafana_alert_webhook(_request(controller, b"x" * (64 * 1024 + 1)))

    assert response.status_code == 413
    controller.accept_grafana_alert.assert_not_called()


@pytest.mark.anyio
async def test_webhook_preserves_safe_controller_rejection_status_without_detail() -> None:
    from toolkit.webui.routers.webhooks import grafana_alert_webhook

    controller = MagicMock()
    controller.accept_grafana_alert.side_effect = ControllerRejectedError(
        "FORBIDDEN",
        "sensitive controller detail",
        {},
        "correlation-1234",
        401,
    )

    response = await grafana_alert_webhook(_request(controller))

    assert response.status_code == 401
    payload = json.loads(response.body)
    assert payload == {"error": "Webhook request was rejected"}
    assert "sensitive" not in response.body.decode()


@pytest.mark.anyio
async def test_webhook_rate_limit_rejects_before_reading_body_or_calling_controller(monkeypatch) -> None:
    from toolkit.webui.routers import webhooks

    controller = MagicMock()
    request = _request(controller)
    monkeypatch.setattr(webhooks, "_allow_request", lambda: False)

    response = await webhooks.grafana_alert_webhook(request)

    assert response.status_code == 429
    request.body.assert_not_awaited()
    controller.accept_grafana_alert.assert_not_called()
