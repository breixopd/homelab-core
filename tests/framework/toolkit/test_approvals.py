"""Durable operational approval queue."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from toolkit.core.ops.approvals import (
    Approval,
    ApprovalKind,
    ApprovalPersistenceError,
    ApprovalStatus,
    ApprovalStore,
)

# --- Approval dataclass ----------------------------------------------------


def test_approval_defaults_request_state():
    a = Approval(kind=ApprovalKind.RIGHTSIZE, service="grafana", current="10.0", proposed="10.1")
    assert a.status is ApprovalStatus.REQUESTED
    assert a.id  # auto-generated
    assert a.requested_at > 0
    assert a.decided_at is None
    assert a.outcome is None


def test_approval_id_is_stable_string():
    a = Approval(kind=ApprovalKind.RIGHTSIZE, service="postgres", current="1g", proposed="2g")
    assert isinstance(a.id, str)
    assert len(a.id) == 32
    # Round-trip preserves the id.
    d = a.to_dict()
    restored = Approval.from_dict(d)
    assert restored.id == a.id


def test_approval_to_dict_roundtrip():
    a = Approval(
        kind=ApprovalKind.RIGHTSIZE,
        service="grafana",
        current="1 CPU",
        proposed="1.25 CPU",
        reason="observed sustained demand",
    )
    d = a.to_dict()
    assert d["kind"] == "rightsize"
    assert d["status"] == "requested"
    restored = Approval.from_dict(d)
    assert restored.kind is ApprovalKind.RIGHTSIZE
    assert restored.status is ApprovalStatus.REQUESTED
    assert restored.reason == "observed sustained demand"


def test_approval_payload_roundtrip():
    payload = {"node": "infra", "memory_mb": 2048, "cpus": 2.0}
    restored = Approval.from_dict(
        Approval(
            kind=ApprovalKind.RIGHTSIZE,
            service="postgres",
            current="1g",
            proposed="2g",
            payload=payload,
        ).to_dict()
    )
    assert restored.payload == payload


# --- ApprovalStore lifecycle ----------------------------------------------


def test_store_enqueue_assigns_unique_id(tmp_path: Path):
    store = ApprovalStore(root=tmp_path)
    a1 = store.enqueue(ApprovalKind.RIGHTSIZE, "grafana", "10.0", "10.1")
    a2 = store.enqueue(ApprovalKind.RIGHTSIZE, "grafana", "10.0", "10.1")
    assert a1.id != a2.id
    assert len(store.pending()) == 2


def test_store_persists_across_instances(tmp_path: Path):
    s1 = ApprovalStore(root=tmp_path)
    s1.enqueue(ApprovalKind.RIGHTSIZE, "postgres", "1g", "2g", reason="grew")
    # New instance reads the same queue file.
    s2 = ApprovalStore(root=tmp_path)
    pending = s2.pending()
    assert len(pending) == 1
    assert pending[0].service == "postgres"
    assert pending[0].reason == "grew"
    assert s1.queue_path == tmp_path / ".homelab-state" / "approvals.json"
    assert s1.queue_path.stat().st_mode & 0o777 == 0o600


def test_store_serializes_mutations_from_stale_instances(tmp_path: Path):
    first = ApprovalStore(root=tmp_path)
    second = ApprovalStore(root=tmp_path)

    first.enqueue(ApprovalKind.RIGHTSIZE, "grafana", "10.0", "10.1")
    second.enqueue(ApprovalKind.RIGHTSIZE, "postgres", "1g", "2g")

    assert {entry.service for entry in ApprovalStore(root=tmp_path).pending()} == {"grafana", "postgres"}


def test_store_fails_closed_on_corrupt_queue(tmp_path: Path):
    queue = tmp_path / ".homelab-state" / "approvals.json"
    queue.parent.mkdir()
    queue.write_text("not-json")

    with pytest.raises(ApprovalPersistenceError, match="unreadable"):
        ApprovalStore(root=tmp_path)


def test_store_fails_closed_on_oversized_queue(tmp_path: Path):
    queue = tmp_path / ".homelab-state" / "approvals.json"
    queue.parent.mkdir()
    queue.write_bytes(b" " * (ApprovalStore.MAX_QUEUE_BYTES + 1))

    with pytest.raises(ApprovalPersistenceError, match="size limit"):
        ApprovalStore(root=tmp_path)


def test_store_refuses_symlinked_queue(tmp_path: Path):
    target = tmp_path / "elsewhere.json"
    target.write_text('{"entries": []}')
    queue = tmp_path / ".homelab-state" / "approvals.json"
    queue.parent.mkdir()
    queue.symlink_to(target)

    with pytest.raises(ApprovalPersistenceError, match="opened safely"):
        ApprovalStore(root=tmp_path)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("requested_at", float("nan")),
        ("service", "not/a-service"),
        ("reason", "x" * 501),
        ("payload", {"value": "x" * 33_000}),
    ],
)
def test_store_fails_closed_on_invalid_entry(tmp_path: Path, field: str, value: object):
    entry = Approval(kind=ApprovalKind.RIGHTSIZE, service="grafana", current="1g", proposed="2g").to_dict()
    entry[field] = value
    queue = tmp_path / ".homelab-state" / "approvals.json"
    queue.parent.mkdir()
    queue.write_text(json.dumps({"entries": [entry]}))

    with pytest.raises(ApprovalPersistenceError, match="invalid entry"):
        ApprovalStore(root=tmp_path)


def test_store_approve_moves_to_approved(tmp_path: Path):
    store = ApprovalStore(root=tmp_path)
    a = store.enqueue(ApprovalKind.RIGHTSIZE, "grafana", "10.0", "10.1")
    store.approve(a.id, decided_by="alice")
    pending = store.pending()
    approved = store.approved()
    assert pending == []
    assert len(approved) == 1
    assert approved[0].status is ApprovalStatus.APPROVED
    assert approved[0].decided_by == "alice"
    assert approved[0].decided_at > 0


def test_store_reject_moves_to_rejected(tmp_path: Path):
    store = ApprovalStore(root=tmp_path)
    a = store.enqueue(ApprovalKind.RIGHTSIZE, "apps", "down", "up")
    store.reject(a.id, decided_by="bob", reason="maintenance window ended")
    pending = store.pending()
    rejected = store.rejected()
    assert pending == []
    assert len(rejected) == 1
    assert rejected[0].status is ApprovalStatus.REJECTED
    assert rejected[0].decided_by == "bob"
    assert rejected[0].decision_reason == "maintenance window ended"


def test_store_reject_refuses_unbounded_reason_without_corrupting_queue(tmp_path: Path):
    store = ApprovalStore(root=tmp_path)
    approval = store.enqueue(ApprovalKind.RIGHTSIZE, "apps", "down", "up")

    with pytest.raises(ApprovalPersistenceError, match="persisted"):
        store.reject(approval.id, decided_by="bob", reason="x" * 501)

    reloaded = ApprovalStore(root=tmp_path)
    assert reloaded.pending()[0].id == approval.id


def test_store_record_outcome_marks_executed(tmp_path: Path):
    store = ApprovalStore(root=tmp_path)
    a = store.enqueue(ApprovalKind.RIGHTSIZE, "grafana", "10.0", "10.1")
    store.approve(a.id, decided_by="alice")
    store.record_outcome(a.id, success=True, detail="applied + verified")
    executed = store.executed()
    assert len(executed) == 1
    assert executed[0].status is ApprovalStatus.EXECUTED
    assert executed[0].outcome == {"success": True, "detail": "applied + verified"}


def test_store_record_outcome_rollback(tmp_path: Path):
    # The differentiator: auto-rollback on verify-failure records outcome=False.
    store = ApprovalStore(root=tmp_path)
    a = store.enqueue(ApprovalKind.RIGHTSIZE, "postgres", "1g", "500m")
    store.approve(a.id, decided_by="alice")
    store.record_outcome(a.id, success=False, detail="verify failed: OOM, rolled back")
    executed = store.executed()
    assert executed[0].outcome["success"] is False
    assert "rolled back" in executed[0].outcome["detail"]


def test_store_approve_unknown_id_no_op(tmp_path: Path):
    store = ApprovalStore(root=tmp_path)
    # Approving a non-existent id is a no-op (best-effort; idempotent re-clicks).
    store.approve("nonexistent", decided_by="alice")
    assert store.pending() == []
    assert store.approved() == []


def test_store_filter_by_kind(tmp_path: Path):
    store = ApprovalStore(root=tmp_path)
    store.enqueue(ApprovalKind.RIGHTSIZE, "grafana", "10.0", "10.1")
    store.enqueue(ApprovalKind.RIGHTSIZE, "postgres", "1g", "2g")
    store.enqueue(ApprovalKind.RIGHTSIZE, "apps", "down", "up")
    rightsizing = store.pending(kind=ApprovalKind.RIGHTSIZE)
    assert {approval.service for approval in rightsizing} == {"grafana", "postgres", "apps"}


def test_store_prunes_old_executed(tmp_path: Path):
    # Executed/rejected entries older than retention_days are pruned on load.
    store = ApprovalStore(root=tmp_path, retention_days=7)
    a = store.enqueue(ApprovalKind.RIGHTSIZE, "grafana", "10.0", "10.1")
    store.approve(a.id, decided_by="alice")
    store.record_outcome(a.id, success=True, detail="ok")
    # Backdate the queue file's entries by 30 days.
    queue_path = tmp_path / ".homelab-state" / "approvals.json"
    data = json.loads(queue_path.read_text())
    old = time.time() - (30 * 86400)
    for entry in data["entries"]:
        entry["requested_at"] = old
        if entry.get("decided_at"):
            entry["decided_at"] = old
    queue_path.write_text(json.dumps(data))
    # New instance loads + prunes.
    store2 = ApprovalStore(root=tmp_path, retention_days=7)
    assert store2.executed() == []
    assert store2.pending() == []
