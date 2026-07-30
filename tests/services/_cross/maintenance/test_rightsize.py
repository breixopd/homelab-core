"""Tests for the auto-rightsize proposal engine + watchdog rightsize CLI.

The autouse _no_network_probes fixture patches time.sleep to a no-op globally,
so these tests never block on real waits. Telemetry is mocked via the
``telemetry=`` argument to ``compute_rightsize_proposals``, never live HTTP.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from toolkit.cli import main
from toolkit.core.ops.watchdog.rightsize import (
    RightsizeApprovalPayload,
    RightsizeConfig,
    RightsizeProposal,
    _load_service_metadata,
    _parse_mem_to_mb,
    _query_prometheus_p95,
    _save_rightsize_state,
    apply_rightsize_proposals,
    can_auto_apply,
    compute_rightsize_proposals,
    execute_approved_rightsize,
    is_within_max_step,
    respects_floor,
    rightsize_config_from_desired_state,
)

# --- pure guardrail helpers ------------------------------------------------


def test_is_within_max_step_small_shrink_ok():
    assert is_within_max_step(1000, 800, 25.0) is True  # 20% shrink


def test_is_within_max_step_oversize_blocked():
    assert is_within_max_step(1000, 500, 25.0) is False  # 50% shrink


def test_is_within_max_step_grow_within_limit():
    # grows 20% — within step, but can_auto_apply separately blocks grows.
    assert is_within_max_step(1000, 1200, 25.0) is True


def test_is_within_max_step_zero_current_blocked():
    # Avoid divide-by-zero: returns False when current is zero.
    assert is_within_max_step(0, 100, 25.0) is False


def test_respects_floor():
    assert respects_floor(200, 128) is True
    assert respects_floor(64, 128) is False


def test_parse_mem_to_mb():
    assert _parse_mem_to_mb("512m") == 512
    assert _parse_mem_to_mb("1g") == 1024
    assert _parse_mem_to_mb("2048") == 2048
    assert _parse_mem_to_mb("768K") == 0  # 768 KiB floors to 0 MiB
    assert _parse_mem_to_mb("garbage") == 0


# --- can_auto_apply policy --------------------------------------------------

_NOW = 1_000_000_000.0


def _proposal(*, current=1000, proposed=800, stateful=False, cpus_cur=2.0, cpus_new=1.5):
    return RightsizeProposal(
        vm="apps",
        service="grafana",
        current_mem_mb=current,
        proposed_mem_mb=proposed,
        current_cpus=cpus_cur,
        proposed_cpus=cpus_new,
        p95_mem_mb=600,
        p95_cpu_pct=20.0,
        reason="idle",
        safe_to_apply=True,
        stateful=stateful,
    )


def test_can_auto_apply_safe_shrink():
    assert can_auto_apply(_proposal(), RightsizeConfig(), last_applied_at=None, now=_NOW) is True


def test_can_auto_apply_blocks_stateful():
    p = _proposal(stateful=True, proposed=800, current=1000)
    assert can_auto_apply(p, RightsizeConfig(), last_applied_at=None, now=_NOW) is False


def test_can_auto_apply_blocks_grow():
    p = _proposal(current=800, proposed=1000)  # grows
    assert can_auto_apply(p, RightsizeConfig(), last_applied_at=None, now=_NOW) is False


def test_can_auto_apply_respects_cooldown():
    last = _NOW - (23 * 3600)  # 23h ago — within 24h cooldown
    assert can_auto_apply(_proposal(), RightsizeConfig(), last_applied_at=last, now=_NOW) is False


def test_can_auto_apply_after_cooldown():
    last = _NOW - (25 * 3600)  # 25h ago — past cooldown
    assert can_auto_apply(_proposal(), RightsizeConfig(), last_applied_at=last, now=_NOW) is True


def test_can_auto_apply_kill_switch_off():
    cfg = RightsizeConfig(enabled=False)
    assert can_auto_apply(_proposal(), cfg, last_applied_at=None, now=_NOW) is False


def test_can_auto_apply_blocks_oversize_step():
    p = _proposal(current=1000, proposed=600)  # 40% shrink — over 25%
    assert can_auto_apply(p, RightsizeConfig(), last_applied_at=None, now=_NOW) is False


def test_can_auto_apply_blocks_cpu_grow():
    p = _proposal(current=1000, proposed=800, cpus_cur=1.0, cpus_new=1.5)
    assert can_auto_apply(p, RightsizeConfig(), last_applied_at=None, now=_NOW) is False


def test_can_auto_apply_blocks_oversize_cpu_step():
    p = _proposal(current=1000, proposed=800, cpus_cur=2.0, cpus_new=1.0)
    assert can_auto_apply(p, RightsizeConfig(), last_applied_at=None, now=_NOW) is False


def test_can_auto_apply_allows_cpu_only_shrink():
    p = _proposal(current=1000, proposed=1000, cpus_cur=2.0, cpus_new=1.5)
    assert can_auto_apply(p, RightsizeConfig(), last_applied_at=None, now=_NOW) is True


# --- compute_rightsize_proposals (with mocked telemetry) -------------------


def test_compute_proposals_with_mock_telemetry(tmp_path: Path):
    (tmp_path / "generated" / "apps").mkdir(parents=True)
    (tmp_path / "generated" / "apps" / "compose.limits.yml").write_text(
        "services:\n  grafana:\n    mem_limit: 1000m\n    cpus: 2.0\n"
    )
    telemetry = {
        "grafana": {
            "p95_mem_mb": 600.0,
            "p95_cpu_pct": 130.0,
            "sample_count": 25_000,
            "stateful": False,
            "current_mem_mb": 1000,
            "current_cpus": 2.0,
            "memory_floor_mb": 128,
        }
    }
    proposals = compute_rightsize_proposals(vm="apps", root=tmp_path, telemetry=telemetry)
    assert len(proposals) == 1
    p = proposals[0]
    assert p.service == "grafana"
    # proposed = ceil(600 * 1.3) = 780; shrink 22% — safe.
    assert p.proposed_mem_mb == 780
    assert p.safe_to_apply is True


def test_runtime_services_inherit_owner_resource_policy():
    metadata = _load_service_metadata()

    assert metadata["homelab-controller"]["stateful"] is True
    assert metadata["homelab-controller"]["memory_floor_mb"] == metadata["homelab-ui"]["memory_floor_mb"]


def test_compute_proposals_enforces_persisted_cooldown(tmp_path: Path):
    telemetry = {
        "grafana": {
            "p95_mem_mb": 600.0,
            "p95_cpu_pct": 130.0,
            "sample_count": 25_000,
            "stateful": False,
            "current_mem_mb": 1000,
            "current_cpus": 2.0,
            "memory_floor_mb": 128,
            "cpu_floor": 0.1,
        }
    }
    _save_rightsize_state(tmp_path, {"apps/grafana": _NOW - 3600})

    with patch("toolkit.core.ops.watchdog.rightsize.time.time", return_value=_NOW):
        proposals = compute_rightsize_proposals(vm="apps", root=tmp_path, telemetry=telemetry)

    assert len(proposals) == 1
    assert proposals[0].safe_to_apply is False
    assert proposals[0].blocked_reason == "cooldown"


def test_compute_proposals_rejects_insufficient_samples(tmp_path: Path):
    telemetry = {
        "grafana": {
            "p95_mem_mb": 100.0,
            "p95_cpu_pct": 10.0,
            "sample_count": 10,
            "current_mem_mb": 1000,
            "current_cpus": 2.0,
            "memory_floor_mb": 128,
            "cpu_floor": 0.1,
        }
    }

    assert compute_rightsize_proposals(vm="apps", root=tmp_path, telemetry=telemetry) == []


def test_compute_proposals_requires_sample_density_for_full_window(tmp_path: Path):
    telemetry = {
        "grafana": {
            "p95_mem_mb": 600.0,
            "p95_cpu_pct": 50.0,
            "sample_count": 20_000,
            "current_mem_mb": 1000,
            "current_cpus": 1.0,
            "memory_floor_mb": 128,
            "cpu_floor": 0.1,
        }
    }

    assert compute_rightsize_proposals(vm="apps", root=tmp_path, telemetry=telemetry) == []


def test_query_prometheus_filters_node_and_uses_compose_service_label(tmp_path: Path):
    payloads = [
        {
            "data": {
                "result": [
                    {
                        "metric": {"container_label_com_docker_compose_service": "grafana"},
                        "value": [0, "104857600"],
                    }
                ]
            }
        },
        {
            "data": {
                "result": [
                    {
                        "metric": {"container_label_com_docker_compose_service": "grafana"},
                        "value": [0, "0.5"],
                    }
                ]
            }
        },
        {
            "data": {
                "result": [
                    {
                        "metric": {"container_label_com_docker_compose_service": "grafana"},
                        "value": [0, "1073741824"],
                    }
                ]
            }
        },
        {
            "data": {
                "result": [
                    {
                        "metric": {"container_label_com_docker_compose_service": "grafana"},
                        "value": [0, "1.5"],
                    }
                ]
            }
        },
        {
            "data": {
                "result": [
                    {
                        "metric": {"container_label_com_docker_compose_service": "grafana"},
                        "value": [0, "25000"],
                    }
                ]
            }
        },
    ]
    requested_urls: list[str] = []

    class Response:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            import json

            return json.dumps(self.payload).encode()

    def urlopen(url, timeout):
        requested_urls.append(url)
        assert timeout == 10
        return Response(payloads.pop(0))

    with (
        patch("toolkit.core.ops.watchdog.rightsize._resolve_prometheus_url", return_value="http://prometheus:9090"),
        patch("toolkit.core.ops.watchdog.rightsize.urllib.request.urlopen", side_effect=urlopen),
    ):
        result = _query_prometheus_p95("apps", root=tmp_path, window_days=7)

    assert result["grafana"] == {
        "p95_mem_mb": 100.0,
        "p95_cpu_pct": 50.0,
        "sample_count": 25000,
        "current_mem_mb": 1024,
        "current_cpus": 1.5,
    }
    decoded = " ".join(__import__("urllib.parse").parse.unquote(url) for url in requested_urls)
    assert 'instance="apps"' in decoded
    assert "container_label_com_docker_compose_service" in decoded
    assert "[7d:5m]" in decoded
    assert "container_spec_memory_limit_bytes" in decoded
    assert "container_spec_cpu_quota" in decoded


def test_query_prometheus_fails_closed_on_non_finite_series(tmp_path: Path):
    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return (
                b'{"data":{"result":[{"metric":'
                b'{"container_label_com_docker_compose_service":"grafana"},"value":[0,"NaN"]}]}}'
            )

    with (
        patch("toolkit.core.ops.watchdog.rightsize._resolve_prometheus_url", return_value="http://prometheus:9090"),
        patch("toolkit.core.ops.watchdog.rightsize.urllib.request.urlopen", return_value=Response()),
    ):
        assert _query_prometheus_p95("apps", root=tmp_path, window_days=7) == {}


def test_apply_safe_proposals_persists_desired_state_and_cooldown(tmp_path: Path, monkeypatch):
    from toolkit.core.config.config import Config, load_config, save_config
    from toolkit.core.config.storage import config_path
    from toolkit.core.state.audit_log import read_audit

    cfg = Config(domain="test.local", dns={"public_ip": "1.2.3.4"})
    save_config(cfg, config_path(tmp_path))
    compose = tmp_path / "generated" / "apps" / "compose.yaml"
    compose.parent.mkdir(parents=True)
    compose.write_text("services:\n  grafana:\n    image: grafana/grafana\n")
    (compose.parent / "compose.limits.yml").write_text("services:\n  grafana:\n    mem_limit: 1000m\n    cpus: 2.0\n")
    monkeypatch.setattr(
        "toolkit.core.deploy.compose_limits._vm_service_names",
        lambda _cfg, vm: ["grafana"] if vm == "apps" else [],
    )
    proposal = _proposal(current=1000, proposed=800, cpus_cur=2.0, cpus_new=1.5)

    with patch("toolkit.core.ops.watchdog.rightsize.time.time", return_value=_NOW):
        applied = apply_rightsize_proposals([proposal], root=tmp_path)

    assert applied == [proposal]
    persisted = load_config(config_path(tmp_path))
    limit = persisted.machines["apps"].resource_limits["grafana"]
    assert limit.memory_mb == 800
    assert limit.cpus == 1.5
    overlay = (tmp_path / "generated" / "apps" / "compose.limits.yml").read_text()
    assert "mem_limit: 800m" in overlay
    state = __import__("json").loads((tmp_path / ".homelab-state" / "rightsize.json").read_text())
    assert state["last_applied_at"]["apps/grafana"] == _NOW
    config_events = read_audit(tmp_path, action="config_save")
    assert config_events[-1]["actor"] == "watchdog-rightsize"


def test_apply_safe_proposals_reconciles_affected_nodes(tmp_path: Path, monkeypatch):
    from toolkit.core.config.config import Config, save_config
    from toolkit.core.config.storage import config_path

    save_config(Config(domain="test.local", dns={"public_ip": "1.2.3.4"}), config_path(tmp_path))
    compose = tmp_path / "generated" / "apps" / "compose.yaml"
    compose.parent.mkdir(parents=True)
    compose.write_text("services:\n  grafana:\n    image: grafana/grafana\n")
    (compose.parent / "compose.limits.yml").write_text("services:\n  grafana:\n    mem_limit: 1000m\n    cpus: 2.0\n")
    monkeypatch.setattr(
        "toolkit.core.deploy.compose_limits._vm_service_names",
        lambda _cfg, vm: ["grafana"] if vm == "apps" else [],
    )
    calls: list[tuple[int, tuple[str, ...]]] = []

    def reconcile(config, nodes):
        calls.append((config.machines["apps"].resource_limits.get("grafana", {}).memory_mb, nodes))
        return True

    proposal = _proposal(current=1000, proposed=800, cpus_cur=2.0, cpus_new=1.5)
    apply_rightsize_proposals([proposal], root=tmp_path, reconcile=reconcile)

    assert calls == [(800, ("apps",))]


def test_apply_safe_proposals_rolls_back_failed_reconcile(tmp_path: Path, monkeypatch):
    from toolkit.core.config.config import Config, load_config, save_config
    from toolkit.core.config.storage import config_path
    from toolkit.core.ops.watchdog.rightsize import RightsizeApplyError

    save_config(Config(domain="test.local", dns={"public_ip": "1.2.3.4"}), config_path(tmp_path))
    compose = tmp_path / "generated" / "apps" / "compose.yaml"
    compose.parent.mkdir(parents=True)
    compose.write_text("services:\n  grafana:\n    image: grafana/grafana\n")
    (compose.parent / "compose.limits.yml").write_text("services:\n  grafana:\n    mem_limit: 1000m\n    cpus: 2.0\n")
    monkeypatch.setattr(
        "toolkit.core.deploy.compose_limits._vm_service_names",
        lambda _cfg, vm: ["grafana"] if vm == "apps" else [],
    )
    observed: list[bool] = []

    def reconcile(config, _nodes):
        observed.append("grafana" in config.machines["apps"].resource_limits)
        return len(observed) > 1

    import pytest

    with pytest.raises(RightsizeApplyError, match="previous resource limits were restored"):
        apply_rightsize_proposals(
            [_proposal(current=1000, proposed=800, cpus_cur=2.0, cpus_new=1.5)],
            root=tmp_path,
            reconcile=reconcile,
        )

    assert observed == [True, False]
    assert load_config(config_path(tmp_path)).machines["apps"].resource_limits == {}
    assert not (tmp_path / ".homelab-state" / "rightsize.json").exists()


def test_apply_safe_proposals_rolls_back_when_cooldown_state_cannot_persist(tmp_path: Path, monkeypatch):
    from toolkit.core.config.config import Config, load_config, save_config
    from toolkit.core.config.storage import config_path
    from toolkit.core.ops.watchdog.rightsize import RightsizeApplyError

    save_config(Config(domain="test.local", dns={"public_ip": "1.2.3.4"}), config_path(tmp_path))
    node_dir = tmp_path / "generated" / "apps"
    node_dir.mkdir(parents=True)
    (node_dir / "compose.yaml").write_text("services:\n  grafana:\n    image: grafana/grafana\n")
    (node_dir / "compose.limits.yml").write_text("services:\n  grafana:\n    mem_limit: 1000m\n    cpus: 2.0\n")
    monkeypatch.setattr(
        "toolkit.core.deploy.compose_limits._vm_service_names",
        lambda _cfg, vm: ["grafana"] if vm == "apps" else [],
    )
    monkeypatch.setattr(
        "toolkit.core.ops.watchdog.rightsize._save_rightsize_state",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
    )
    reconciled: list[int | None] = []

    def reconcile(config, _nodes):
        limit = config.machines["apps"].resource_limits.get("grafana")
        reconciled.append(limit.memory_mb if limit else None)
        return True

    with pytest.raises(RightsizeApplyError, match="cooldown state"):
        apply_rightsize_proposals(
            [_proposal(current=1000, proposed=800, cpus_cur=2.0, cpus_new=1.5)],
            root=tmp_path,
            reconcile=reconcile,
        )

    assert reconciled == [800, None]
    assert load_config(config_path(tmp_path)).machines["apps"].resource_limits == {}


def test_apply_safe_proposals_refuses_missing_current_limit(tmp_path: Path):
    from toolkit.core.config.config import Config, save_config
    from toolkit.core.config.storage import config_path
    from toolkit.core.ops.watchdog.rightsize import RightsizeApplyError

    save_config(Config(domain="test.local", dns={"public_ip": "1.2.3.4"}), config_path(tmp_path))

    with pytest.raises(RightsizeApplyError, match="current enforced limit is unavailable"):
        apply_rightsize_proposals([_proposal(current=1000, proposed=800)], root=tmp_path)


def test_execute_approved_rightsize_uses_verified_transaction(tmp_path: Path, monkeypatch):
    from toolkit.core.config.config import Config, load_config, save_config
    from toolkit.core.config.storage import config_path
    from toolkit.core.ops.approvals import Approval, ApprovalKind, ApprovalStatus

    save_config(Config(domain="test.local", dns={"public_ip": "1.2.3.4"}), config_path(tmp_path))
    node_dir = tmp_path / "generated" / "apps"
    node_dir.mkdir(parents=True)
    (node_dir / "compose.yaml").write_text("services:\n  grafana:\n    image: grafana/grafana\n")
    (node_dir / "compose.limits.yml").write_text("services:\n  grafana:\n    mem_limit: 1000m\n    cpus: 1.0\n")
    monkeypatch.setattr(
        "toolkit.core.deploy.compose_limits._vm_service_names",
        lambda _cfg, vm: ["grafana"] if vm == "apps" else [],
    )
    approval = Approval(
        kind=ApprovalKind.RIGHTSIZE,
        service="grafana",
        current="1000 MB / 1 CPU",
        proposed="1300 MB / 1.25 CPU",
        status=ApprovalStatus.APPROVED,
        payload=RightsizeApprovalPayload(
            node="apps",
            current_mem_mb=1000,
            proposed_mem_mb=1300,
            current_cpus=1.0,
            proposed_cpus=1.25,
            p95_mem_mb=1000.0,
            p95_cpu_pct=90.0,
            stateful=False,
            reason="growth needed",
        ).model_dump(mode="json"),
    )
    reconciled: list[tuple[str, ...]] = []

    applied = execute_approved_rightsize(
        root=tmp_path,
        approval=approval,
        reconcile=lambda _config, nodes: reconciled.append(nodes) is None,
    )

    assert len(applied) == 1
    assert reconciled == [("apps",)]
    limit = load_config(config_path(tmp_path)).machines["apps"].resource_limits["grafana"]
    assert limit.memory_mb == 1300
    assert limit.cpus == 1.25


def test_execute_approved_rightsize_resumes_after_interrupted_config_write(tmp_path: Path, monkeypatch):
    from toolkit.core.config.config import Config, save_config
    from toolkit.core.config.storage import config_path
    from toolkit.core.machines.models import MachineResourceLimit
    from toolkit.core.ops.approvals import Approval, ApprovalKind, ApprovalStatus

    cfg = Config(domain="test.local", dns={"public_ip": "1.2.3.4"})
    cfg.machines["apps"].resource_limits["grafana"] = MachineResourceLimit(memory_mb=1300, cpus=1.25)
    save_config(Config.model_validate(cfg.model_dump(mode="python")), config_path(tmp_path))
    node_dir = tmp_path / "generated" / "apps"
    node_dir.mkdir(parents=True)
    (node_dir / "compose.yaml").write_text("services:\n  grafana:\n    image: grafana/grafana\n")
    (node_dir / "compose.limits.yml").write_text("services:\n  grafana:\n    mem_limit: 1300m\n    cpus: 1.25\n")
    monkeypatch.setattr(
        "toolkit.core.deploy.compose_limits._vm_service_names",
        lambda _cfg, vm: ["grafana"] if vm == "apps" else [],
    )
    payload = RightsizeApprovalPayload(
        node="apps",
        current_mem_mb=1000,
        proposed_mem_mb=1300,
        current_cpus=1.0,
        proposed_cpus=1.25,
        p95_mem_mb=1000,
        p95_cpu_pct=90,
        stateful=False,
        reason="growth needed",
    )
    approval = Approval(
        kind=ApprovalKind.RIGHTSIZE,
        service="grafana",
        current="1000 MB / 1 CPU",
        proposed="1300 MB / 1.25 CPU",
        status=ApprovalStatus.APPROVED,
        payload=payload.model_dump(mode="json"),
    )

    result = execute_approved_rightsize(
        root=tmp_path,
        approval=approval,
        reconcile=lambda _config, _nodes: True,
    )

    assert len(result) == 1


def test_compute_proposals_fails_closed_on_corrupt_state(tmp_path: Path):
    state = tmp_path / ".homelab-state" / "rightsize.json"
    state.parent.mkdir()
    state.write_text("not-json")
    telemetry = {
        "grafana": {
            "p95_mem_mb": 600.0,
            "p95_cpu_pct": 130.0,
            "sample_count": 25_000,
            "current_mem_mb": 1000,
            "current_cpus": 2.0,
            "memory_floor_mb": 128,
            "cpu_floor": 0.1,
        }
    }

    import pytest

    with pytest.raises(RuntimeError, match="refusing automatic changes"):
        compute_rightsize_proposals(vm="apps", root=tmp_path, telemetry=telemetry)


@pytest.mark.parametrize("value", [float("nan"), float("inf"), -1.0])
def test_compute_proposals_fails_closed_on_invalid_cooldown_timestamp(tmp_path: Path, value: float):
    state = tmp_path / ".homelab-state" / "rightsize.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"last_applied_at": {"apps/grafana": value}}))

    telemetry = {
        "grafana": {
            "p95_mem_mb": 600.0,
            "p95_cpu_pct": 50.0,
            "sample_count": 25_000,
            "current_mem_mb": 1000,
            "current_cpus": 1.0,
            "memory_floor_mb": 128,
            "cpu_floor": 0.1,
        }
    }

    with pytest.raises(RuntimeError, match="refusing automatic changes"):
        compute_rightsize_proposals(vm="apps", root=tmp_path, telemetry=telemetry)


def test_compute_proposals_refuses_symlinked_cooldown_state(tmp_path: Path):
    target = tmp_path / "elsewhere.json"
    target.write_text('{"last_applied_at": {}}')
    state = tmp_path / ".homelab-state" / "rightsize.json"
    state.parent.mkdir()
    state.symlink_to(target)

    telemetry = {
        "grafana": {
            "p95_mem_mb": 600.0,
            "p95_cpu_pct": 50.0,
            "sample_count": 25_000,
            "current_mem_mb": 1000,
            "current_cpus": 1.0,
            "memory_floor_mb": 128,
            "cpu_floor": 0.1,
        }
    }

    with pytest.raises(RuntimeError, match="refusing automatic changes"):
        compute_rightsize_proposals(vm="apps", root=tmp_path, telemetry=telemetry)


def test_compute_proposals_stateful_no_shrink(tmp_path: Path):
    (tmp_path / "generated" / "infra").mkdir(parents=True)
    (tmp_path / "generated" / "infra" / "compose.limits.yml").write_text(
        "services:\n  postgres:\n    mem_limit: 2000m\n    cpus: 2.0\n"
    )
    telemetry = {
        "postgres": {
            "p95_mem_mb": 100.0,
            "p95_cpu_pct": 5.0,
            "sample_count": 25_000,
            "stateful": True,
            "current_mem_mb": 2000,
            "current_cpus": 2.0,
            "memory_floor_mb": 512,
        }
    }
    proposals = compute_rightsize_proposals(vm="infra", root=tmp_path, telemetry=telemetry)
    assert len(proposals) == 1
    p = proposals[0]
    # Stateful: proposed clamped to current (no shrink below current).
    assert p.proposed_mem_mb == 2000
    assert p.safe_to_apply is False
    assert p.blocked_reason == "stateful-service"


def test_compute_proposals_caps_large_shrink_to_safe_step(tmp_path: Path):
    (tmp_path / "generated" / "apps").mkdir(parents=True)
    (tmp_path / "generated" / "apps" / "compose.limits.yml").write_text(
        "services:\n  tiny:\n    mem_limit: 1000m\n    cpus: 1.0\n"
    )
    telemetry = {
        "tiny": {
            "p95_mem_mb": 50.0,  # ceil(50 * 1.3) = 65 < floor
            "p95_cpu_pct": 5.0,
            "sample_count": 25_000,
            "stateful": False,
            "current_mem_mb": 1000,
            "current_cpus": 1.0,
            "memory_floor_mb": 256,  # floor clamps the shrink
        }
    }
    proposals = compute_rightsize_proposals(vm="apps", root=tmp_path, telemetry=telemetry)
    p = proposals[0]
    assert p.proposed_mem_mb == 750
    assert p.proposed_cpus == 0.75
    assert p.safe_to_apply is True


def test_compute_proposals_skips_noop(tmp_path: Path):
    telemetry = {
        "grafana": {
            "p95_mem_mb": 1000 / 1.3,
            "p95_cpu_pct": 100 / 1.3,
            "sample_count": 25_000,
            "current_mem_mb": 1000,
            "current_cpus": 1.0,
            "memory_floor_mb": 128,
            "cpu_floor": 0.1,
        }
    }

    assert compute_rightsize_proposals(vm="apps", root=tmp_path, telemetry=telemetry) == []


def test_compute_proposals_blocks_cpu_grow_even_when_memory_safe(tmp_path: Path):
    (tmp_path / "generated" / "apps").mkdir(parents=True)
    (tmp_path / "generated" / "apps" / "compose.limits.yml").write_text(
        "services:\n  grafana:\n    mem_limit: 1000m\n    cpus: 1.0\n"
    )
    telemetry = {
        "grafana": {
            "p95_mem_mb": 600.0,  # ceil(600 * 1.3) = 780; memory shrink is safe.
            "p95_cpu_pct": 125.0,  # proposes 1.5 CPUs, a grow from 1.0.
            "sample_count": 25_000,
            "stateful": False,
            "current_mem_mb": 1000,
            "current_cpus": 1.0,
            "memory_floor_mb": 128,
        }
    }
    proposals = compute_rightsize_proposals(vm="apps", root=tmp_path, telemetry=telemetry)
    assert proposals[0].proposed_mem_mb == 780
    assert proposals[0].proposed_cpus == 1.62
    assert proposals[0].safe_to_apply is False


def test_compute_proposals_empty_telemetry(tmp_path: Path):
    proposals = compute_rightsize_proposals(vm="apps", root=tmp_path, telemetry={})
    assert proposals == []


def test_compute_proposals_kill_switch_off(tmp_path: Path):
    cfg = RightsizeConfig(enabled=False)
    telemetry = {
        "x": {
            "p95_mem_mb": 1.0,
            "p95_cpu_pct": 0.0,
            "stateful": False,
            "current_mem_mb": 100,
            "current_cpus": 1.0,
            "memory_floor_mb": 0,
        }
    }
    assert compute_rightsize_proposals(vm="apps", root=tmp_path, cfg=cfg, telemetry=telemetry) == []


def test_rightsize_policy_is_manifest_configurable():
    from toolkit.core.config.config import Config

    cfg = Config(
        service_settings={
            "homelab-ui": {
                "rightsize-enabled": False,
                "rightsize-headroom-percent": 150,
                "rightsize-max-step-percent": 15,
                "rightsize-cooldown-hours": 48,
                "rightsize-telemetry-days": 14,
                "rightsize-minimum-samples-per-minute": 3,
            }
        }
    )

    policy = rightsize_config_from_desired_state(cfg)

    assert policy == RightsizeConfig(
        enabled=False,
        headroom_factor=1.5,
        max_step_pct=15,
        cooldown_hours=48,
        default_telemetry_window_days=14,
        minimum_samples_per_minute=3,
    )


# --- CLI smoke -------------------------------------------------------------


def test_watchdog_rightsize_subcommand_exists_and_no_telemetry(tmp_path: Path):
    runner = CliRunner()
    # Stub the telemetry fetcher so the CLI never hits a real Prometheus
    # (load_config defaults infra→10.10.10.10 even for an empty tmp root).
    with patch("toolkit.core.ops.watchdog.rightsize._query_prometheus_p95", return_value={}):
        result = runner.invoke(main, ["--root", str(tmp_path), "watchdog", "rightsize", "--dry-run", "--node", "apps"])
    assert result.exit_code == 0, f"CLI failed: {result.output}\n{result.exception}"
    assert "No rightsizing proposals" in result.output


def test_watchdog_rightsize_help_lists_flags(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "watchdog", "rightsize", "--help"])
    assert result.exit_code == 0, result.exception
    for flag in ("--dry-run", "--apply", "--node", "--json"):
        assert flag in result.output


def test_watchdog_rightsize_rejects_conflicting_output_modes(tmp_path: Path):
    runner = CliRunner()

    both_modes = runner.invoke(
        main,
        ["--root", str(tmp_path), "watchdog", "rightsize", "--apply", "--dry-run"],
    )
    json_apply = runner.invoke(
        main,
        ["--root", str(tmp_path), "watchdog", "rightsize", "--apply", "--json"],
    )

    assert both_modes.exit_code != 0
    assert "cannot be combined" in both_modes.output
    assert json_apply.exit_code != 0
    assert "cannot be combined" in json_apply.output


def test_watchdog_rightsize_apply_reconciles_before_success(tmp_path: Path, monkeypatch):
    from toolkit.core.config.config import Config, save_config
    from toolkit.core.config.storage import config_path

    save_config(Config(domain="test.local", dns={"public_ip": "1.2.3.4"}), config_path(tmp_path))
    compose = tmp_path / "generated" / "apps" / "compose.yaml"
    compose.parent.mkdir(parents=True)
    compose.write_text("services:\n  grafana:\n    image: grafana/grafana\n")
    (compose.parent / "compose.limits.yml").write_text("services:\n  grafana:\n    mem_limit: 1000m\n    cpus: 2.0\n")
    telemetry = {
        "grafana": {
            "p95_mem_mb": 600.0,
            "p95_cpu_pct": 130.0,
            "sample_count": 25_000,
            "stateful": False,
            "current_mem_mb": 1000,
            "current_cpus": 2.0,
            "memory_floor_mb": 128,
            "cpu_floor": 0.1,
        }
    }
    monkeypatch.setattr(
        "toolkit.core.deploy.compose_limits._vm_service_names",
        lambda _cfg, vm: ["grafana"] if vm == "apps" else [],
    )
    reconciled: list[tuple[str, ...]] = []

    def reconcile(_root, _config, nodes, *, on_log=None):
        reconciled.append(nodes)
        return True

    with (
        patch("toolkit.core.ops.watchdog.rightsize._query_prometheus_p95", return_value=telemetry),
        patch("toolkit.core.ops.watchdog.rightsize.reconcile_rightsize_nodes", side_effect=reconcile),
    ):
        result = CliRunner().invoke(
            main,
            ["--root", str(tmp_path), "watchdog", "rightsize", "--apply", "--node", "apps"],
        )

    assert result.exit_code == 0, (result.output, result.exception)
    assert reconciled == [("apps",)]
    assert "Applied and verified 1 safe proposal" in result.output
