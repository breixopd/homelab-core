"""Safe probes for service-owned agents running on managed hosts."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import ExternalHost


def systemd_unit_active(root: Path, host: ExternalHost, unit: str) -> bool | None:
    """Return a managed host systemd unit state without treating probe errors as false."""
    from toolkit.core.infra.hosts import host_ssh_args

    args = host_ssh_args(root, host)
    if args is None:
        return None
    try:
        proc = subprocess.run(
            [*args, f"systemctl is-active --quiet {unit} && echo active || echo inactive"],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if proc.returncode != 0:
        return None
    return "active" in (proc.stdout or "")
