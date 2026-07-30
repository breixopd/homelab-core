from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


@dataclass(slots=True)
class Service:
    name: str
    label: str
    description: str
    image: str = ""


@dataclass(frozen=True, slots=True)
class AccessGroup:
    name: str
    label: str
    description: str
    default_invite: bool = False
    administrator: bool = False


@dataclass
class Category:
    name: str
    label: str
    compose_file: str
    placement: str = "control"
    always_on: bool = False
    description: str = ""
    priority: int = 100
    compose_profiles: list[str] = field(default_factory=list)
    service_group: str = ""
    access_group: AccessGroup | None = None
    _depends_on: list[str] = field(default_factory=list, repr=False)
    _validate: Callable[[Config], list[str]] | None = field(default=None, repr=False)
    _selected_compose_profiles: Callable[..., list[str]] | None = field(default=None, repr=False)

    def services(self, config: Config) -> list[Service]:
        """Return enabled service applications projected from strict manifests."""
        from toolkit.core.manifest.catalog import load_service_catalog
        from toolkit.core.manifest.routes import service_is_enabled

        return [
            Service(name=manifest.name, label=manifest.label, description=manifest.description)
            for manifest in load_service_catalog().manifests
            if manifest.category == self.name and service_is_enabled(config, manifest)
        ]

    def runtime_node(self, config: Config) -> str:
        from toolkit.core.manifest.placement import category_node

        return category_node(config, self.placement)

    def depends_on(self) -> list[str]:
        return list(self._depends_on)

    def selected_compose_profiles(self, config: Config) -> list[str]:
        if self._selected_compose_profiles:
            return self._selected_compose_profiles(config)
        return list(self.compose_profiles) if self.compose_profiles else [self.name]

    def validate(self, config: Config) -> list[str]:
        if self._validate:
            return self._validate(config)
        return []
