"""Provider-dispatched pre-deploy database dump and restore safety gate."""

from __future__ import annotations

import json
import logging
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.core.config.roles import uses_remote_nodes
from toolkit.core.deploy.destructive_guard import write_sensitive_file
from toolkit.core.ops.database_provider import primary_database_node, primary_database_provider
from toolkit.core.ops.dump_repository import DumpRecord
from toolkit.core.state.audit_log import AuditAction, audit

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

logger = logging.getLogger(__name__)


def pre_deploy_dump(cfg: Config, root: Path, *, vm: str | None = None) -> str | None:
    """Create a provider-owned pre-deploy dump without exposing provider internals.

    Operational dump failures remain non-fatal, matching the deploy workflow's
    existing behavior. Invalid provider contracts raise before deployment so a
    configured recovery gate can never be silently bypassed.
    """
    provider = primary_database_provider(cfg)
    node = primary_database_node(cfg, provider, vm)
    try:
        return provider.plugin.pre_deploy_database_dump(cfg, root, vm=node)
    except Exception as exc:
        logger.warning("pre-deploy database dump failed for %s: %s", provider.manifest.name, exc)
        return None


def _record_restore_intent(root: Path, record: DumpRecord, actor: str, provider: str) -> Path:
    """Persist the exact immutable artifact selected before changing database state."""
    payload = {
        "actor": actor,
        "database_provider": provider,
        "dump_id": record.dump_id,
        "name": record.name,
        "path": record.path,
        "sha256": record.sha256,
        "size_bytes": record.size_bytes,
        "started_at": datetime.now(UTC).isoformat(),
    }
    intent_id = f"{int(time.time())}-{uuid.uuid4().hex[:12]}"
    path = root / ".homelab-state" / "restore-intents" / f"{intent_id}.json"
    write_sensitive_file(path, json.dumps(payload, indent=2, sort_keys=True) + "\n")
    return path


def restore_dump(
    cfg: Config,
    root: Path,
    record: DumpRecord,
    *,
    vm: str | None = None,
    actor: str = "system",
) -> bool:
    """Restore a provider-issued dump record and audit the result."""
    remote = uses_remote_nodes(cfg)
    if record.is_remote != remote:
        raise ValueError("dump location does not match the configured deployment mode")

    provider = primary_database_provider(cfg)
    node = primary_database_node(cfg, provider, vm)
    intent = _record_restore_intent(root, record, actor, provider.manifest.name)
    audit(
        root,
        AuditAction.RESTORE,
        actor=actor,
        detail=f"restore started: {record.dump_id} ({record.name})",
        vm=node,
        extra={
            "database_provider": provider.manifest.name,
            "dump_id": record.dump_id,
            "sha256": record.sha256,
            "intent": str(intent),
        },
    )
    started = time.monotonic()
    try:
        ok = provider.plugin.restore_database_dump(cfg, root, record, vm=node)
    except Exception as exc:
        logger.error("database restore failed for %s: %s", provider.manifest.name, exc)
        ok = False
    audit(
        root,
        AuditAction.RESTORE,
        actor=actor,
        ok=ok,
        detail=f"restore {'completed' if ok else 'failed'}: {record.dump_id} ({record.name})",
        vm=node,
        duration_s=time.monotonic() - started,
        extra={
            "database_provider": provider.manifest.name,
            "dump_id": record.dump_id,
            "sha256": record.sha256,
            "intent": str(intent),
        },
    )
    return ok


def list_dumps(cfg: Config, root: Path, *, vm: str | None = None) -> list[DumpRecord]:
    """List pre-deploy dumps owned by the enabled primary database provider."""
    provider = primary_database_provider(cfg)
    node = primary_database_node(cfg, provider, vm)
    try:
        return provider.plugin.list_database_dumps(cfg, root, vm=node)
    except Exception as exc:
        logger.warning("database dump discovery failed for %s: %s", provider.manifest.name, exc)
        return []
