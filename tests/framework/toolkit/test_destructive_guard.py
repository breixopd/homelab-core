from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from toolkit.core.deploy.destructive_guard import (
    RecoveryCheckpointRequiredError,
    ResourcesStillPresentError,
    assert_destroyed,
    record_verified_checkpoint,
    require_verified_checkpoint,
)


def test_verified_checkpoint_requires_hashed_evidence(tmp_path: Path):
    evidence = tmp_path / "restore-drill.json"
    evidence.write_text('{"ok": true}\n')
    recorded = record_verified_checkpoint(tmp_path, ["infra", "apps"], [evidence])
    loaded = require_verified_checkpoint(tmp_path, ["apps"], timedelta(days=1))
    assert loaded.checkpoint_id == recorded.checkpoint_id
    assert len(next(iter(loaded.evidence.values()))) == 64


def test_checkpoint_without_evidence_is_rejected(tmp_path: Path):
    with pytest.raises(RecoveryCheckpointRequiredError):
        record_verified_checkpoint(tmp_path, ["infra"], [])


def test_checkpoint_rejects_evidence_changed_after_restore_drill(tmp_path: Path):
    evidence = tmp_path / "restore-drill.json"
    evidence.write_text('{"ok": true}\n')
    record_verified_checkpoint(tmp_path, ["infra"], [evidence])
    evidence.write_text('{"ok": false}\n')

    with pytest.raises(RecoveryCheckpointRequiredError, match="changed"):
        require_verified_checkpoint(tmp_path, ["infra"], timedelta(days=1))


def test_checkpoint_rejects_future_verification_timestamp(tmp_path: Path):
    evidence = tmp_path / "restore-drill.json"
    evidence.write_text('{"ok": true}\n')
    record_verified_checkpoint(tmp_path, ["infra"], [evidence])
    checkpoint_path = tmp_path / ".homelab-state" / "checkpoints" / "latest.json"
    payload = json.loads(checkpoint_path.read_text())
    payload["verified_at"] = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    checkpoint_path.write_text(json.dumps(payload))

    with pytest.raises(RecoveryCheckpointRequiredError, match="future"):
        require_verified_checkpoint(tmp_path, ["infra"], timedelta(days=1))


def test_destroy_verification_rejects_remaining_target():
    with pytest.raises(ResourcesStillPresentError):
        assert_destroyed(["apps-01"], ["infra-01", "apps-01"])
