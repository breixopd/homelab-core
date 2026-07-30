"""Transactional synchronization of an image-owned framework snapshot."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_FILES_MANIFEST = ".homelab-framework-files.json"
_VERSION_FILE = ".homelab-framework-version"
_LOCK_FILE = ".homelab-framework-update.lock"
_VERSION = re.compile(r"^[0-9A-Za-z][0-9A-Za-z.+-]{0,99}$")


class FrameworkSyncError(RuntimeError):
    """A managed framework snapshot could not be applied safely."""


@dataclass(frozen=True, slots=True)
class FrameworkSyncResult:
    previous_version: str
    version: str
    managed_files: int


def _safe_relative_path(raw: object) -> Path:
    if not isinstance(raw, str) or not raw or len(raw) > 4_096 or "\x00" in raw or "\\" in raw:
        raise FrameworkSyncError("unsafe framework path in managed-file manifest")
    candidate = PurePosixPath(raw)
    if candidate.is_absolute() or any(part in {"", ".", ".."} for part in candidate.parts):
        raise FrameworkSyncError("unsafe framework path in managed-file manifest")
    return Path(*candidate.parts)


def _load_manifest(root: Path, *, require_sources: bool) -> tuple[Path, ...]:
    path = root / _FILES_MANIFEST
    if path.is_symlink():
        raise FrameworkSyncError(f"framework manifest cannot be a symlink: {path}")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FrameworkSyncError(f"cannot read framework manifest at {path}") from exc
    if not isinstance(raw, list) or len(raw) > 20_000:
        raise FrameworkSyncError("framework managed-file manifest is invalid")
    entries = tuple(_safe_relative_path(item) for item in raw)
    if len(entries) != len(set(entries)):
        raise FrameworkSyncError("framework managed-file manifest contains duplicates")
    if require_sources:
        for relative in entries:
            source = root / relative
            if not source.is_file() and not source.is_symlink():
                raise FrameworkSyncError(f"framework source is missing managed file {relative.as_posix()!r}")
    return entries


def _read_version(root: Path) -> str:
    path = root / _VERSION_FILE
    if path.is_symlink():
        raise FrameworkSyncError(f"framework version marker cannot be a symlink: {path}")
    try:
        version = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError) as exc:
        raise FrameworkSyncError(f"cannot read framework version at {root}") from exc
    if not _VERSION.fullmatch(version):
        raise FrameworkSyncError("framework version marker is invalid")
    return version


def _ensure_safe_parent(root: Path, relative: Path) -> None:
    current = root
    for part in relative.parts[:-1]:
        current /= part
        if current.is_symlink():
            raise FrameworkSyncError(f"managed framework parent is a symlink: {relative.as_posix()}")


def _copy_snapshot_path(source_root: Path, relative: Path, destination: Path) -> None:
    _ensure_safe_parent(source_root, relative)
    source = source_root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        link = os.readlink(source)
        if os.path.isabs(link):
            raise FrameworkSyncError(f"managed framework symlink is absolute: {relative.as_posix()}")
        resolved = (source.parent / link).resolve()
        if not resolved.is_relative_to(source_root.resolve()):
            raise FrameworkSyncError(f"managed framework symlink escapes snapshot: {relative.as_posix()}")
        destination.symlink_to(link)
        return
    if not source.is_file():
        raise FrameworkSyncError(f"managed framework source is not a file: {relative.as_posix()}")
    shutil.copy2(source, destination, follow_symlinks=False)


def _install_staged_path(staged: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(staged, destination)


def _remove_path(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.exists():
        raise FrameworkSyncError(f"managed framework path unexpectedly became a directory: {path}")


def _prune_empty_parents(root: Path, relative: Path) -> None:
    current = (root / relative).parent
    while current != root:
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def _copy_existing(root: Path, relative: Path, backup: Path) -> bool:
    source = root / relative
    if not source.exists() and not source.is_symlink():
        return False
    _ensure_safe_parent(root, relative)
    destination = backup / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink():
        destination.symlink_to(os.readlink(source))
    elif source.is_file():
        shutil.copy2(source, destination, follow_symlinks=False)
    else:
        raise FrameworkSyncError(f"managed framework path is not a file: {relative.as_posix()}")
    return True


def _apply_snapshot(
    target: Path,
    stage: Path,
    backup: Path,
    old_paths: tuple[Path, ...],
    new_paths: tuple[Path, ...],
) -> None:
    marker_paths = (Path(_FILES_MANIFEST), Path(_VERSION_FILE))
    managed_union = tuple(sorted(set(old_paths) | set(new_paths) | set(marker_paths), key=lambda item: item.as_posix()))
    backed_up = {relative for relative in managed_union if _copy_existing(target, relative, backup)}
    try:
        for relative in (path for path in managed_union if path not in marker_paths):
            _ensure_safe_parent(target, relative)
            _remove_path(target / relative)
        for relative in new_paths:
            _install_staged_path(stage / relative, target / relative)
        for relative in marker_paths:
            _install_staged_path(stage / relative, target / relative)
        for relative in set(old_paths) - set(new_paths):
            _prune_empty_parents(target, relative)
    except Exception as exc:
        try:
            for relative in managed_union:
                _remove_path(target / relative)
            for relative in sorted(backed_up, key=lambda item: item.as_posix()):
                _install_staged_path(backup / relative, target / relative)
        except Exception as rollback_exc:
            raise FrameworkSyncError("framework update failed and rollback failed") from rollback_exc
        raise FrameworkSyncError("framework update failed and was rolled back") from exc


def sync_framework(source: Path, target: Path) -> FrameworkSyncResult:
    """Replace image-managed files while preserving every local untracked path."""
    source = source.resolve()
    if target.is_symlink():
        raise FrameworkSyncError("framework target cannot be a symlink")
    target = target.resolve()
    if source == target:
        raise FrameworkSyncError("framework source and target must differ")
    if source.is_relative_to(target) or target.is_relative_to(source):
        raise FrameworkSyncError("framework source and target must not overlap")
    if (target / ".git").exists():
        raise FrameworkSyncError("refusing to overwrite a source checkout")
    if not target.is_dir():
        raise FrameworkSyncError("framework target does not exist")

    new_paths = _load_manifest(source, require_sources=True)
    new_version = _read_version(source)

    lock_path = target / _LOCK_FILE
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(lock_path, flags, 0o600)
    except OSError as exc:
        raise FrameworkSyncError("cannot acquire framework update lock") from exc
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        if (target / ".git").exists():
            raise FrameworkSyncError("refusing to overwrite a source checkout")
        old_paths = _load_manifest(target, require_sources=False)
        old_version = _read_version(target)
        temporary = Path(tempfile.mkdtemp(prefix=".homelab-framework-update-", dir=target))
        try:
            stage = temporary / "stage"
            backup = temporary / "backup"
            stage.mkdir()
            backup.mkdir()
            for relative in new_paths:
                _copy_snapshot_path(source, relative, stage / relative)
            for marker in (_FILES_MANIFEST, _VERSION_FILE):
                shutil.copy2(source / marker, stage / marker, follow_symlinks=False)
            _apply_snapshot(target, stage, backup, old_paths, new_paths)
        finally:
            shutil.rmtree(temporary, ignore_errors=True)
    finally:
        os.close(descriptor)

    return FrameworkSyncResult(
        previous_version=old_version,
        version=new_version,
        managed_files=len(new_paths),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synchronize a managed Homelab framework snapshot")
    parser.add_argument("--source", type=Path, required=True)
    parser.add_argument("--target", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        result = sync_framework(args.source, args.target)
    except FrameworkSyncError as exc:
        parser.exit(1, f"framework sync failed: {exc}\n")
    print(f"Framework {result.previous_version} -> {result.version} ({result.managed_files} managed files reconciled)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
