"""tautulli service plugin.

The media-cache hook owns Tautulli webhook registration; this plugin provides
flat metadata and a lightweight health probe.
"""

from __future__ import annotations

import json
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
        from toolkit.services.sdk import VerifyCheck, VerifyStatus, docker_curl

        if service_setting_str(cfg, "media-library", "server") not in ("plex", "both"):
            return [VerifyCheck("tautulli", "health", True, "not applicable (plex disabled)")]
        api_key = (secrets.get("TAUTULLI_API_KEY") or "").strip()
        if not api_key:
            return [
                VerifyCheck(
                    "tautulli",
                    "server_info",
                    False,
                    "TAUTULLI_API_KEY missing; Plex server cannot be verified",
                    status=VerifyStatus.NOT_READY,
                )
            ]
        rc, body = docker_curl(
            cfg,
            vm_ip,
            "tautulli",
            f"http://localhost:8181/api/v2?apikey={api_key}&cmd=get_server_info",
            root=root,
        )
        if rc != 0 or not body:
            return [VerifyCheck("tautulli", "server_info", False, "server info API unreachable")]
        try:
            payload = json.loads(body)
            response = payload.get("response", {}) if isinstance(payload, dict) else {}
            data = response.get("data") if isinstance(response, dict) else None
            configured = (
                isinstance(response, dict)
                and response.get("result") == "success"
                and isinstance(data, dict)
                and bool(data.get("pms_identifier") or data.get("pms_name"))
            )
        except (json.JSONDecodeError, TypeError, AttributeError):
            configured = False
        return [
            VerifyCheck(
                "tautulli",
                "server_info",
                configured,
                "Plex server configured" if configured else "Tautulli returned no configured Plex server",
            )
        ]
