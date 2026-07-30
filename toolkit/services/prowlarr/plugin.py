"""prowlarr service plugin."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck

__all__ = ["ProwlarrPlugin"]


class ProwlarrPlugin(ServicePlugin):
    service = "prowlarr"
    category = "media"

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Add public indexers from the schema API and sync them to Sonarr/Radarr."""
        from toolkit.core.ops.automation import resolve_docker_service_url
        from toolkit.services._arr import (
            configure_prowlarr_indexers,
            reconcile_prowlarr_application_urls,
            reconcile_servarr_api_key,
            trigger_prowlarr_indexer_sync,
        )

        if root is None:
            return []
        logs: list[str] = []
        api_key = reconcile_servarr_api_key(root, "prowlarr", secrets, "PROWLARR_API_KEY")
        if not api_key:
            logs.append("Prowlarr: API key missing — skip indexer bootstrap")
            return logs
        prowlarr_url = resolve_docker_service_url("prowlarr", 9696)
        flaresolverr_url = resolve_docker_service_url("flaresolverr", 8191)
        wanted_indexers = tuple(
            dict.fromkeys(
                item.strip().lower()
                for item in str(self.setting(cfg, "indexers")).split(",")
                if item.strip()
            )
        )
        logs.extend(reconcile_prowlarr_application_urls(prowlarr_url, api_key))
        logs.extend(
            configure_prowlarr_indexers(
                prowlarr_url=prowlarr_url,
                api_key=api_key,
                flaresolverr_url=flaresolverr_url,
                wanted_indexers=wanted_indexers,
            )
        )
        if trigger_prowlarr_indexer_sync(prowlarr_url, api_key):
            logs.append("Prowlarr: application indexer sync triggered")
        return logs

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Verify Prowlarr health, indexers, applications, and CF-indexer FlareSolverr wiring."""
        from toolkit.services._arr import verify_prowlarr_standard
        from toolkit.services.sdk import VerifyCheck

        api_key = secrets.get("PROWLARR_API_KEY", "")
        if not api_key:
            return [VerifyCheck("prowlarr", "api", False, "PROWLARR_API_KEY not set")]
        return verify_prowlarr_standard(cfg, "http://prowlarr:9696", "prowlarr", vm_ip, root, api_key)
