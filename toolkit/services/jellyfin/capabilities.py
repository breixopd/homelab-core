"""Jellyfin hardware-transcode selection from generic host capabilities."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.core.capabilities import detect_server_capabilities

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


def resolve_hw_transcode(cfg: Config, *, root: Path | None = None, fast: bool = True) -> str:
    from toolkit.core.manifest.placement import service_node
    from toolkit.core.manifest.settings import service_setting_str

    configured = service_setting_str(cfg, "jellyfin", "hardware-transcode")
    if configured != "auto":
        return configured
    capabilities = detect_server_capabilities(cfg, vm=service_node(cfg, "jellyfin"), root=root, fast=fast)
    return capabilities.gpu.backend if capabilities.gpu.has_gpu_transcode else "none"
