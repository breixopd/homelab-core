from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from toolkit.controller.contracts import DestroyInfraOperation, JobKind
from toolkit.controller.operations import OperationExecutionError, build_operation_registry
from toolkit.core.config.config import Config, load_config, save_config
from toolkit.core.config.mutations import config_revision
from toolkit.core.config.storage import config_path
from toolkit.core.machines import MachineSpec


def _configured_worker(root) -> str:
    worker = MachineSpec(
        managed=True,
        hostname="worker-01",
        address="10.10.10.20",
        gateway="10.10.10.1",
        vmid=820,
        labels=("compute",),
    )
    save_config(Config(machines={**Config().machines, "worker-east": worker}), config_path(root))
    return config_revision(root)


def _operation(revision: str) -> DestroyInfraOperation:
    return DestroyInfraOperation(
        action="retire_machine",
        scopes=["worker-east"],
        config_revision=revision,
        plan_id="plan-identifier-1234",
        plan_hash="a" * 64,
        approval_token="consumed-approval",
    )


def test_retirement_removes_desired_state_only_after_verified_destroy(tmp_path, monkeypatch) -> None:
    revision = _configured_worker(tmp_path)
    retired: list[str] = []
    monkeypatch.setattr(
        "toolkit.core.infra.infra_destroy.retire_machine_infrastructure_guarded",
        lambda _root, machine_id, on_log=None, **_kwargs: retired.append(machine_id) or 0,
    )
    generated: list[bool] = []
    monkeypatch.setattr(
        "toolkit.core.generate.generate.run_full_generate",
        lambda _root, validate: generated.append(validate) or {},
    )
    context = MagicMock(actor="local:operator")

    result = build_operation_registry(tmp_path).resolve(JobKind.DESTROY_INFRA)(context, _operation(revision))

    assert result == {"ok": True, "action": "retire_machine", "scopes": ["worker-east"]}
    assert retired == ["worker-east"]
    assert "worker-east" not in load_config(config_path(tmp_path)).machines
    assert generated == [True]


def test_retirement_preserves_desired_state_when_destroy_verification_fails(tmp_path, monkeypatch) -> None:
    revision = _configured_worker(tmp_path)
    monkeypatch.setattr(
        "toolkit.core.infra.infra_destroy.retire_machine_infrastructure_guarded",
        lambda *_args, **_kwargs: 1,
    )
    context = MagicMock(actor="local:operator")

    with pytest.raises(OperationExecutionError, match="retirement failed verification"):
        build_operation_registry(tmp_path).resolve(JobKind.DESTROY_INFRA)(context, _operation(revision))

    assert "worker-east" in load_config(config_path(tmp_path)).machines
