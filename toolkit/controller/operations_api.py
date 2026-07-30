"""Controller-owned, secret-free operations and recovery inventory."""

from __future__ import annotations

import json
import os
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path

from toolkit.controller.managed_hosts_api import read_managed_hosts_view
from toolkit.controller.read_models import (
    BackupDrillView,
    BackupDumpView,
    BackupNodeView,
    BackupOperationsView,
    MaintenanceOperationsView,
    ManagedHostsView,
    OperationsView,
    UpdateCandidateView,
    UpdateOperationsView,
)
from toolkit.core.config.config import Config, load_config
from toolkit.core.config.storage import config_path
from toolkit.core.ops.backup_inventory import BackupInventory
from toolkit.core.ops.backup_restore_drill import read_backup_drill_evidence
from toolkit.core.ops.db_safety import list_dumps

_MAX_MAINTENANCE_STATE_BYTES = 64 * 1024
_INVENTORY_CACHE_TTL_SECONDS = 30.0
_dump_cache: dict[Path, tuple[float, list[BackupDumpView]]] = {}
_dump_cache_lock = threading.Lock()
_backup_cache: dict[Path, tuple[float, BackupInventory]] = {}
_backup_cache_lock = threading.Lock()
# Update-plan assembly and remote inventories are expensive; keep the snapshot
# briefly while always projecting the cheap local maintenance state live.
_OPERATIONS_CACHE_TTL_SECONDS = 30.0
_OPERATIONS_ERROR_TTL_SECONDS = 2.0
_OPERATIONS_CACHE_LIMIT = 16
_OPERATIONS_INFLIGHT_LIMIT = 8
_OPERATIONS_COLD_WAIT_SECONDS = 8.0
_OperationsKey = tuple[Path, str]
_operations_probe_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="operations-probe")
_operations_refresh_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="operations-refresh")
_operations_lock = threading.Lock()
_operations_cache: dict[_OperationsKey, tuple[float, OperationsView, bool]] = {}
_operations_inflight: dict[_OperationsKey, Future[OperationsView]] = {}


def _updates_status(root: Path, cfg: Config) -> UpdateOperationsView:
    from toolkit.core.ops.release_state import (
        ReleaseStateError,
        load_active_release,
        load_recovery_release,
        load_rollback_release,
    )
    from toolkit.core.ops.update_plan import UpdatePlanError, load_current_update_plan

    try:
        active = load_active_release(root)
        rollback = load_rollback_release(root)
        recovery = load_recovery_release(root)
        if recovery is not None:
            return UpdateOperationsView(
                available=True,
                reason="Automatic rollback needs verification before further updates can run",
                active_revision=active.revision if active else "",
                recovery_required=True,
            )
        rollback_available = bool(
            active is not None and rollback is not None and rollback.expected_active_revision == active.revision
        )
        plan = load_current_update_plan(root, cfg)
        if plan is None:
            return UpdateOperationsView(
                available=True,
                reason="Run an update check to discover compatible releases",
                active_revision=active.revision if active else "",
                rollback_available=rollback_available,
            )
        return UpdateOperationsView(
            available=True,
            reason=(
                f"{len(plan.candidates)} compatible update(s) ready for review"
                if plan.candidates
                else "No compatible updates found"
            ),
            revision=plan.revision,
            checked_at=datetime.fromisoformat(plan.checked_at),
            candidates=[
                UpdateCandidateView(
                    service=candidate.service,
                    current=candidate.current,
                    target=candidate.target,
                    changelog_url=candidate.changelog_url,
                )
                for candidate in plan.candidates
            ],
            active_revision=active.revision if active else "",
            rollback_available=rollback_available,
        )
    except (OSError, ValueError, ReleaseStateError, UpdatePlanError):
        return UpdateOperationsView(
            available=False,
            reason="Update discovery state is invalid or stale; run a fresh check",
        )


