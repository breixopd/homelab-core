"""plex service plugin.

Owns its verify() (library sections probe) on top of the base ServicePlugin
defaults read from its manifest and Compose application.
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.services.sdk import VerifyCheck


class PlexPlugin(ServicePlugin):
    service = "plex"
    category = "media"

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Verify Plex identity and library sections using the stored ``PLEX_TOKEN``."""
        from toolkit.core.manifest.settings import service_setting_str
        from toolkit.services.sdk import VerifyCheck, docker_curl

        if service_setting_str(cfg, "media-library", "server") not in ("plex", "both"):
            return [VerifyCheck("plex", "libraries", True, "not applicable (jellyfin only)")]
        token = secrets.get("PLEX_TOKEN", "")
        if not token:
            return [VerifyCheck("plex", "identity", True, "skipped (no PLEX_TOKEN in secrets)")]

        checks: list[VerifyCheck] = []
        identity_path = f"/identity?X-Plex-Token={token}"
        if cfg.is_multi_node:
            rc, body = docker_curl(cfg, vm_ip, "plex", f"http://localhost:32400{identity_path}", root=root)
            identity_ok = rc == 0 and bool((body or "").strip())
            identity_text = body or ""
        else:
            import httpx

            try:
                resp = httpx.get(f"http://localhost:32400{identity_path}", timeout=10)
                identity_ok = resp.status_code == 200
                identity_text = resp.text if identity_ok else ""
            except httpx.HTTPError:
                identity_ok = False
                identity_text = ""
        machine = ""
        if identity_ok and "machineIdentifier=" in identity_text:
            import re

            match = re.search(r'machineIdentifier="([^"]+)"', identity_text)
            machine = match.group(1)[:12] if match else ""
        checks.append(
            VerifyCheck(
                "plex",
                "identity",
                identity_ok,
                f"server reachable ({machine}…)"
                if machine
                else ("reachable" if identity_ok else "identity unreachable"),
            )
        )

        path = f"/library/sections?X-Plex-Token={token}"
        if cfg.is_multi_node:
            rc, body = docker_curl(cfg, vm_ip, "plex", f"http://localhost:32400{path}", root=root)
            if rc != 0 or not (body or "").strip():
                return [VerifyCheck("plex", "libraries", False, "library API unreachable")]
            text = body
        else:
            import httpx

            try:
                resp = httpx.get(f"http://localhost:32400{path}", timeout=10)
                if resp.status_code != 200:
                    return [VerifyCheck("plex", "libraries", False, f"HTTP {resp.status_code}")]
                text = resp.text
            except httpx.HTTPError:
                return [VerifyCheck("plex", "libraries", False, "API unreachable")]

        # Plex returns XML; count <Directory> entries under <MediaContainer>.
        count = len(re.findall(r"<Directory\b", text))
        has_sections = count > 0
        detail = f"{count} section(s)" if has_sections else "no library sections configured"
        checks.append(VerifyCheck("plex", "libraries", has_sections, detail))
        return checks
