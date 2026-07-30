"""Compile operator navigation from service manifests and resolved routes."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.manifest.catalog import ServiceCatalog
    from toolkit.core.manifest.routes import CompiledRoute


@dataclass(frozen=True, slots=True)
class PortalBookmark:
    title: str
    href: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class PortalBookmarkGroup:
    name: str
    items: tuple[PortalBookmark, ...]


def portal_bookmark_groups(
    cfg: Config,
    catalog: ServiceCatalog | None = None,
) -> list[PortalBookmarkGroup]:
    """Return enabled manifest-owned operator bookmarks."""
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import compile_routes

    selected = catalog or load_service_catalog()
    default_routes: dict[str, list[CompiledRoute]] = {}
    for compiled_route in compile_routes(cfg, selected):
        if compiled_route.match is None:
            default_routes.setdefault(compiled_route.service, []).append(compiled_route)

    sections: dict[str, list[tuple[int, str, PortalBookmark]]] = {}
    scheme = "http" if cfg.domain == "localhost" else "https"
    for manifest in selected.manifests:
        bookmark = manifest.operator_bookmark
        if bookmark is None:
            continue
        candidates = default_routes.get(manifest.name, [])
        route: CompiledRoute | None
        if bookmark.route_subdomain is None:
            route = candidates[0] if len(candidates) == 1 else None
        else:
            route = next(
                (candidate for candidate in candidates if candidate.subdomain == bookmark.route_subdomain),
                None,
            )
        if route is None:
            continue
        sections.setdefault(bookmark.section, []).append(
            (
                bookmark.priority,
                manifest.name,
                PortalBookmark(
                    title=manifest.label,
                    href=f"{scheme}://{route.host}",
                    description=bookmark.description,
                ),
            )
        )

    return [
        PortalBookmarkGroup(name, tuple(entry[2] for entry in sorted(entries))) for name, entries in sections.items()
    ]
