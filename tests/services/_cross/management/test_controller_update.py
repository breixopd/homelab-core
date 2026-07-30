from __future__ import annotations

from pathlib import Path

import pytest
from toolkit.controller.contracts import UpdateOperation
from toolkit.controller.operations import OperationExecutionError, _update_handler
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.deploy.deploy_workflow import DeployWorkflowResult
from toolkit.core.ops.release_state import load_active_release, load_recovery_release
from toolkit.core.ops.update_plan import UpdateCandidate, UpdatePlan


class _Context:
    actor = "owner"

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def check_cancelled(self) -> None:
        return None

    def log(self, message: str, payload: dict | None = None, **_kwargs) -> None:
        self.events.append((message, payload or {}))


def _plan() -> UpdatePlan:
    return UpdatePlan(
        revision="a" * 64,
        checked_at="2026-07-12T00:00:00+00:00",
        candidates=(
            UpdateCandidate(
                service="redis",
                current="8.8.0-alpine",
                target="8.9.0-alpine",
                current_image="redis:8.8.0-alpine",
                target_image="redis:8.9.0-alpine",
                changelog_url="",
            ),
        ),
    )


def _setup_apply(tmp_path: Path, monkeypatch, outcomes: list[bool]) -> None:
    save_config(Config(domain="example.com", proxmox={"provision_machines": False}), config_path(tmp_path))
    monkeypatch.setattr("toolkit.core.ops.update_plan.load_current_update_plan", lambda *_args: _plan())
    monkeypatch.setattr(
        "toolkit.core.ops.release_update.resolve_target_digest",
        lambda _candidate: "redis@sha256:" + ("b" * 64),
    )
    monkeypatch.setattr("toolkit.core.ops.release_update.affected_roles", lambda *_args: ("infra",))
    monkeypatch.setattr("toolkit.core.ops.release_update.selected_services_require_backup", lambda *_args: False)

    async def deploy(*_args, **_kwargs):
        ok = outcomes.pop(0)
        return DeployWorkflowResult(ok, "done", "positive" if ok else "negative", {})

    monkeypatch.setattr("toolkit.core.deploy.deploy_workflow.run_deploy_workflow", deploy)


def test_update_apply_pins_digest_and_runs_verified_deployment(tmp_path: Path, monkeypatch) -> None:
    _setup_apply(tmp_path, monkeypatch, [True])

    result = _update_handler(tmp_path)(
        _Context(),
        UpdateOperation(action="apply", services=["redis"], revision="a" * 64),
    )

    active = load_active_release(tmp_path)
    assert active is not None
    assert active.images == {"redis": "redis@sha256:" + ("b" * 64)}
    assert result["revision"] == active.revision
    assert result["nodes"] == ["infra"]


def test_update_apply_restores_previous_release_after_failed_verification(tmp_path: Path, monkeypatch) -> None:
    _setup_apply(tmp_path, monkeypatch, [False, True])

    with pytest.raises(OperationExecutionError, match="previous release was restored"):
        _update_handler(tmp_path)(
            _Context(),
            UpdateOperation(action="apply", services=["redis"], revision="a" * 64),
        )

    assert load_active_release(tmp_path) is None


def test_failed_automatic_rollback_requires_and_completes_explicit_recovery(tmp_path: Path, monkeypatch) -> None:
    _setup_apply(tmp_path, monkeypatch, [False, False, True])

    with pytest.raises(OperationExecutionError, match="automatic rollback both failed"):
        _update_handler(tmp_path)(
            _Context(),
            UpdateOperation(action="apply", services=["redis"], revision="a" * 64),
        )

    assert load_active_release(tmp_path) is None
    recovery = load_recovery_release(tmp_path)
    assert recovery is not None
    assert recovery.previous is None
    assert recovery.failed.images == {"redis": "redis@sha256:" + ("b" * 64)}

    result = _update_handler(tmp_path)(_Context(), UpdateOperation(action="recover"))

    assert result == {"ok": True, "action": "recover", "nodes": ["infra"]}
    assert load_recovery_release(tmp_path) is None


def test_new_updates_are_rejected_until_failed_rollback_is_recovered(tmp_path: Path, monkeypatch) -> None:
    _setup_apply(tmp_path, monkeypatch, [False, False])

    with pytest.raises(OperationExecutionError, match="automatic rollback both failed"):
        _update_handler(tmp_path)(
            _Context(),
            UpdateOperation(action="apply", services=["redis"], revision="a" * 64),
        )

    with pytest.raises(OperationExecutionError, match="recovery is required"):
        _update_handler(tmp_path)(
            _Context(),
            UpdateOperation(action="apply", services=["redis"], revision="a" * 64),
        )


def test_update_apply_rejects_a_superseded_plan(tmp_path: Path, monkeypatch) -> None:
    _setup_apply(tmp_path, monkeypatch, [True])

    with pytest.raises(OperationExecutionError) as raised:
        _update_handler(tmp_path)(
            _Context(),
            UpdateOperation(action="apply", services=["redis"], revision="c" * 64),
        )

    assert raised.value.code == "CONFLICT"


def test_stateful_update_requires_encrypted_backups(tmp_path: Path, monkeypatch) -> None:
    _setup_apply(tmp_path, monkeypatch, [True])
    monkeypatch.setattr("toolkit.core.ops.release_update.selected_services_require_backup", lambda *_args: True)

    with pytest.raises(OperationExecutionError, match="require configured encrypted backups") as raised:
        _update_handler(tmp_path)(
            _Context(),
            UpdateOperation(action="apply", services=["redis"], revision="a" * 64),
        )

    assert raised.value.code == "OPERATION_REJECTED"
    assert load_active_release(tmp_path) is None