def _backup_status(root: Path, cfg: Config) -> tuple[bool | None, str, list[BackupNodeView]]:
    if not cfg.backups.enabled:
        return None, "", []
    from toolkit.core.ops.backup_inventory import read_backup_inventory

    now = time.monotonic()
    with _backup_cache_lock:
        cached = _backup_cache.get(root)
        inventory = cached[1] if cached is not None and now - cached[0] <= _INVENTORY_CACHE_TTL_SECONDS else None
    if inventory is None:
        inventory = read_backup_inventory(cfg, root)
        with _backup_cache_lock:
            _backup_cache[root] = (time.monotonic(), inventory)
            if len(_backup_cache) > 32:
                oldest = min(_backup_cache, key=lambda item: _backup_cache[item][0])
                del _backup_cache[oldest]
    nodes = [
        BackupNodeView(
            role=node.role,
            status=node.status,
            ok=node.ok,
            snapshot_count=node.snapshot_count,
            last_snapshot_at=node.last_snapshot_at,
            age_hours=node.age_hours,
            size_bytes=node.size_bytes,
        )
        for node in inventory.nodes
    ]
    return inventory.ok, inventory.error, nodes


def _maintenance_status(root: Path, daily_at: str, *, enabled: bool = True) -> MaintenanceOperationsView:
    schedule_label = f"Daily at {daily_at}"
    path = root / "data" / "maintenance" / "last-run.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        try:
            content = os.read(descriptor, _MAX_MAINTENANCE_STATE_BYTES + 1)
        finally:
            os.close(descriptor)
        if len(content) > _MAX_MAINTENANCE_STATE_BYTES:
            raise ValueError("maintenance state exceeds its size limit")
        raw = json.loads(content)
        if not isinstance(raw, dict):
            raise ValueError("maintenance state is invalid")
        timestamp = float(raw.get("timestamp", 0))
        actions = raw.get("actions")
        errors = raw.get("errors")
        if not isinstance(actions, list) or not isinstance(errors, list):
            raise ValueError("maintenance state is invalid")
        return MaintenanceOperationsView(
            enabled=enabled,
            daily_at=daily_at,
            schedule_label=schedule_label,
            last_run_at=datetime.fromtimestamp(timestamp, UTC) if timestamp > 0 else None,
            ok=raw.get("ok") if isinstance(raw.get("ok"), bool) else None,
            action_count=min(len(actions), 10_000),
            error_count=min(len(errors), 10_000),
        )
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError, OverflowError):
        return MaintenanceOperationsView(enabled=enabled, daily_at=daily_at, schedule_label=schedule_label)


def _dumps(root: Path, cfg) -> list[BackupDumpView]:
    now = time.monotonic()
    with _dump_cache_lock:
        cached = _dump_cache.get(root)
        if cached is not None and now - cached[0] <= _INVENTORY_CACHE_TTL_SECONDS:
            return list(cached[1])
    views = [
        BackupDumpView(
            dump_id=record.dump_id,
            name=record.name,
            size_bytes=record.size_bytes,
            size=record.size,
        )
        for record in list_dumps(cfg, root)[:7]
    ]
    with _dump_cache_lock:
        _dump_cache[root] = (time.monotonic(), list(views))
        if len(_dump_cache) > 32:
            oldest = min(_dump_cache, key=lambda item: _dump_cache[item][0])
            del _dump_cache[oldest]
    return views


def _operations_key(root: Path, cfg: Config) -> _OperationsKey:
    import hashlib

    revision = hashlib.sha256(cfg.model_dump_json().encode()).hexdigest()
    return root.resolve(), revision


def _build_operations_view(root: Path, cfg: Config) -> OperationsView:
    # These independent reads can involve SSH/filesystem work.  Use one
    # bounded, shared executor so the cold request does not pay each latency
    # serially and requests cannot create unbounded worker threads.
    backup_future = _operations_probe_executor.submit(_backup_status, root, cfg)
    hosts_future = _operations_probe_executor.submit(read_managed_hosts_view, root)
    dumps_future = _operations_probe_executor.submit(_dumps, root, cfg)
    updates_future = _operations_probe_executor.submit(_updates_status, root, cfg)
    drill_evidence = read_backup_drill_evidence(root) if cfg.backups.enabled else None
    backup_ok, backup_error, backup_nodes = backup_future.result()
    hosts = hosts_future.result()
    dumps = dumps_future.result()
    updates = updates_future.result()
    return OperationsView(
        maintenance=_maintenance_status(root, cfg.maintenance.daily_at, enabled=cfg.maintenance.enabled),
        backups=BackupOperationsView(
            enabled=cfg.backups.enabled,
            target=cfg.backups.target,
            storage_host=cfg.backups.storage_host,
            ok=backup_ok,
            error=backup_error,
            nodes=backup_nodes,
            drill=BackupDrillView(
                last_run_at=drill_evidence.checked_at if drill_evidence else None,
                ok=drill_evidence.ok if drill_evidence else None,
                node_count=drill_evidence.node_count if drill_evidence else 0,
                artifact_count=drill_evidence.artifact_count if drill_evidence else 0,
                error_count=drill_evidence.error_count if drill_evidence else 0,
            ),
        ),
        dumps=dumps,
        hosts=hosts,
        updates=updates,
    )


