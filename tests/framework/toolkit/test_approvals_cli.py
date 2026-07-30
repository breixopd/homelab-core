"""Operational approval CLI lifecycle."""

from __future__ import annotations

import json
import time
from pathlib import Path

from click.testing import CliRunner
from toolkit.cli import main
from toolkit.core.ops.approvals import ApprovalKind, ApprovalStatus, ApprovalStore


def test_approvals_list_empty(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "approvals", "list"])
    assert result.exit_code == 0, result.exception
    assert "No actionable approvals" in result.output


def test_approvals_list_reports_corrupt_queue_without_traceback(tmp_path: Path):
    queue = tmp_path / ".homelab-state" / "approvals.json"
    queue.parent.mkdir()
    queue.write_text("not-json")

    result = CliRunner().invoke(main, ["--root", str(tmp_path), "approvals", "list"])

    assert result.exit_code != 0
    assert "approval queue is unreadable" in result.output
    assert "Traceback" not in result.output


def test_approvals_list_shows_pending(tmp_path: Path):
    store = ApprovalStore(root=tmp_path)
    store.enqueue(ApprovalKind.RIGHTSIZE, "postgres", "1g", "2g", reason="grew")
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "approvals", "list"])
    assert result.exit_code == 0, result.exception
    assert "postgres" in result.output
    assert "1g" in result.output and "2g" in result.output


def test_approvals_reject_lifecycle(tmp_path: Path):
    store = ApprovalStore(root=tmp_path)
    a = store.enqueue(ApprovalKind.RIGHTSIZE, "grafana", "1 CPU", "1.25 CPU")
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "approvals", "reject", a.id, "--reason", "not now"])
    assert result.exit_code == 0, result.exception
    assert "Rejected" in result.output
    store2 = ApprovalStore(root=tmp_path)
    rejected = store2.rejected()
    assert len(rejected) == 1
    assert rejected[0].status is ApprovalStatus.REJECTED


def test_approvals_approve_unknown_id_errors(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "approvals", "approve", "nonexistent"])
    assert result.exit_code != 0
    assert "No pending approval" in result.output


def test_rightsize_applies_enqueues_risky_to_approvals(tmp_path: Path):
    """Applying safe proposals also queues changes needing explicit approval."""
    from unittest.mock import patch

    (tmp_path / "generated" / "infra").mkdir(parents=True)
    (tmp_path / "generated" / "infra" / "compose.limits.yml").write_text(
        "services:\n  postgres:\n    mem_limit: 5000m\n    cpus: 2.0\n"  # stateful, big shrink needed
    )
    telemetry = {
        "postgres": {
            "p95_mem_mb": 100.0,  # ceil(100 * 1.3) = 130 — but floor 512, and stateful clamp to current
            "p95_cpu_pct": 5.0,
            "sample_count": 25_000,
            "stateful": True,
            "current_mem_mb": 5000,
            "current_cpus": 2.0,
            "memory_floor_mb": 512,
            "cpu_floor": 0.1,
        }
    }
    runner = CliRunner()
    with patch("toolkit.core.ops.watchdog.rightsize._query_prometheus_p95", return_value=telemetry):
        result = runner.invoke(main, ["--root", str(tmp_path), "watchdog", "rightsize", "--apply", "--node", "infra"])
    assert result.exit_code == 0, (result.output, result.exception)
    assert "enqueued" in result.output
    # The risky proposal should now be in the approval queue.
    store = ApprovalStore(root=tmp_path)
    pending = store.pending(kind=ApprovalKind.RIGHTSIZE)
    assert len(pending) == 1
    assert pending[0].service == "postgres"
    assert pending[0].payload["node"] == "infra"


