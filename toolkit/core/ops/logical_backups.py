"""Manifest-owned application-consistent exports captured by node snapshots."""

from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from toolkit.core.config.config import Config
from toolkit.core.config.storage import env_path
from toolkit.core.manifest.schema import NodeId
from toolkit.core.manifest.storage import read_role_environment


@dataclass(frozen=True, slots=True)
class LogicalDumpResult:
    ok: bool
    artifacts: tuple[Path, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _DumpSpec:
    service: str
    artifact: str
    strategy: Literal["container", "sqlite"]
    timeout_seconds: int
    container: str = ""
    command: tuple[str, ...] = ()
    source_env: str = ""
    source_subpath: str = ""
    database_path: str = ""


def _dump_specs(cfg: Config, root: Path | None, role: NodeId) -> tuple[_DumpSpec, ...]:
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_node, manifest_runtime_nodes, manifest_storage_nodes
    from toolkit.core.manifest.routes import service_is_enabled

    catalog_root = (
        root
        if root is not None
        and any(
            any(root.glob(pattern))
            for pattern in ("toolkit/services/*/service.yaml", "services/*/service.yaml", "*/service.yaml")
        )
        else None
    )
    specs: list[_DumpSpec] = []
    for manifest in load_service_catalog(catalog_root).manifests:
        if not service_is_enabled(cfg, manifest):
            continue
        assets = {asset.name: asset for asset in manifest.data_specs}
        for export in manifest.backup_exports:
            if export.strategy == "sqlite":
                asset = assets[export.data_spec]
                nodes = manifest_storage_nodes(cfg, manifest, asset.runtime_service)
                if role not in nodes:
                    continue
                specs.append(
                    _DumpSpec(
                        service=manifest.name,
                        artifact=export.artifact,
                        strategy="sqlite",
                        timeout_seconds=export.timeout_seconds,
                        source_env=asset.source_env or "",
                        source_subpath=asset.source_subpath,
                        database_path=export.database_path,
                    )
                )
                continue
            nodes = (
                manifest_runtime_nodes(cfg, manifest, export.runtime_service)
                if export.runtime_service
                else (manifest_node(cfg, manifest),)
            )
            if role not in nodes:
                continue
            specs.append(
                _DumpSpec(
                    service=manifest.name,
                    artifact=export.artifact,
                    strategy="container",
                    timeout_seconds=export.timeout_seconds,
                    container=export.container or export.runtime_service or manifest.name,
                    command=export.command,
                )
            )
    return tuple(specs)


def logical_dump_names(cfg: Config, role: NodeId, root: Path | None = None) -> tuple[str, ...]:
    """Return the exact manifest-owned artifacts a role snapshot must contain."""
    return tuple(spec.artifact for spec in _dump_specs(cfg, root, role))


def _dump_output_dir(root: Path, role: NodeId) -> Path:
    environment = read_role_environment(env_path(role, root))
    configured = environment.get("KOPIA_DUMPS_SOURCE", "").strip()
    dumps_root = Path(configured) if configured else root / "data" / "kopia" / "dumps"
    if not dumps_root.is_absolute():
        raise ValueError("KOPIA_DUMPS_SOURCE must resolve to an absolute path")
    return dumps_root / role


def _container_export(spec: _DumpSpec, pending: Path) -> str:
    with pending.open("wb") as output:
        compressor = subprocess.Popen(
            ["gzip", "-6", "-c"],
            stdin=subprocess.PIPE,
            stdout=output,
            stderr=subprocess.PIPE,
        )
        try:
            if compressor.stdin is None or compressor.stderr is None:
                raise OSError("gzip pipeline did not expose required streams")
            result = subprocess.run(
                ["docker", "exec", spec.container, *spec.command],
                stdout=compressor.stdin,
                stderr=subprocess.PIPE,
                timeout=spec.timeout_seconds,
                check=False,
            )
            compressor.stdin.close()
            compressor_error = compressor.stderr.read()
            compressor_code = compressor.wait(timeout=min(spec.timeout_seconds, 120))
        except BaseException:
            if compressor.stdin is not None and not compressor.stdin.closed:
                compressor.stdin.close()
            compressor.kill()
            compressor.wait()
            raise
    if result.returncode != 0:
        return result.stderr.decode(errors="replace").strip() or f"container export exited {result.returncode}"
    if compressor_code != 0:
        return compressor_error.decode(errors="replace").strip() or f"gzip exited {compressor_code}"
    with gzip.open(pending, "rb") as stream:
        if not stream.read(1):
            return "empty database export"
    return ""


def _sqlite_export(spec: _DumpSpec, root: Path, role: NodeId, pending: Path) -> str:
    environment = read_role_environment(env_path(role, root))
    source_root = environment.get(spec.source_env, "").strip()
    if not source_root:
        return f"generated/{role}/.env has no {spec.source_env}"
    storage_root = Path(source_root)
    if not storage_root.is_absolute():
        return f"{spec.source_env} must resolve to an absolute path"
    if spec.source_subpath:
        storage_root /= spec.source_subpath
    try:
        storage_root = storage_root.resolve(strict=True)
        source = (storage_root / spec.database_path).resolve(strict=True)
    except OSError as exc:
        return f"database is unavailable: {exc}"
    if not source.is_relative_to(storage_root):
        return "database resolves outside its declared storage asset"
    if not source.is_file():
        return "database source is not a regular file"

    raw = pending.with_suffix(".sqlite.tmp")
    started = time.monotonic()

    def enforce_deadline(_status: int, _remaining: int, _total: int) -> None:
        if time.monotonic() - started > spec.timeout_seconds:
            raise TimeoutError(f"SQLite backup exceeded {spec.timeout_seconds}s")

    try:
        with (
            sqlite3.connect(f"{source.as_uri()}?mode=ro", uri=True, timeout=30) as source_db,
            sqlite3.connect(raw) as backup_db,
        ):
            source_db.backup(backup_db, pages=256, progress=enforce_deadline, sleep=0.1)
            result = backup_db.execute("PRAGMA quick_check").fetchone()
            if result != ("ok",):
                return "SQLite backup integrity check failed"
        with raw.open("rb") as source_stream, gzip.open(pending, "wb", compresslevel=6) as output:
            shutil.copyfileobj(source_stream, output, length=1024 * 1024)
        return ""
    finally:
        raw.unlink(missing_ok=True)


def prepare_logical_dumps(cfg: Config, root: Path, role: NodeId) -> LogicalDumpResult:
    """Atomically refresh every manifest-owned database export on a node."""
    specs = _dump_specs(cfg, root, role)
    if not specs:
        return LogicalDumpResult(True)
    try:
        output_dir = _dump_output_dir(root, role)
    except ValueError as exc:
        return LogicalDumpResult(False, errors=(str(exc),))
    output_dir.mkdir(parents=True, exist_ok=True)
    temporary: list[tuple[Path, Path]] = []
    errors: list[str] = []
    try:
        for spec in specs:
            destination = output_dir / spec.artifact
            pending = output_dir / f".{spec.artifact}.{os.getpid()}.tmp"
            detail = (
                _sqlite_export(spec, root, role, pending)
                if spec.strategy == "sqlite"
                else _container_export(spec, pending)
            )
            if detail or not pending.is_file() or pending.stat().st_size == 0:
                errors.append(f"{spec.service}: {detail or 'empty database export'}"[:240])
                break
            pending.chmod(0o600)
            temporary.append((pending, destination))
        if errors:
            return LogicalDumpResult(False, errors=tuple(errors))
        for pending, destination in temporary:
            pending.replace(destination)
            destination.chmod(0o600)
        return LogicalDumpResult(True, tuple(destination for _pending, destination in temporary))
    except (OSError, sqlite3.Error, subprocess.SubprocessError, TimeoutError) as exc:
        return LogicalDumpResult(False, errors=(str(exc)[:240],))
    finally:
        for pending in output_dir.glob(f".*.{os.getpid()}.*tmp"):
            pending.unlink(missing_ok=True)
