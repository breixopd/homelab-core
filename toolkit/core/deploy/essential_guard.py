"""Guard deploy plans that would disable essential homelab services."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


class EssentialServicesDisabledError(RuntimeError):
    """Raised when config would disable one or more essential services."""


def essential_removal_allowed() -> bool:
    return os.environ.get("HOMELAB_ALLOW_ESSENTIAL_REMOVAL", "").strip() == "1"


def disabled_essential_services(cfg: Config) -> list[str]:
    """Return essential service names whose category is not enabled in *cfg*."""
    from toolkit.core.compose.registry import enabled_categories, get_category
    from toolkit.services import essential_service_plugins

    enabled_cats = {cat.name for cat in enabled_categories(cfg)}
    disabled: list[str] = []
    for plugin in essential_service_plugins():
        try:
            cat = get_category(plugin.category)
        except KeyError:
            cat = None
        if cat is not None and cat.always_on:
            continue
        if plugin.category not in enabled_cats:
            disabled.append(plugin.service)
    return sorted(disabled)


def assert_essential_services_enabled(cfg: Config) -> None:
    """Refuse deploy when essential services would be disabled.

    Set ``HOMELAB_ALLOW_ESSENTIAL_REMOVAL=1`` to override intentionally.
    """
    if essential_removal_allowed():
        return
    disabled = disabled_essential_services(cfg)
    if not disabled:
        return
    names = ", ".join(disabled)
    raise EssentialServicesDisabledError(
        f"Deploy would disable essential service(s): {names}. "
        "Re-enable their categories in config.yaml or set HOMELAB_ALLOW_ESSENTIAL_REMOVAL=1 "
        "to override."
    )
