"""Crash-safe persistence helpers for bounded controller state."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, content: str, *, mode: int = 0o600) -> None:
    """Durably replace a text file without exposing a partial write."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_write_json(path: Path, payload: dict[str, Any], *, mode: int = 0o600) -> None:
    """Durably replace a JSON document without exposing a partial write."""
    import json

    atomic_write_text(path, json.dumps(payload, indent=2) + "\n", mode=mode)
