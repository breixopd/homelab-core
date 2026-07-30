from __future__ import annotations

import json

from toolkit.core.config.config import Config
from toolkit.core.registry.reconcile import build_discovery_snapshot, write_last_reconcile


def test_build_discovery_snapshot_includes_services(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text(
        "services:\n  postgres:\n    image: pg:16\n  redis:\n    image: redis:7\n    depends_on: [postgres]\n"
    )
    cfg = Config(domain="example.com")
    snap = build_discovery_snapshot(cfg, tmp_path)
    assert snap["service_count"] == 2
    assert snap["services"] == ["postgres", "redis"]
    assert snap["dependency_edges"] == 1


def test_write_last_reconcile_tracks_fingerprint_diff(tmp_path):
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services:\n  postgres:\n    image: pg:16\n")
    cfg = Config(domain="example.com")

    first = write_last_reconcile(tmp_path, cfg)
    doc1 = json.loads(first.read_text())
    assert doc1["validate"]["ok"] is True
    assert doc1["previous_fingerprint"] == ""
    assert doc1["idempotent"] is False

    second = write_last_reconcile(tmp_path, cfg)
    doc2 = json.loads(second.read_text())
    assert doc2["previous_fingerprint"] == doc1["desired_fingerprint"]
    assert doc2["idempotent"] is True
    assert doc2["diff"]["discovery_changed"] == []

    compose.write_text("services:\n  postgres:\n    image: pg:16\n  redis:\n    image: redis:7\n")
    third = write_last_reconcile(tmp_path, cfg)
    doc3 = json.loads(third.read_text())
    assert doc3["idempotent"] is False
    assert "service_count" in doc3["diff"]["discovery_changed"]
