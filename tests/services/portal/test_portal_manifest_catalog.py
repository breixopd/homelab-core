from __future__ import annotations

from toolkit.core.config.config import Config
from toolkit.core.manifest.catalog import load_service_catalog
from toolkit.core.manifest.routes import compile_routes
from toolkit.services.caddy.routes import compile_caddy_routes
from toolkit.services.portal.plugin import _portal_service_groups, _portal_status_summary


def test_portal_catalog_uses_manifest_auth_and_exposure() -> None:
    groups = _portal_service_groups(Config(domain="example.com"))
    services = {service["url"]: service for group in groups for service in group["services"]}

    assert services["fmd.example.com"]["auth"] == "split"
    assert services["fmd.example.com"]["exposure"] == "public"
    assert services["grafana.example.com"]["auth"] == "oidc"
    assert services["grafana.example.com"]["exposure"] == "private"
    assert services["git.example.com"]["auth"] == "forward_auth"
    assert services["git.example.com"]["service"] == "gitea"


def test_portal_collapses_secondary_routes_into_the_primary_service_card() -> None:
    groups = _portal_service_groups(Config(domain="example.com"))
    services = [service for group in groups for service in group["services"]]
    seaweedfs = next(service for service in services if service["service"] == "seaweedfs")

    assert sum(service["service"] == "seaweedfs" for service in services) == 1
    assert seaweedfs["url"] == "files.example.com"
    assert seaweedfs["auth"] == "forward_auth"
    assert seaweedfs["endpoints"] == [
        {
            "url": "s3.example.com",
            "label": "S3",
            "auth": "native",
            "exposure": "public",
        }
    ]


def test_portal_status_path_routes_to_the_authenticated_web_ui() -> None:
    routes = compile_routes(Config(domain="example.com"), load_service_catalog())
    status = next(
        route
        for route in routes
        if route.host == "example.com" and route.match is not None and route.match.paths == ("/api/portal/status",)
    )

    assert status.compose_service == "homelab-ui"
    assert status.auth.mode == "forward_auth"
    assert status.upstream == "homelab-ui:8080"

    site = next(
        site for site in compile_caddy_routes(Config(domain="example.com"), routes) if site.host == "example.com"
    )
    assert site.handlers[0].paths == ("/api/portal/status",)
    assert site.handlers[0].upstream
    assert site.handlers[0].auth_mode == "forward_auth"
    assert site.handlers[-1].file_server_root == "/srv/portal"


def test_portal_summary_does_not_claim_unchecked_services_are_reachable() -> None:
    summary = _portal_status_summary({}, configured=30)

    assert summary == {
        "green": 0,
        "yellow": 0,
        "red": 0,
        "gray": 30,
        "total": 30,
        "has_checks": False,
    }


def test_portal_summary_reports_real_checks_when_available() -> None:
    summary = _portal_status_summary(
        {"portal": "green", "grafana": "yellow", "missing": "unexpected"},
        configured=3,
    )

    assert summary["green"] == 1
    assert summary["yellow"] == 1
    assert summary["gray"] == 1
    assert summary["total"] == 3
    assert summary["has_checks"] is True
