"""Tests for portal bookmark generation."""

from __future__ import annotations

from toolkit.core.config.config import Config
from toolkit.core.manifest.catalog import ServiceCatalog
from toolkit.core.manifest.schema import ServiceManifest
from toolkit.core.ops.portal_bookmarks import PortalBookmark, PortalBookmarkGroup, portal_bookmark_groups


def test_portal_bookmarks_include_security_when_enabled():
    cfg = Config(domain="example.com", email="admin@example.com")
    cfg.services.security = True
    cfg.services.media = True
    cfg.services.cloud = True
    groups = portal_bookmark_groups(cfg)
    titles = {item.title for group in groups for item in group.items}
    assert "Wazuh Dashboard" in titles
    # Headscale is a control API without a bundled Web UI; its nodes and users
    # are managed through Homelab's service-management page.
    assert "Headscale" not in titles
    assert "qBittorrent" in titles
    assert "Komodo" in titles
    assert "Gitea" in titles
    sonarr = next(item for group in groups for item in group.items if item.title == "Sonarr")
    assert sonarr.href == "https://sonarr.example.com"


def test_portal_bookmarks_omit_security_when_disabled():
    cfg = Config(domain="example.com", email="admin@example.com")
    cfg.services.security = False
    groups = portal_bookmark_groups(cfg)
    titles = {item.title for group in groups for item in group.items}
    assert "Wazuh Dashboard" not in titles
    assert "Headscale" not in titles


def test_custom_service_contributes_bookmark_without_core_change() -> None:
    manifest = ServiceManifest.model_validate(
        {
            "name": "example",
            "label": "Example",
            "description": "Custom service",
            "icon": "box",
            "category": "cloud",
            "placement": "apps",
            "priority": 50,
            "routes": [
                {
                    "subdomain": "custom",
                    "upstream": "example:8080",
                    "exposure": "private",
                    "auth": {"mode": "forward_auth"},
                }
            ],
            "operator_bookmark": {
                "section": "Custom tools",
                "priority": 10,
                "description": "Manage Example",
            },
        }
    )

    groups = portal_bookmark_groups(
        Config(domain="example.com", services={"cloud": True}),
        ServiceCatalog((manifest,)),
    )

    assert groups == [
        PortalBookmarkGroup(
            "Custom tools",
            (PortalBookmark("Example", "https://custom.example.com", "Manage Example"),),
        )
    ]
