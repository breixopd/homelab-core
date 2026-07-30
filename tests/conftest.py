"""Shared pytest configuration."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_REMOTE_EXECUTABLES = frozenset({"scp", "sftp", "ssh", "rsync"})


def _executable_name(args: object) -> str:
    if isinstance(args, str):
        return Path(args.split(maxsplit=1)[0]).name if args.strip() else ""
    if isinstance(args, os.PathLike):
        return Path(args).name
    if isinstance(args, list | tuple) and args:
        return Path(os.fspath(args[0])).name
    return ""


@pytest.fixture(autouse=True)
def _block_remote_subprocesses(monkeypatch: pytest.MonkeyPatch):
    """Make the automated test suite incapable of contacting managed hosts."""
    original_run = subprocess.run
    original_popen = subprocess.Popen

    def reject(args: object) -> None:
        executable = _executable_name(args)
        if executable in _REMOTE_EXECUTABLES:
            raise RuntimeError(
                f"unit test attempted remote executable {executable!r}; mock the owning transport boundary"
            )

    def guarded_run(*popenargs, **kwargs):
        args = popenargs[0] if popenargs else kwargs.get("args")
        reject(args)
        return original_run(*popenargs, **kwargs)

    def guarded_popen(*popenargs, **kwargs):
        args = popenargs[0] if popenargs else kwargs.get("args")
        reject(args)
        return original_popen(*popenargs, **kwargs)

    monkeypatch.setattr(subprocess, "run", guarded_run)
    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    yield
