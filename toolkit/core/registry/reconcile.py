"""DYN-A-lite: discovery snapshot + fingerprint diff written on generate."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from toolkit.core.registry.service_graph import ServiceGraph

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


def _fingerprint(payload: dict[str, Any]) -> str:
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(blob).hexdigest()


def build_discovery_snapshot(cfg: Config, root: Path) -> dict[str, Any]:
    """Structured discovery index for reconcile fingerprints."""
    compose_path = root / "docker-compose.yml"
    graph = ServiceGraph.from_compose(compose_path) if compose_path.is_file() else ServiceGraph(nodes={})
    dep_map = graph.dependency_map()
    return {
        "enabled_categories": sorted(cfg.enabled_categories),
        "enabled_nodes": sorted(cfg.enabled_nodes),
        "domain": cfg.domain,
        "service_count": len(graph.nodes),
        "services": sorted(graph.nodes.keys()),
        "dependency_edges": sum(len(v) for v in dep_map.values()),
    }


def write_last_reconcile(root: Path, cfg: Config, *, trigger: str = "generate") -> Path:
    """Write `.homelab-state/last-reconcile.json` with discovery diff vs previous run."""
    state_dir = root / ".homelab-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    out_path = state_dir / "last-reconcile.json"

    discovery = build_discovery_snapshot(cfg, root)
    desired = _fingerprint(discovery)

    previous = ""
    discovery_changed: list[str] = []
    if out_path.is_file():
        try:
            prev_doc = json.loads(out_path.read_text())
            previous = str(prev_doc.get("desired_fingerprint") or "")
            old_discovery = prev_doc.get("discovery") or {}
            for key, value in discovery.items():
                if old_discovery.get(key) != value:
                    discovery_changed.append(key)
        except (json.JSONDecodeError, OSError):
            pass

    doc = {
        "reconcile_id": datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ"),
        "trigger": trigger,
        "discovery": discovery,
        "desired_fingerprint": desired,
        "previous_fingerprint": previous,
        "diff": {
            "discovery_changed": discovery_changed,
            "fingerprint_changed": desired != previous if previous else bool(discovery_changed),
        },
        "validate": {"ok": True},
        "idempotent": desired == previous if previous else False,
    }
    out_path.write_text(json.dumps(doc, indent=2) + "\n")
    return out_path
