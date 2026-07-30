from __future__ import annotations

from pathlib import Path

import pytest
from toolkit.controller.contracts import SecretRotationOperation
from toolkit.controller.operations import OperationExecutionError, _secret_rotation_handler
from toolkit.controller.settings_api import SecretMutationError, restore_secret_values
from toolkit.controller.worker import OperationCancelledError, OperationLeaseLostError
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.deploy.deploy_workflow import DeployWorkflowResult
from toolkit.core.deploy.operation_lease import LeaseBusyError, OperationLease


class _Context:
    actor = "owner"

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def check_cancelled(self) -> None:
        return None

    def log(self, message: str, payload: dict | None = None, **_kwargs) -> None:
        self.events.append((message, payload or {}))


class _CancellingContext(_Context):
    def __init__(self) -> None:
        super().__init__()
        self.checks = 0

    def check_cancelled(self) -> None:
        self.checks += 1
        if self.checks >= 3:
            raise OperationCancelledError("cancel requested")


class _LeaseLosingContext(_Context):
    def log(self, message: str, payload: dict | None = None, **kwargs) -> None:
        if len(self.events) >= 2:
            raise OperationLeaseLostError("operation lease was lost")
        super().log(message, payload, **kwargs)


def _result(success: bool) -> DeployWorkflowResult:
    return DeployWorkflowResult(success, "done", "positive" if success else "negative", {})


def test_rotation_holds_one_lease_across_mutation_and_deploy(tmp_path: Path, monkeypatch) -> None:
    save_config(Config(domain="example.com"), config_path(tmp_path))
    rotated = {"GRAFANA_WEBHOOK_HMAC_SECRET": "new-value"}
    monkeypatch.setattr("toolkit.core.ops.db_safety.pre_deploy_dump", lambda *_args: None)
    monkeypatch.setattr(
        "toolkit.controller.settings_api.rotate_secret_values",
        lambda *_args: ({"GRAFANA_WEBHOOK_HMAC_SECRET": "old-value"}, rotated),
    )

    async def deploy(root, _cfg, **kwargs):
        lease = kwargs["operation_lease"]
        assert lease.snapshot.operation == "controller-secret-rotation"
        with pytest.raises(LeaseBusyError):
            OperationLease.acquire(root, "concurrent-cli")
        return _result(True)

    monkeypatch.setattr("toolkit.core.deploy.deploy_workflow.run_deploy_workflow", deploy)

    result = _secret_rotation_handler(tmp_path)(
        _Context(),
        SecretRotationOperation(secret_names=list(rotated)),
    )

    assert result == {"ok": True, "changed_names": ["GRAFANA_WEBHOOK_HMAC_SECRET"]}
    replacement = OperationLease.acquire(tmp_path, "next-operation")
    replacement.release()


def test_rotation_restores_secrets_before_rollback_deploy(tmp_path: Path, monkeypatch) -> None:
    save_config(Config(domain="example.com"), config_path(tmp_path))
    before = {"GRAFANA_WEBHOOK_HMAC_SECRET": "old-value"}
    rotated = {"GRAFANA_WEBHOOK_HMAC_SECRET": "new-value"}
    events: list[str] = []
    monkeypatch.setattr("toolkit.core.ops.db_safety.pre_deploy_dump", lambda *_args: None)
    monkeypatch.setattr(
        "toolkit.controller.settings_api.rotate_secret_values",
        lambda *_args: (before, rotated),
    )
    monkeypatch.setattr(
        "toolkit.controller.settings_api.restore_secret_values",
        lambda _root, actual_before, expected: events.append(
            "restore" if actual_before == before and expected == rotated else "invalid-restore"
        ),
    )

    async def deploy(_root, _cfg, **_kwargs):
        events.append("deploy")
        return _result(len(events) > 2)

    monkeypatch.setattr("toolkit.core.deploy.deploy_workflow.run_deploy_workflow", deploy)

    with pytest.raises(OperationExecutionError, match="previous credentials restored"):
        _secret_rotation_handler(tmp_path)(
            _Context(),
            SecretRotationOperation(secret_names=list(rotated)),
        )

    assert events == ["deploy", "restore", "deploy"]


