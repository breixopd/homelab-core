"""Management category profile selection."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


def management_selected_compose_profiles(config: Config) -> list[str]:
    profiles = ["management", "svc-homelab-ui"]
    if config.backups.enabled:
        profiles.append("svc-kopia")
    return sorted(set(profiles))
