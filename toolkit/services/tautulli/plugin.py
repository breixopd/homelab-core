"""tautulli service plugin.

The media-cache hook owns Tautulli webhook registration; this plugin provides
flat metadata and a lightweight health probe.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class TautulliPlugin(ServicePlugin):
    service = "tautulli"
    category = "media"

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        from toolkit.core.manifest.settings import service_setting_str
        from toolkit.services.sdk import VerifyCheck, docker_curl

        if service_setting_str(cfg, "media-library", "server") not in ("plex", "both"):
            return [VerifyCheck("tautulli", "health", True, "not applicable (plex disabled)")]
        rc, body = docker_curl(cfg, vm_ip, "tautulli", "http://localhost:8181/status", root=root)
        ok = rc == 0 and "tautulli" in (body or "").lower()
        return [VerifyCheck("tautulli", "health", ok, "status ok" if ok else "status endpoint unreachable")]
