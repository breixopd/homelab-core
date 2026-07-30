"""Ephemeral previous-secret context for transactional service reconciliation."""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path

import yaml

_CONTEXT_ENV = "HOMELAB_ROTATION_PREVIOUS_FILE"


def _context_directory(root: Path) -> Path:
    return root.resolve() / ".homelab-state" / "ansible"


@contextmanager
def previous_secret_context(root: Path, values: Mapping[str, str | None]) -> Iterator[None]:
    """Expose previous values to one in-process deployment and remove them afterward."""
    retained = {name: value for name, value in values.items() if isinstance(value, str) and value}
    if not retained:
        yield
        return

    directory = _context_directory(root)
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    fd, raw_path = tempfile.mkstemp(prefix="rotation-previous-", suffix=".yaml", dir=directory)
    path = Path(raw_path)
    previous_env = os.environ.get(_CONTEXT_ENV)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            yaml.safe_dump(retained, handle, sort_keys=True)
        path.chmod(0o600)
        os.environ[_CONTEXT_ENV] = str(path)
        yield
    finally:
        if previous_env is None:
            os.environ.pop(_CONTEXT_ENV, None)
        else:
            os.environ[_CONTEXT_ENV] = previous_env
        path.unlink(missing_ok=True)


def load_previous_secret_values(root: Path) -> dict[str, str]:
    """Load a validated root-owned rotation context, when one is active."""
    raw_path = os.environ.get(_CONTEXT_ENV, "").strip()
    if not raw_path:
        return {}
    path = Path(raw_path).resolve()
    directory = _context_directory(root)
    if path.parent != directory:
        raise ValueError("rotation previous-secret context is outside the protected state directory")
    try:
        metadata = path.stat(follow_symlinks=False)
    except OSError as exc:
        raise ValueError("rotation previous-secret context is unavailable") from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.geteuid() or metadata.st_mode & 0o077:
        raise ValueError("rotation previous-secret context has unsafe ownership or permissions")
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict) or any(
        not isinstance(name, str) or not isinstance(value, str) for name, value in document.items()
    ):
        raise ValueError("rotation previous-secret context must contain string values")
    return document