def test_rightsize_reports_corrupt_approval_queue(tmp_path: Path):
    from unittest.mock import patch

    queue = tmp_path / ".homelab-state" / "approvals.json"
    queue.parent.mkdir()
    queue.write_text("not-json")
    telemetry = {
        "postgres": {
            "p95_mem_mb": 100.0,
            "p95_cpu_pct": 5.0,
            "sample_count": 25_000,
            "stateful": True,
            "current_mem_mb": 5000,
            "current_cpus": 2.0,
            "memory_floor_mb": 512,
            "cpu_floor": 0.1,
        }
    }

    with patch("toolkit.core.ops.watchdog.rightsize._query_prometheus_p95", return_value=telemetry):
        result = CliRunner().invoke(
            main,
            ["--root", str(tmp_path), "watchdog", "rightsize", "--apply", "--node", "infra"],
        )

    assert result.exit_code != 0
    assert "approval queue is unreadable" in result.output
    assert "Traceback" not in result.output


def test_rightsize_does_not_queue_cooldown_deferred_change(tmp_path: Path):
    from unittest.mock import patch

    state = tmp_path / ".homelab-state" / "rightsize.json"
    state.parent.mkdir()
    state.write_text(json.dumps({"last_applied_at": {"apps/grafana": time.time()}}))
    telemetry = {
        "grafana": {
            "p95_mem_mb": 600.0,
            "p95_cpu_pct": 50.0,
            "sample_count": 25_000,
            "stateful": False,
            "current_mem_mb": 1000,
            "current_cpus": 1.0,
            "memory_floor_mb": 128,
            "cpu_floor": 0.1,
        }
    }

    with patch("toolkit.core.ops.watchdog.rightsize._query_prometheus_p95", return_value=telemetry):
        result = CliRunner().invoke(
            main,
            ["--root", str(tmp_path), "watchdog", "rightsize", "--apply", "--node", "apps"],
        )

    assert result.exit_code == 0, (result.output, result.exception)
    assert "0 guarded proposal(s) enqueued" in result.output
    assert ApprovalStore(root=tmp_path).all() == []


def test_approving_rightsize_executes_and_records_outcome(tmp_path: Path):
    from unittest.mock import patch

    approval = ApprovalStore(root=tmp_path).enqueue(
        ApprovalKind.RIGHTSIZE,
        "grafana",
        "1000 MB / 1 CPU",
        "1300 MB / 1.25 CPU",
        payload={"node": "apps"},
    )

    with patch("toolkit.core.ops.watchdog.rightsize.execute_approved_rightsize", return_value=[object()]):
        result = CliRunner().invoke(main, ["--root", str(tmp_path), "approvals", "approve", approval.id])

    assert result.exit_code == 0, (result.output, result.exception)
    assert "Approved, applied, and verified" in result.output
    executed = ApprovalStore(root=tmp_path).executed()
    assert len(executed) == 1
    assert executed[0].outcome == {"success": True, "detail": "applied and verified"}


def test_execute_retries_rightsize_left_approved_after_interruption(tmp_path: Path):
    from unittest.mock import patch

    store = ApprovalStore(root=tmp_path)
    approval = store.enqueue(
        ApprovalKind.RIGHTSIZE,
        "grafana",
        "1000 MB / 1 CPU",
        "1300 MB / 1.25 CPU",
        payload={"node": "apps"},
    )
    store.approve(approval.id, decided_by="cli-operator")

    with patch("toolkit.core.ops.watchdog.rightsize.execute_approved_rightsize", return_value=[object()]):
        result = CliRunner().invoke(main, ["--root", str(tmp_path), "approvals", "execute", approval.id])

    assert result.exit_code == 0, (result.output, result.exception)
    assert "Applied and verified" in result.output
    assert ApprovalStore(root=tmp_path).executed()[0].outcome["success"] is True


def test_approvals_list_shows_retry_command_for_approved_request(tmp_path: Path):
    store = ApprovalStore(root=tmp_path)
    approval = store.enqueue(ApprovalKind.RIGHTSIZE, "grafana", "1 CPU", "1.25 CPU")
    store.approve(approval.id, decided_by="cli-operator")

    result = CliRunner().invoke(main, ["--root", str(tmp_path), "approvals", "list"])

    assert result.exit_code == 0
    assert f"approvals execute {approval.id}" in result.output
