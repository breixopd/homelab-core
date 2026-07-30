"""Plugin-owned directory access groups, route policy, and invite cards."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.categories import AccessGroup
    from toolkit.core.config.config import Config
    from toolkit.core.manifest.catalog import ServiceCatalog
    from toolkit.core.manifest.schema import ServiceManifest


@dataclass(frozen=True, slots=True)
class ServiceGroup:
    name: str
    label: str
    description: str
    default_invite: bool
    administrator: bool


@dataclass(frozen=True, slots=True)
class ServiceCard:
    """One app row for invite email and activation success page."""

    label: str
    url: str
    blurb: str
    sign_in: str


def service_groups() -> tuple[ServiceGroup, ...]:
    """Discover directory access groups from category plugins."""
    from toolkit.core.compose.registry import all_categories, load_all

    load_all()
    declared: list[tuple[int, AccessGroup]] = [
        (category.priority, category.access_group) for category in all_categories() if category.access_group is not None
    ]
    declared.sort(key=lambda item: (not item[1].default_invite, item[0], item[1].name))
    return tuple(
        ServiceGroup(
            name=group.name,
            label=group.label,
            description=group.description,
            default_invite=group.default_invite,
            administrator=group.administrator,
        )
        for _priority, group in declared
    )


HOMELAB_SERVICE_GROUPS: tuple[ServiceGroup, ...] = service_groups()
HOMELAB_GROUP_NAMES: tuple[str, ...] = tuple(group.name for group in HOMELAB_SERVICE_GROUPS)
ADMIN_SERVICE_GROUPS: tuple[str, ...] = tuple(group.name for group in HOMELAB_SERVICE_GROUPS if group.administrator)
OWNER_BOOTSTRAP_GROUPS: tuple[str, ...] = ("lldap_admin", *HOMELAB_GROUP_NAMES)
DEFAULT_NEW_USER_GROUPS: tuple[str, ...] = tuple(group.name for group in HOMELAB_SERVICE_GROUPS if group.default_invite)


def effective_access_groups(manifest: ServiceManifest) -> tuple[str, ...]:
    """Resolve a service override or its category plugin's default group."""
    if manifest.identity.access_groups:
        return manifest.identity.access_groups
    from toolkit.core.compose.registry import get_category, load_all

    load_all()
    group = get_category(manifest.category).service_group
    return (group,) if group else ()


def default_user_groups_for_enabled_services(services) -> list[str]:
    """Return enabled plugin groups marked as routine invite defaults."""
    from toolkit.core.compose.registry import all_categories, load_all

    load_all()
    return [
        category.access_group.name
        for category in sorted(all_categories(), key=lambda item: (item.priority, item.name))
        if category.access_group is not None
        and category.access_group.default_invite
        and services.enabled(category.name)
    ]


def validate_service_groups(groups: list[str] | tuple[str, ...]) -> list[str]:
    """Validate selected plugin groups without permitting LLDAP built-in roles."""
    selected = list(dict.fromkeys(groups))
    unsupported = sorted(set(selected) - set(HOMELAB_GROUP_NAMES))
    if unsupported:
        raise ValueError(f"unsupported user groups: {', '.join(unsupported)}")
    return selected


def _subjects(groups: tuple[str, ...]) -> list[str]:
    ordered = dict.fromkeys((*groups, *ADMIN_SERVICE_GROUPS))
    return [*(f"group:{group}" for group in ordered), "group:lldap_admin"]


def build_authelia_access_rules(
    cfg: Config,
    catalog: ServiceCatalog | None = None,
) -> list[dict]:
    """Compile least-privilege Authelia rules from enabled service routes."""
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import compile_routes

    selected = catalog or load_service_catalog()
    manifests = {manifest.name: manifest for manifest in selected.manifests}
    routes = compile_routes(cfg, selected)
    auth_host = next(
        (route.host for route in routes if route.service == "authelia" and route.match is None),
        f"auth.{cfg.domain}",
    )
    rules: list[dict] = [{"domain": auth_host, "policy": "bypass"}]
    seen_hosts = {auth_host}
    for route in routes:
        if route.match is not None or route.host in seen_hosts:
            continue
        manifest = manifests.get(route.service)
        if manifest is None:
            continue
        groups = effective_access_groups(manifest)
        if not groups:
            continue
        rules.append(
            {
                "domain": route.host,
                "policy": "one_factor",
                "subjects": _subjects(groups),
            }
        )
        seen_hosts.add(route.host)
    rules.append(
        {
            "domain": f"*.{cfg.domain}",
            "policy": "one_factor",
            "subjects": _subjects(ADMIN_SERVICE_GROUPS),
        }
    )
    return rules


def service_urls_for_groups(
    cfg: Config,
    groups: list[str],
    *,
    catalog: ServiceCatalog | None = None,
) -> list[tuple[str, str]]:
    """Human-readable service URLs for invite notifications."""
    return [
        (card.label, card.url)
        for _tier, cards in invite_sections_for_groups(cfg, groups, catalog=catalog)
        for card in cards
    ]


def invite_sections_for_groups(
    cfg: Config,
    groups: list[str],
    *,
    catalog: ServiceCatalog | None = None,
) -> list[tuple[str, list[ServiceCard]]]:
    """Compile grouped invite cards from enabled service manifests and routes."""
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import compile_routes

    selected = catalog or load_service_catalog()
    selected_groups = set(groups)
    default_routes = {route.service: route for route in compile_routes(cfg, selected) if route.match is None}
    cards_by_group: dict[str, list[tuple[int, str, ServiceCard]]] = {}
    scheme = "http" if cfg.domain == "localhost" else "https"
    for manifest in selected.manifests:
        card = manifest.identity.invite
        if card is None or card.group not in selected_groups:
            continue
        route = default_routes.get(manifest.name)
        if route is None:
            continue
        cards_by_group.setdefault(card.group, []).append(
            (
                card.priority,
                manifest.name,
                ServiceCard(
                    label=manifest.label,
                    url=f"{scheme}://{route.host}{card.path if card.path != '/' else ''}",
                    blurb=card.blurb,
                    sign_in=card.sign_in,
                ),
            )
        )

    sections: list[tuple[str, list[ServiceCard]]] = []
    for group in HOMELAB_SERVICE_GROUPS:
        entries = cards_by_group.get(group.name, [])
        if entries:
            sections.append((group.label, [entry[2] for entry in sorted(entries)]))
    return sections
