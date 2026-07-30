from __future__ import annotations

import subprocess

import pytest


@pytest.mark.parametrize("executable", ["ssh", "scp", "sftp", "rsync"])
def test_remote_processes_are_blocked_until_mocked(executable: str) -> None:
    with pytest.raises(RuntimeError, match="mock the owning transport boundary"):
        subprocess.run([executable, "example.invalid"], check=False)