def _pending_operations_view(root: Path, cfg: Config) -> OperationsView:
    return OperationsView(
        maintenance=_maintenance_status(root, cfg.maintenance.daily_at, enabled=cfg.maintenance.enabled),
        backups=BackupOperationsView(
            enabled=cfg.backups.enabled,
            target=cfg.backups.target,
            storage_host=cfg.backups.storage_host,
            error="Refreshing backup inventory",
        ),
        dumps=[],
        hosts=ManagedHostsView(revision=_operations_key(root, cfg)[1], hosts=[], service_choices=[]),
        updates=UpdateOperationsView(available=False, reason="Refreshing operational inventory"),
    )


def _failed_operations_view(root: Path, cfg: Config) -> OperationsView:
    return OperationsView(
        maintenance=_maintenance_status(root, cfg.maintenance.daily_at, enabled=cfg.maintenance.enabled),
        backups=BackupOperationsView(
            enabled=cfg.backups.enabled,
            target=cfg.backups.target,
            storage_host=cfg.backups.storage_host,
            ok=False if cfg.backups.enabled else None,
            error="Operational inventory refresh failed",
        ),
        dumps=[],
        hosts=ManagedHostsView(revision=_operations_key(root, cfg)[1], hosts=[], service_choices=[]),
        updates=UpdateOperationsView(
            available=False,
            reason="Operational inventory refresh failed; retrying shortly",
        ),
    )


def _complete_operations(
    key: _OperationsKey,
    root: Path,
    cfg: Config,
    future: Future[OperationsView],
) -> None:
    failed = False
    try:
        result = future.result()
    except Exception:
        failed = True
        result = _failed_operations_view(root, cfg)
    with _operations_lock:
        if _operations_inflight.get(key) is not future:
            return
        _operations_inflight.pop(key, None)
        _operations_cache[key] = (time.monotonic(), result, failed)
        while len(_operations_cache) > _OPERATIONS_CACHE_LIMIT:
            oldest = min(_operations_cache, key=lambda item: _operations_cache[item][0])
            del _operations_cache[oldest]


def _with_current_maintenance(view: OperationsView, root: Path, cfg: Config) -> OperationsView:
    return view.model_copy(
        deep=True,
        update={
            "maintenance": _maintenance_status(
                root,
                cfg.maintenance.daily_at,
                enabled=cfg.maintenance.enabled,
            )
        },
    )


def read_operations_view(root: Path) -> OperationsView:
    root = root.resolve()
    cfg = load_config(config_path(root))
    key = _operations_key(root, cfg)
    now = time.monotonic()
    new_future: Future[OperationsView] | None = None
    with _operations_lock:
        cached = _operations_cache.get(key)
        future = _operations_inflight.get(key)
        cache_ttl = _OPERATIONS_ERROR_TTL_SECONDS if cached and cached[2] else _OPERATIONS_CACHE_TTL_SECONDS
        if cached and now - cached[0] <= cache_ttl and future is None:
            return _with_current_maintenance(cached[1], root, cfg)
        if future is None and len(_operations_inflight) < _OPERATIONS_INFLIGHT_LIMIT:
            new_future = _operations_refresh_executor.submit(_build_operations_view, root, cfg)
            _operations_inflight[key] = new_future
            future = new_future
    if new_future is not None:
        new_future.add_done_callback(lambda completed: _complete_operations(key, root, cfg, completed))
    if cached:
        return _with_current_maintenance(cached[1], root, cfg)
    if future is not None:
        try:
            return _with_current_maintenance(
                future.result(timeout=_OPERATIONS_COLD_WAIT_SECONDS),
                root,
                cfg,
            )
        except Exception:
            if future.done():
                return _failed_operations_view(root, cfg)
    return _pending_operations_view(root, cfg)
