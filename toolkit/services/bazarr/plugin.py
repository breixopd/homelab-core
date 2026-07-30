"""bazarr service plugin.

Owns its verify() on top of the base ServicePlugin defaults
(compose_service, env_vars, secrets_needed, credentials) read from service.yaml.
Subtitle and provider wiring is owned by the embedded Servarr integration plugin.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class BazarrPlugin(ServicePlugin):
    service = "bazarr"
    category = "media"

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Bazarr health, languages, providers, and Sonarr/Radarr links."""
        from toolkit.services._arr import verify_bazarr_standard

        return verify_bazarr_standard(cfg, secrets, vm_ip, root)
