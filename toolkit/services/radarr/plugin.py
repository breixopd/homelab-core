"""radarr service plugin.

Owns its verify() on top of the base ServicePlugin defaults read from its
manifest and Compose application. Cross-service wiring is owned by the
embedded Servarr integration plugin.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class RadarrPlugin(ServicePlugin):
    service = "radarr"
    category = "media"

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Verify Radarr health, root folders, Prowlarr indexers, and qBittorrent round-trip."""
        from toolkit.services._arr import verify_arr_standard
        from toolkit.services.sdk import VerifyCheck

        api_key = secrets.get("RADARR_API_KEY", "")
        if not api_key:
            return [VerifyCheck("radarr", "api", False, "RADARR_API_KEY not set")]
        return verify_arr_standard("radarr", cfg, "http://radarr:7878", "radarr", 7878, vm_ip, root, api_key)