def test_rotation_reports_cancelled_only_after_rollback_converges(tmp_path: Path, monkeypatch) -> None:
    save_config(Config(domain="example.com"), config_path(tmp_path))
    before = {"GRAFANA_WEBHOOK_HMAC_SECRET": "old-value"}
    rotated = {"GRAFANA_WEBHOOK_HMAC_SECRET": "new-value"}
    events: list[str] = []
    monkeypatch.setattr("toolkit.core.ops.db_safety.pre_deploy_dump", lambda *_args: None)
    monkeypatch.setattr("toolkit.controller.settings_api.rotate_secret_values", lambda *_args: (before, rotated))
    monkeypatch.setattr(
        "toolkit.controller.settings_api.restore_secret_values",
        lambda *_args: events.append("restore"),
    )

    async def deploy(_root, _cfg, **_kwargs):
        events.append("deploy")
        return _result(len(events) > 2)

    monkeypatch.setattr("toolkit.core.deploy.deploy_workflow.run_deploy_workflow", deploy)

    with pytest.raises(OperationCancelledError):
        _secret_rotation_handler(tmp_path)(
            _CancellingContext(),
            SecretRotationOperation(secret_names=list(rotated)),
        )

    assert events == ["deploy", "restore", "deploy"]


def test_rotation_lease_loss_still_restores_and_converges_rollback(tmp_path: Path, monkeypatch) -> None:
    save_config(Config(domain="example.com"), config_path(tmp_path))
    before = {"GRAFANA_WEBHOOK_HMAC_SECRET": "old-value"}
    rotated = {"GRAFANA_WEBHOOK_HMAC_SECRET": "new-value"}
    events: list[str] = []
    monkeypatch.setattr("toolkit.core.ops.db_safety.pre_deploy_dump", lambda *_args: None)
    monkeypatch.setattr("toolkit.controller.settings_api.rotate_secret_values", lambda *_args: (before, rotated))
    monkeypatch.setattr(
        "toolkit.controller.settings_api.restore_secret_values",
        lambda *_args: events.append("restore"),
    )

    async def deploy(_root, _cfg, **_kwargs):
        events.append("deploy")
        return _result(len(events) > 2)

    monkeypatch.setattr("toolkit.core.deploy.deploy_workflow.run_deploy_workflow", deploy)

    with pytest.raises(OperationLeaseLostError):
        _secret_rotation_handler(tmp_path)(
            _LeaseLosingContext(),
            SecretRotationOperation(secret_names=list(rotated)),
        )

    assert events == ["deploy", "restore", "deploy"]


def test_rotation_hard_interruption_still_restores_and_converges_rollback(tmp_path: Path, monkeypatch) -> None:
    save_config(Config(domain="example.com"), config_path(tmp_path))
    before = {"GRAFANA_WEBHOOK_HMAC_SECRET": "old-value"}
    rotated = {"GRAFANA_WEBHOOK_HMAC_SECRET": "new-value"}
    events: list[str] = []
    monkeypatch.setattr("toolkit.core.ops.db_safety.pre_deploy_dump", lambda *_args: None)
    monkeypatch.setattr("toolkit.controller.settings_api.rotate_secret_values", lambda *_args: (before, rotated))
    monkeypatch.setattr(
        "toolkit.controller.settings_api.restore_secret_values",
        lambda *_args: events.append("restore"),
    )

    async def deploy(_root, _cfg, **_kwargs):
        events.append("deploy")
        if len(events) == 1:
            raise KeyboardInterrupt
        return _result(True)

    monkeypatch.setattr("toolkit.core.deploy.deploy_workflow.run_deploy_workflow", deploy)

    with pytest.raises(OperationExecutionError, match="previous credentials restored"):
        _secret_rotation_handler(tmp_path)(
            _Context(),
            SecretRotationOperation(secret_names=list(rotated)),
        )

    assert events == ["deploy", "restore", "deploy"]


def test_restore_preserves_unrelated_concurrent_secret_changes(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "secrets.enc.yaml").touch()
    state = {
        "ROTATED": "new-value",
        "UNRELATED": "concurrent-value",
    }
    monkeypatch.setattr("toolkit.controller.settings_api.load_secrets_plaintext", lambda _path: dict(state))
    monkeypatch.setattr(
        "toolkit.controller.settings_api.save_secrets_plaintext",
        lambda values, _path: state.clear() or state.update(values),
    )

    restore_secret_values(tmp_path, {"ROTATED": "old-value"}, {"ROTATED": "new-value"})

    assert state == {"ROTATED": "old-value", "UNRELATED": "concurrent-value"}


def test_restore_rejects_selected_secret_race_without_partial_write(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import MagicMock

    (tmp_path / "secrets.enc.yaml").touch()
    state = {"ROTATED": "operator-replacement", "UNRELATED": "unchanged"}
    save = MagicMock()
    monkeypatch.setattr("toolkit.controller.settings_api.load_secrets_plaintext", lambda _path: dict(state))
    monkeypatch.setattr("toolkit.controller.settings_api.save_secrets_plaintext", save)

    with pytest.raises(SecretMutationError, match="changed concurrently"):
        restore_secret_values(tmp_path, {"ROTATED": "old-value"}, {"ROTATED": "new-value"})

    save.assert_not_called()
