"""Media category validation and adaptive Compose profiles."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


def media_selected_compose_profiles(config: Config, *, root: Path | None = None) -> list[str]:
    from toolkit.core.capabilities import detect_gpu_for_vm, load_capabilities
    from toolkit.core.manifest.placement import service_node
    from toolkit.core.manifest.settings import service_enabled, service_setting_str
    from toolkit.services.jellyfin.capabilities import resolve_hw_transcode

    vpn_enabled = service_enabled(config, "gluetun")
    tdarr_enabled = service_enabled(config, "tdarr")
    server = service_setting_str(config, "media-library", "server")
    profiles = ["media", "media-vpn" if vpn_enabled else "media-no-vpn"]
    if service_enabled(config, "media-cache"):
        profiles.append("media-cache")
    if tdarr_enabled:
        profiles.append("media-tdarr")

    hw_transcode = resolve_hw_transcode(config, root=root)
    if hw_transcode in ("nvidia", "vaapi"):
        transcode_node = service_node(config, "jellyfin")
        if os.environ.get("HOMELAB_NODE") == transcode_node:
            capabilities = load_capabilities(vm="local", root=root or Path.cwd())
            if not capabilities.gpu.has_gpu_transcode:
                hw_transcode = "none"
        elif config.is_multi_node:
            gpu = detect_gpu_for_vm(config, transcode_node, root=root)
            if not gpu.source.endswith(":unreachable") and not gpu.has_gpu_transcode:
                hw_transcode = "none"
        else:
            capabilities = load_capabilities(vm="local", root=root or Path.cwd())
            if not capabilities.gpu.has_gpu_transcode:
                hw_transcode = "none"

    if server in ("jellyfin", "both"):
        profile = f"media-jellyfin-{hw_transcode}" if hw_transcode in ("nvidia", "vaapi") else "media-jellyfin"
        profiles.append(profile)
    if server in ("plex", "both"):
        profile = f"media-plex-{hw_transcode}" if hw_transcode in ("nvidia", "vaapi") else "media-plex"
        profiles.append(profile)
    return sorted(set(profiles))
