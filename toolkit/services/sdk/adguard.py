"""AdGuard Home control API helpers — cfg-aware."""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services.sdk.http import basic_auth_header

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

__all__ = [
    "adguard_control_url",
    "adguard_list_rewrites",
]


def adguard_control_url(*, internal: bool = True) -> str:
    """AdGuard control API base path (inside container uses localhost:3000)."""
    host = "localhost" if internal else "adguard"
    return f"http://{host}:3000/control"


def adguard_list_rewrites(
    cfg: Config,
    vm_ip: str,
    root: Path,
    secrets: dict[str, str],
) -> tuple[list[dict] | None, str]:
    """Return the AdGuard rewrite list, or ``(None, error_detail)``.

    Multi-VM: curl the control API from inside the ``adguard`` container.
    Single-host: use the :class:`AdGuardDNS` client over Docker DNS.
    """
    from toolkit.core.ops.dns import AdGuardDNS
    from toolkit.services.sdk._vmexec import docker_curl

    password = secrets.get("ADGUARD_ADMIN_PASSWORD", "")
    if not password:
        return None, "ADGUARD_ADMIN_PASSWORD not set"

    if cfg.is_multi_node:
        auth = {"Authorization": basic_auth_header("admin", password)}
        rc, body = docker_curl(
            cfg,
            vm_ip,
            "adguard",
            f"{adguard_control_url(internal=True)}/rewrite/list",
            root=root,
            headers=auth,
        )
        if rc != 0 or not body:
            return None, "rewrite API unreachable"
        if "install" in body:
            return None, "setup wizard pending (run deploy hooks)"
        try:
            rewrites = json.loads(body)
        except json.JSONDecodeError:
            return None, "invalid rewrite JSON"
        if not isinstance(rewrites, list):
            return None, "unexpected API response"
        return rewrites, ""

    try:
        return AdGuardDNS(password=password).list_rewrites(), ""
    except OSError:
        return None, "rewrite API unreachable"
