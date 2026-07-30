"""Provider-dispatched isolated database restore drills and recovery checkpoints."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.core.config.roles import deployed_roles, uses_remote_nodes
from toolkit.core.deploy.destructive_guard import record_verified_checkpoint, write_sensitive_file
from toolkit.core.deploy.operation_lease import OperationLease
from toolkit.core.ops.database_provider import primary_database_node, primary_database_provider
from toolkit.core.ops.dump_repository import DumpRecord
from toolkit.core.state.audit_log import AuditAction, audit

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


@dataclass(frozen=True, slots=True)
class RestoreDrillResult:
    ok: bool
    message: str
    database_count: int = 0
    evidence_path: Path | None = None
    checkpoint_id: str | None = None


def run_restore_drill(
    cfg: Config,
    root: Path,
    record: DumpRecord,
    *,
    actor: str = "system",
    vm: str | None = None,
) -> RestoreDrillResult:
    """Restore a dump through its provider and issue a verified checkpoint."""
    root = root.resolve()
    remote = uses_remote_nodes(cfg)
    if record.is_remote != remote:
        return RestoreDrillResult(False, "dump location does not match the configured deployment mode")

    provider = primary_database_provider(cfg)
    node = primary_database_node(cfg, provider, vm)
    started = time.monotonic()
    audit(
        root,
        AuditAction.RESTORE,
        actor=actor,
        detail=f"isolated restore drill started: {record.dump_id}",
        vm=node,
        extra={
            "database_provider": provider.manifest.name,
            "dump_id": record.dump_id,
            "sha256": record.sha256,
        },
    )
    lease: OperationLease | None = None
    try:
        lease = OperationLease.acquire(root, "restore-drill")
        ok, database_count, error = provider.plugin.run_database_restore_drill(cfg, root, record, vm=node)
        if not ok or database_count < 1:
            raise RuntimeError(error or "isolated database restore or verification query failed")

        evidence = {
            "database_count": database_count,
            "database_provider": provider.manifest.name,
            "dump_id": record.dump_id,
            "dump_name": record.name,
            "dump_sha256": record.sha256,
            "ok": True,
            "verified_at": datetime.now(UTC).isoformat(),
            "vm": node,
        }
        evidence_path = root / ".homelab-state" / "restore-drills" / f"{int(time.time())}-{record.dump_id}.json"
        write_sensitive_file(evidence_path, json.dumps(evidence, indent=2, sort_keys=True) + "\n")
        checkpoint = record_verified_checkpoint(root, deployed_roles(cfg), (evidence_path,))
        result = RestoreDrillResult(
            True,
            f"isolated restore verified {database_count} connectable database(s)",
            database_count,
            evidence_path,
            checkpoint.checkpoint_id,
        )
    except Exception as exc:
        result = RestoreDrillResult(False, str(exc))
    finally:
        if lease is not None:
            lease.release()

    audit(
        root,
        AuditAction.RESTORE,
        actor=actor,
        ok=result.ok,
        detail=f"isolated restore drill {'completed' if result.ok else 'failed'}: {record.dump_id}",
        vm=node,
        duration_s=time.monotonic() - started,
        extra={
            "database_provider": provider.manifest.name,
            "dump_id": record.dump_id,
            "sha256": record.sha256,
        },
    )
    return result
