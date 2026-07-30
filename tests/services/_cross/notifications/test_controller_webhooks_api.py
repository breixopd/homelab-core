from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

import pytest
from toolkit.controller.store import ControllerStore, JobQueueLimitError
from toolkit.controller.webhooks_api import (
    _SECRET_CACHE,
    WebhookAuthenticationError,
    WebhookPayloadError,
    accept_grafana_alert,
)

_SECRET = "grafana-webhook-secret-that-is-long-enough-123456"
_NOW = 1_800_000_000


def _body(*alerts: dict, status: str = "firing", **extra) -> bytes:
    return json.dumps({"receiver": "homelab", "status": status, "alerts": list(alerts), **extra}).encode()


def _alert(service: str = "sonarr", *, status: str = "firing") -> dict:
    return {
        "status": status,
        "labels": {"alertname": "ContainerDown", "homelab_service": service},
        "annotations": {"description": "untrusted content"},
        "fingerprint": "upstream-fingerprint",
    }


def _signature(body: bytes, timestamp: str = str(_NOW)) -> str:
    return hmac.new(_SECRET.encode(), timestamp.encode() + b":" + body, hashlib.sha256).hexdigest()


@pytest.fixture(autouse=True)
def _secrets(monkeypatch) -> None:
    monkeypatch.setattr(
        "toolkit.controller.webhooks_api.load_secrets_plaintext",
        lambda _path: {"GRAFANA_WEBHOOK_HMAC_SECRET": _SECRET},
    )


def _accept(root: Path, store: ControllerStore, body: bytes, **overrides):
    timestamp = overrides.pop("timestamp", str(_NOW))
    return accept_grafana_alert(
        root,
        store,
        body,
        signature=overrides.pop("signature", _signature(body, timestamp)),
        timestamp=timestamp,
        content_type=overrides.pop("content_type", "application/json; charset=utf-8"),
        now=overrides.pop("now", float(_NOW)),
        **overrides,
    )


def test_current_grafana_payload_queues_bounded_idempotent_jobs(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    body = _body(_alert("sonarr"), _alert("radarr"), title="ignored untrusted title")

    first = _accept(tmp_path, store, body)
    replay = _accept(tmp_path, store, body)

    assert first.outcome == "queued"
    assert [job.service for job in first.jobs] == ["radarr", "sonarr"]
    assert all(not job.replayed for job in first.jobs)
    assert [job.job_id for job in replay.jobs] == [job.job_id for job in first.jobs]
    assert all(job.replayed for job in replay.jobs)
    persisted = [store.get_job(job.job_id) for job in first.jobs]
    assert all(job.actor == "webhook:grafana" for job in persisted)
    assert "untrusted content" not in "".join(job.request.model_dump_json() for job in persisted)


def test_only_per_alert_explicit_service_labels_can_authorize_heal(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    body = _body(
        {"status": "firing", "labels": {"container": "postgres"}},
        status="firing",
        labels={"homelab_service": "postgres"},
        commonLabels={"homelab_service": "postgres"},
    )

    receipt = _accept(tmp_path, store, body)

    assert receipt.outcome == "ignored"
    assert receipt.reason == "no_firing_services"
    assert receipt.jobs == []


def test_resolved_alerts_are_ignored(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    receipt = _accept(tmp_path, store, _body(_alert(status="resolved"), status="resolved"))

    assert receipt.outcome == "ignored"


@pytest.mark.parametrize("bad_signature", ["", "not-hex", "a" * 63, "a" * 65])
def test_signature_is_strictly_validated(tmp_path: Path, bad_signature: str) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    body = _body(_alert())

    with pytest.raises(WebhookAuthenticationError):
        _accept(tmp_path, store, body, signature=bad_signature)


@pytest.mark.parametrize("timestamp", [str(_NOW - 301), str(_NOW + 301), "not-a-time", "-1"])
def test_timestamp_replay_window_is_enforced(tmp_path: Path, timestamp: str) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    body = _body(_alert())

    with pytest.raises(WebhookAuthenticationError):
        _accept(tmp_path, store, body, timestamp=timestamp)


@pytest.mark.parametrize(
    ("body", "content_type"),
    [(b"", "application/json"), (b"[]", "application/json"), (b"{}", "text/plain")],
)
def test_payload_shape_size_and_content_type_are_bounded(
    tmp_path: Path,
    body: bytes,
    content_type: str,
) -> None:
    store = ControllerStore(tmp_path / "controller.db")

    with pytest.raises(WebhookPayloadError):
        _accept(tmp_path, store, body, content_type=content_type)


def test_webhook_specific_queue_limit_does_not_block_idempotent_replay(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    accepted = []
    for index in range(8):
        body = _body(_alert(f"service-{index}"))
        accepted.append((body, _accept(tmp_path, store, body)))

    ninth = _body(_alert("service-9"))
    with pytest.raises(JobQueueLimitError):
        _accept(tmp_path, store, ninth)

    replay = _accept(tmp_path, store, accepted[0][0])
    assert replay.jobs[0].replayed is True


def test_multi_service_notification_is_rejected_atomically_at_capacity(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    for index in range(7):
        _accept(tmp_path, store, _body(_alert(f"service-{index}")))
    body = _body(_alert("sonarr"), _alert("radarr"))

    with pytest.raises(JobQueueLimitError):
        _accept(tmp_path, store, body)

    assert store.active_job_counts() == (7, 0)


def test_correctly_shaped_invalid_signatures_decrypt_secret_only_once(tmp_path: Path, monkeypatch) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    body = _body(_alert())
    calls = 0

    def load(_path):
        nonlocal calls
        calls += 1
        return {"GRAFANA_WEBHOOK_HMAC_SECRET": _SECRET}

    _SECRET_CACHE.clear()
    monkeypatch.setattr("toolkit.controller.webhooks_api.load_secrets_plaintext", load)
    for _ in range(2):
        with pytest.raises(WebhookAuthenticationError):
            _accept(tmp_path, store, body, signature="b" * 64)

    assert calls == 1
