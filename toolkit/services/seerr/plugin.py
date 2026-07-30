"""seerr service plugin — owns verify() on top of service.yaml defaults."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class SeerrPlugin(ServicePlugin):
    service = "seerr"
    category = "media"

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Seerr status endpoint and Jellyfin/Sonarr/Radarr connection settings."""
        from toolkit.services._arr import verify_seerr_standard

        return verify_seerr_standard(cfg, secrets, vm_ip, root)
