"""Music Sync's controller action is owned by the service test suite."""

from __future__ import annotations

from pathlib import Path

from toolkit.controller.contracts import JobRequest, JobState, ServiceActionOperation
from toolkit.controller.operations import build_operation_registry
from toolkit.controller.store import ControllerStore
from toolkit.controller.worker import ControllerWorker
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path
from toolkit.services import get_service_plugin


def test_service_action_dispatches_only_to_the_declaring_plugin(tmp_path: Path, monkeypatch) -> None:
    save_config(Config(domain="example.com"), config_path(tmp_path))
    plugin = get_service_plugin("music-sync")
    assert plugin is not None
    monkeypatch.setattr(plugin, "supported_actions", lambda: frozenset({"sync-now"}))
    invoked: list[str] = []
    monkeypatch.setattr(
        plugin,
        "execute_action",
        lambda action, _cfg, _secrets, _root: invoked.append(action) or ["Sync accepted"],
        raising=False,
    )
    store = ControllerStore(tmp_path / "controller.db")
    job = store.create_job(
        JobRequest(
            idempotency_key="service-action-1234",
            operation=ServiceActionOperation(service="music-sync", action="sync-now"),
        ),
        principal="owner",
    )

    ControllerWorker(store, build_operation_registry(tmp_path), worker_id="worker-a").run_once()

    finished = store.get_job(job.job_id)
    assert finished.state is JobState.SUCCEEDED
    assert finished.result == {"ok": True, "service": "music-sync", "action": "sync-now"}
    assert invoked == ["sync-now"]
    assert "Sync accepted" in [event.message for event in store.events_after(job.job_id, 0)]
