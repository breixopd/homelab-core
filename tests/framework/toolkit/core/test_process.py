from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import pytest
from toolkit.core.process import run_text_process_group


def test_run_text_process_group_preserves_text_io() -> None:
    result = run_text_process_group(
        [sys.executable, "-c", "import sys; print(sys.stdin.read())"],
        input_text="secret-over-stdin",
        timeout=2,
    )

    assert result.returncode == 0
    assert result.stdout.strip() == "secret-over-stdin"
    assert result.stderr == ""


def test_run_text_process_group_kills_descendants_on_timeout(tmp_path: Path) -> None:
    pid_file = tmp_path / "descendant.pid"
    child = "import os, pathlib, sys, time; pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); time.sleep(30)"
    parent = (
        "import subprocess, sys, time; "
        f"subprocess.Popen([sys.executable, '-c', {child!r}, sys.argv[1]]); "
        "time.sleep(30)"
    )

    with pytest.raises(subprocess.TimeoutExpired):
        run_text_process_group([sys.executable, "-c", parent, str(pid_file)], timeout=0.2)

    descendant_pid = int(pid_file.read_text())
    time.sleep(0.05)
    try:
        os.kill(descendant_pid, 0)
    except ProcessLookupError:
        return
    # A killed grandchild can remain briefly as an init-owned zombie; it must
    # not remain runnable after the process-group kill.
    stat_path = Path(f"/proc/{descendant_pid}/stat")
    try:
        state = stat_path.read_text().split()[2]
    except (FileNotFoundError, ProcessLookupError):
        return
    assert state == "Z"
