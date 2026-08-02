"""Process execution primitives with deadline-safe descendant cleanup."""

from __future__ import annotations

import os
import signal
import subprocess
from collections.abc import Sequence

_TERMINATION_GRACE_SECONDS = 0.5


def _signal_process_group(pid: int, sig: signal.Signals) -> bool:
    """Signal a process group, returning false when it has already exited."""
    try:
        os.killpg(pid, sig)
    except ProcessLookupError:
        return False
    return True


def run_text_process_group(
    args: Sequence[str],
    *,
    input_text: str | None = None,
    timeout: float,
) -> subprocess.CompletedProcess[str]:
    """Run a text process and kill its complete local process group on timeout."""
    process = subprocess.Popen(
        list(args),
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    try:
        stdout, stderr = process.communicate(input=input_text, timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        _signal_process_group(process.pid, signal.SIGTERM)
        try:
            stdout, stderr = process.communicate(timeout=_TERMINATION_GRACE_SECONDS)
            # The group leader can exit before a descendant finishes handling
            # SIGTERM. Close that race so timed-out helpers cannot outlive the
            # bounded operation as runnable processes.
            _signal_process_group(process.pid, signal.SIGKILL)
        except subprocess.TimeoutExpired:
            _signal_process_group(process.pid, signal.SIGKILL)
            stdout, stderr = process.communicate()
        raise subprocess.TimeoutExpired(args, timeout, output=stdout, stderr=stderr) from exc
    return subprocess.CompletedProcess(list(args), process.returncode, stdout, stderr)
