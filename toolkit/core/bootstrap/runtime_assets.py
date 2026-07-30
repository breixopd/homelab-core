"""Seed and update packaged runtime inputs required by a clean toolkit root."""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.resources
import json
import os
import tempfile
from pathlib import Path

_MANIFEST = ".homelab-runtime-assets.json"
_SKIP_NAMES = {"__pycache__"}


class RuntimeAssetsError(RuntimeError):
    """Packaged runtime assets could not be materialized safely."""


def _package_version() -> str:
    try:
        return importlib.metadata.version("homelab-toolkit")
    except importlib.metadata.PackageNotFoundError:
        return "source"


def _collect_files(source, relative: Path = Path()) -> dict[Path, bytes]:
    files: dict[Path, bytes] = {}
    for entry in source.iterdir():
        if entry.name in _SKIP_NAMES:
            continue
        child = relative / entry.name
        if entry.is_dir():
            files.update(_collect_files(entry, child))
        elif entry.name.endswith(".pyc"):
            continue
        else:
            files[child] = entry.read_bytes()
    return files


def _content_hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_manifest(path: Path) -> tuple[str | None, dict[Path, str]]:
    if path.is_symlink():
        raise RuntimeAssetsError(f"runtime asset manifest cannot be a symlink: {path}")
    if not path.is_file():
        return None, {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        version = data.get("version")
        raw_files = data.get("files", {})
        if not isinstance(raw_files, dict) or not all(
            isinstance(item, str) and isinstance(digest, str) for item, digest in raw_files.items()
        ):
            raise ValueError
        files = {Path(item): digest for item, digest in raw_files.items()}
    except (OSError, TypeError, ValueError, AttributeError):
        raise RuntimeAssetsError(f"runtime asset manifest is invalid: {path}")
    if not isinstance(version, str) or any(
        path.is_absolute()
        or not path.parts
        or any(part in {"", ".", ".."} for part in path.parts)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        for path, digest in files.items()
    ):
        raise RuntimeAssetsError(f"runtime asset manifest contains unsafe paths: {path}")
    return version, files


def _atomic_write(path: Path, content: bytes, *, executable: bool = False) -> None:
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(0o755 if executable else 0o644)
        os.replace(temporary, path)
    except OSError as exc:
        temporary.unlink(missing_ok=True)
        raise RuntimeAssetsError(f"cannot write runtime asset {path}") from exc


def _write_manifest(path: Path, version: str, files: dict[Path, str]) -> None:
    payload = (
        json.dumps(
            {
                "version": version,
                "files": {item.as_posix(): files[item] for item in sorted(files, key=Path.as_posix)},
            },
            indent=2,
        )
        + "\n"
    ).encode()
    _atomic_write(path, payload)


def _ensure_safe_destination(root: Path, relative: Path) -> Path:
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink():
            raise RuntimeAssetsError(f"runtime asset path cannot be a symlink: {current}")
    return current


def ensure_runtime_assets(root: Path) -> list[Path]:
    """Install/update packaged assets without overwriting source checkouts.

    Managed files are refreshed when the installed package version changes;
    untracked local files are never overwritten. Same-version runs only fill
    missing files, making repeated CLI invocations idempotent.
    """
    root = root.expanduser().resolve()
    if (root / ".git").exists() and (root / "pyproject.toml").is_file():
        return []
    root.mkdir(parents=True, exist_ok=True)

    package_root = importlib.resources.files("toolkit")
    package_files: dict[Path, bytes] = {}
    for relative, content in _collect_files(package_root / "services").items():
        package_files[Path("toolkit") / "services" / relative] = content
    package_files[Path("toolkit/Dockerfile")] = (package_root / "Dockerfile").read_bytes()
    package_files[Path("stacks/platform.yaml")] = (
        package_root / "core" / "bootstrap" / "assets" / "platform.yaml"
    ).read_bytes()

    manifest_path = root / _MANIFEST
    previous_version, managed = _read_manifest(manifest_path)
    version = _package_version()
    refresh_managed = previous_version is not None and previous_version != version
    copied: list[Path] = []
    managed_now = dict(managed)
    for relative, content in package_files.items():
        destination = _ensure_safe_destination(root, relative)
        previous_hash = managed.get(relative)
        if destination.exists():
            if not destination.is_file():
                raise RuntimeAssetsError(f"runtime asset path is not a file: {destination}")
            current_hash = _content_hash(destination.read_bytes())
            if not refresh_managed or previous_hash is None or current_hash != previous_hash:
                # Existing unmanaged and locally customized files belong to the
                # operator. Keep the last installed digest so a later upgrade
                # will continue to recognize the customization.
                continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(destination, content, executable=content.startswith(b"#!"))
        copied.append(destination)
        managed_now[relative] = _content_hash(content)

    for relative in managed.keys() - package_files.keys():
        destination = _ensure_safe_destination(root, relative)
        if not destination.exists():
            managed_now.pop(relative, None)
            continue
        if not destination.is_file():
            raise RuntimeAssetsError(f"runtime asset path is not a file: {destination}")
        if _content_hash(destination.read_bytes()) == managed[relative]:
            destination.unlink()
            managed_now.pop(relative, None)

    if previous_version != version or managed_now != managed or not manifest_path.exists():
        _write_manifest(manifest_path, version, managed_now)
    return copied
