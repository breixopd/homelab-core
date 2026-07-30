"""Authenticated external event ingestion with narrow durable operations."""

from __future__ import annotations

import hashlib
import hmac
import stat
import threading
import time
from pathlib import Path
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, TypeAdapter, ValidationError

from toolkit.controller.contracts import JobRequest, ServiceName, WebhookHealOperation
from toolkit.controller.read_models import GrafanaWebhookReceipt, WebhookJobReceipt
from toolkit.controller.store import ControllerStore
from toolkit.core.config.storage import secrets_path
from toolkit.core.secrets.secrets import load_secrets_plaintext

_MAX_BODY_BYTES = 64 * 1024
_MAX_TIMESTAMP_SKEW_SECONDS = 300
_MAX_ACTIVE_HEALS = 8
_SIGNATURE: TypeAdapter[str] = TypeAdapter(Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")])
_LABEL = Annotated[str, StringConstraints(min_length=1, max_length=256)]
_SECRET_CACHE_LOCK = threading.Lock()
_SECRET_CACHE: dict[Path, tuple[tuple[int, int, int, int] | None, str]] = {}


class WebhookAuthenticationError(RuntimeError):
    pass


class WebhookConfigurationError(RuntimeError):
    pass


class WebhookPayloadError(RuntimeError):
    pass


class _GrafanaAlert(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    status: str = Field(min_length=1, max_length=32)
    labels: dict[_LABEL, _LABEL] = Field(default_factory=dict, max_length=64)


class _GrafanaPayload(BaseModel):
    model_config = ConfigDict(extra="ignore", strict=True)

    status: str = Field(min_length=1, max_length=32)
    alerts: list[_GrafanaAlert] = Field(default_factory=list, max_length=32)


def _secret_fingerprint(path: Path) -> tuple[int, int, int, int] | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if path.is_symlink() or not stat.S_ISREG(info.st_mode):
        raise WebhookConfigurationError("Grafana webhook secret storage is invalid")
    return info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def _webhook_secret(root: Path) -> str:
    path = secrets_path(root.resolve())
    fingerprint = _secret_fingerprint(path)
    with _SECRET_CACHE_LOCK:
        cached = _SECRET_CACHE.get(path)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        secret = load_secrets_plaintext(path).get("GRAFANA_WEBHOOK_HMAC_SECRET", "")
        if len(secret) < 32:
            raise WebhookConfigurationError("Grafana webhook authentication is not configured")
        if len(_SECRET_CACHE) >= 8:
            _SECRET_CACHE.pop(next(iter(_SECRET_CACHE)))
        _SECRET_CACHE[path] = (fingerprint, secret)
        return secret


def _verify_request(
    root: Path,
    raw_body: bytes,
    *,
    signature: str,
    timestamp: str,
    content_type: str,
    now: float,
) -> None:
    if not raw_body or len(raw_body) > _MAX_BODY_BYTES:
        raise WebhookPayloadError("webhook body size is invalid")
    if content_type.split(";", 1)[0].strip().lower() != "application/json":
        raise WebhookPayloadError("webhook content type is invalid")
    if not timestamp.isascii() or not timestamp.isdigit() or len(timestamp) > 16:
        raise WebhookAuthenticationError("webhook timestamp is invalid")
    if abs(now - int(timestamp)) > _MAX_TIMESTAMP_SKEW_SECONDS:
        raise WebhookAuthenticationError("webhook timestamp is outside the accepted window")
    try:
        normalized_signature = _SIGNATURE.validate_python(signature.lower())
    except ValidationError as exc:
        raise WebhookAuthenticationError("webhook signature is invalid") from exc
    secret = _webhook_secret(root)
    expected = hmac.new(
        secret.encode("utf-8"),
        timestamp.encode("ascii") + b":" + raw_body,
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(normalized_signature, expected):
        raise WebhookAuthenticationError("webhook signature is invalid")


def accept_grafana_alert(
    root: Path,
    store: ControllerStore,
    raw_body: bytes,
    *,
    signature: str,
    timestamp: str,
    content_type: str,
    now: float | None = None,
) -> GrafanaWebhookReceipt:
    _verify_request(
        root,
        raw_body,
        signature=signature,
        timestamp=timestamp,
        content_type=content_type,
        now=time.time() if now is None else now,
    )
    try:
        payload = _GrafanaPayload.model_validate_json(raw_body)
    except ValidationError as exc:
        raise WebhookPayloadError("Grafana webhook payload is invalid") from exc

    services: set[str] = set()
    if payload.status == "firing":
        for alert in payload.alerts:
            if alert.status != "firing":
                continue
            candidate = alert.labels.get("homelab_service", "")
            try:
                services.add(TypeAdapter(ServiceName).validate_python(candidate))
            except ValidationError:
                continue
    if not services:
        return GrafanaWebhookReceipt(outcome="ignored", reason="no_firing_services", jobs=[])
    if len(services) > 8:
        raise WebhookPayloadError("Grafana webhook targets too many services")

    requests: list[JobRequest] = []
    for service in sorted(services):
        fingerprint = hashlib.sha256(raw_body + b"\0" + service.encode("ascii")).hexdigest()
        idempotency_key = (
            "grafana-"
            + hashlib.sha256(timestamp.encode("ascii") + b"\0" + raw_body + b"\0" + service.encode("ascii")).hexdigest()
        )
        requests.append(
            JobRequest(
                idempotency_key=idempotency_key,
                operation=WebhookHealOperation(service=service, alert_fingerprint=fingerprint),
            )
        )
    submitted = store.submit_job_batch_limited(
        requests,
        principal="webhook:grafana",
        active_limit=_MAX_ACTIVE_HEALS,
    )
    receipts: list[WebhookJobReceipt] = []
    for service, (job, created) in zip(sorted(services), submitted, strict=True):
        receipts.append(WebhookJobReceipt(service=service, job_id=job.job_id, replayed=not created))
    return GrafanaWebhookReceipt(outcome="queued", jobs=receipts)
