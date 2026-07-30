"""Raw Grafana webhook proxy to the authenticated controller."""

from __future__ import annotations

import threading
import time
from collections import deque

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from toolkit.controller.client import ControllerClientError, ControllerRejectedError

router = APIRouter(tags=["webhooks"])
_MAX_BODY_BYTES = 64 * 1024
_RATE_WINDOW_SECONDS = 60
_RATE_LIMIT = 60
_RATE_LOCK = threading.Lock()
_REQUEST_TIMES: deque[float] = deque(maxlen=_RATE_LIMIT)
_CONTROLLER_CAPACITY = threading.BoundedSemaphore(2)


def _allow_request(now: float | None = None) -> bool:
    current = time.monotonic() if now is None else now
    with _RATE_LOCK:
        while _REQUEST_TIMES and current - _REQUEST_TIMES[0] >= _RATE_WINDOW_SECONDS:
            _REQUEST_TIMES.popleft()
        if len(_REQUEST_TIMES) >= _RATE_LIMIT:
            return False
        _REQUEST_TIMES.append(current)
        return True


@router.post("/api/webhooks/grafana-alert")
async def grafana_alert_webhook(request: Request):
    if not _allow_request() or not _CONTROLLER_CAPACITY.acquire(blocking=False):
        return JSONResponse({"error": "Webhook rate limit exceeded"}, status_code=429)
    try:
        raw_body = await request.body()
        if not raw_body or len(raw_body) > _MAX_BODY_BYTES:
            return JSONResponse({"error": "Webhook payload was rejected"}, status_code=413)
        try:
            receipt = await run_in_threadpool(
                request.app.state.controller.accept_grafana_alert,
                raw_body,
                signature=request.headers.get("X-Grafana-Alerting-Signature", ""),
                timestamp=request.headers.get("X-Grafana-Alerting-Signature-Timestamp", ""),
                content_type=request.headers.get("Content-Type", ""),
            )
        except ControllerRejectedError as exc:
            status_code = exc.status_code if exc.status_code in {401, 413, 422, 429, 503} else 502
            return JSONResponse({"error": "Webhook request was rejected"}, status_code=status_code)
        except ControllerClientError:
            return JSONResponse({"error": "Webhook service is unavailable"}, status_code=503)
        return JSONResponse(
            receipt.model_dump(mode="json"),
            status_code=202 if receipt.outcome == "queued" else 200,
        )
    finally:
        _CONTROLLER_CAPACITY.release()
