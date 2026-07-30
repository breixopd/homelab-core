"""CrowdSec LAPI / cscli helpers — cfg-aware."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

__all__ = [
    "crowdsec_lapi_url",
    "crowdsec_cscli",
    "crowdsec_health_url",
]

CROWDSEC_LAPI_PORT = 8080


def crowdsec_lapi_url(*, internal: bool = True) -> str:
    """CrowdSec local API base URL."""
    host = "localhost" if internal else "crowdsec"
    return f"http://{host}:{CROWDSEC_LAPI_PORT}"


def crowdsec_health_url(*, internal: bool = True) -> str:
    """CrowdSec health probe URL."""
    return f"{crowdsec_lapi_url(internal=internal)}/health"


def crowdsec_cscli(
    cfg: Config,
    vm_ip: str,
    root: Path,
    args: list[str],
    *,
    timeout: int = 15,
) -> tuple[int, str]:
    """Run ``cscli`` inside the crowdsec container on the target VM."""
    from toolkit.services.sdk._vmexec import docker_exec_on_vm

    return docker_exec_on_vm(cfg, "crowdsec", ["cscli", *args], vm_ip, root, timeout=timeout)
