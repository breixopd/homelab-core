"""portal service plugin — defaults from service.yaml; override post_start/verify/heal when needed."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.generate.artifacts import ArtifactGenerationContext
    from toolkit.services.sdk import VerifyCheck


def _portal_service_meta() -> dict[str, tuple[str, str]]:
    from toolkit.core.manifest.catalog import load_service_catalog

    return {
        manifest.name: (
            manifest.icon,
            manifest.name.replace("-server", "").replace("-core", "")[:10],
        )
        for manifest in load_service_catalog().manifests
    }


def _portal_route_entry(
    route,
    *,
    label: str,
    description: str,
    portal_meta: dict[str, tuple[str, str]],
    container_status: dict[str, str] | None,
) -> dict[str, object]:
    service = route.compose_service or route.service
    metadata = portal_meta.get(service) or portal_meta.get(route.service)
    icon = metadata[0] if metadata else route.service[:1].upper()
    tag = metadata[1] if metadata else route.service
    status_key = service if container_status and service in container_status else route.service
    return {
        "url": route.host,
        "name": label,
        "description": description,
        "icon": icon,
        "tag": tag,
        "service": service,
        "exposure": route.exposure,
        "status": (container_status or {}).get(status_key, "gray"),
        "auth": route.auth.mode,
    }


def _portal_endpoint_entry(route) -> dict[str, str]:
    label = (route.subdomain or route.host).replace("-", " ").upper()
    return {
        "url": route.host,
        "label": label,
        "auth": route.auth.mode,
        "exposure": route.exposure,
    }


def _portal_service_groups(cfg: Config, container_status: dict[str, str] | None = None) -> list[dict]:
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import compile_routes

    catalog = load_service_catalog()
    manifests = {manifest.name: manifest for manifest in catalog.manifests}
    portal_meta = _portal_service_meta()
    grouped: dict[str, dict] = {}
    service_routes: dict[str, list] = {}
    for route in compile_routes(cfg, catalog):
        if route.match is not None or route.file_server_root or route.service.startswith("project-"):
            continue
        service_routes.setdefault(route.service, []).append(route)

    for service, routes in service_routes.items():
        manifest = manifests[service]
        preferred_subdomain = manifest.operator_bookmark.route_subdomain if manifest.operator_bookmark else None
        primary = next((route for route in routes if route.subdomain == preferred_subdomain), routes[0])
        secondary = [route for route in routes if route is not primary]
        group = grouped.setdefault(
            manifest.category,
            {
                "id": manifest.category,
                "label": manifest.category.replace("-", " ").title(),
                "services": [],
            },
        )
        entry = _portal_route_entry(
            primary,
            label=manifest.label,
            description=manifest.description,
            portal_meta=portal_meta,
            container_status=container_status,
        )
        entry["endpoints"] = [_portal_endpoint_entry(route) for route in secondary]
        group["services"].append(entry)
    return list(grouped.values())


def _portal_status_summary(
    container_status: dict[str, str] | None,
    *,
    configured: int = 0,
) -> dict[str, int | bool]:
    counts: dict[str, int | bool] = {
        "green": 0,
        "yellow": 0,
        "red": 0,
        "gray": 0,
        "total": configured,
        "has_checks": bool(container_status),
    }
    for value in (container_status or {}).values():
        key = value if value in {"green", "yellow", "red", "gray"} else "gray"
        counts[key] = int(counts[key]) + 1
    if not container_status:
        counts["gray"] = configured
    return counts


def _portal_quick_links(cfg: Config) -> list[dict[str, str]]:
    from toolkit.core.ops.portal_bookmarks import portal_bookmark_groups

    links: list[dict[str, str]] = []
    for group in portal_bookmark_groups(cfg):
        for item in group.items[:6] if group.name == "Quick actions" else group.items[:3]:
            links.append({"title": item.title, "href": item.href, "description": item.description})
    return links[:12]


def _portal_projects_list(cfg: Config) -> list[dict[str, str]]:
    entries = cfg.projects.entries if cfg.projects else []
    return [
        {"url": f"{project.subdomain}.{cfg.domain}", "name": project.name or project.subdomain}
        for project in entries
        if project.show_on_portal and project.subdomain
    ]


class PortalPlugin(ServicePlugin):
    service = "portal"
    category = "management"

    def generate_artifacts(self, context: ArtifactGenerationContext) -> None:
        container_status: dict[str, str] = {}
        service_groups = _portal_service_groups(context.config, container_status)
        configured_services = sum(len(group["services"]) for group in service_groups)
        context.render_template(
            "generated/portal/index.html",
            "portal.html.j2",
            {
                "domain": context.config.domain,
                "service_groups": service_groups,
                "status_summary": _portal_status_summary(
                    container_status,
                    configured=configured_services,
                ),
                "quick_links": _portal_quick_links(context.config),
                "homelab_ui_url": f"https://homelab.{context.config.domain}",
                "projects": _portal_projects_list(context.config),
            },
        )
        context.render_template(
            "generated/portal/favicon.svg",
            "favicon.svg",
            {},
        )

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        import httpx
        from toolkit.core.ops.dns import resolve_public_dns_ip
        from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_curl, parse_curl_headers, ssh_on_vm

        if not cfg.category_enabled("management"):
            return [VerifyCheck("portal", "apex", True, "management not enabled")]

        if cfg.domain == "localhost":
            if container_exists_on_vm(cfg, vm_ip, "portal", root):
                rc, body = docker_curl(cfg, vm_ip, "portal", "http://localhost/", root=root, timeout=10)
                ok = rc == 0
                detail = "localhost portal ok" if ok else (body or "unreachable")[:80]
                return [VerifyCheck("portal", "apex", ok, detail)]
            return [VerifyCheck("portal", "apex", True, "skipped (localhost)")]

        url = f"https://{cfg.domain}/"
        try:
            resp = httpx.get(url, timeout=15, follow_redirects=True)
        except httpx.HTTPError:
            resp = None
        if resp is not None:
            ok = resp.status_code < 500
            return [VerifyCheck("portal", "apex", ok, f"HTTP {resp.status_code}")]
        public_ip, _ = resolve_public_dns_ip(cfg)
        if cfg.is_multi_node and public_ip:
            infra_ip = self.runtime_address(cfg)
            deploy_root = root.resolve()
            shell = (
                f"curl -skI --max-time 12 --resolve {cfg.domain}:443:127.0.0.1 "
                f"-H 'X-Forwarded-Proto: https' https://{cfg.domain}/ 2>&1 | head -15"
            )
            rc, out, _ = ssh_on_vm(cfg, infra_ip, shell, root=deploy_root, timeout=20)
            status, _h = parse_curl_headers(out or "")
            if status is not None:
                ok = status < 500
                return [VerifyCheck("portal", "apex", ok, f"HTTP {status} via infra")]
        return [VerifyCheck("portal", "apex", False, "unreachable")]
