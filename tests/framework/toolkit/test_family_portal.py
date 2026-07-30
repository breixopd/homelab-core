from __future__ import annotations

from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.ops.family_portal import family_portal_groups, tier_labels_for_groups


def test_family_portal_groups_fallback_when_no_sections():
    cfg = Config(domain="example.com", services=ServicesConfig())
    groups = family_portal_groups(cfg, ["homelab-unknown"])

    assert len(groups) == 1
    assert groups[0].name == "Your homelab"
    assert groups[0].items[0].href == "https://auth.example.com"


def test_family_portal_groups_media_user_gets_account_section():
    cfg = Config(domain="example.com", services=ServicesConfig(media=True))
    groups = family_portal_groups(cfg, ["homelab-media"])

    names = [g.name for g in groups]
    assert "Account" in names
    account = next(g for g in groups if g.name == "Account")
    assert account.items[0].title == "Password & security"


def test_tier_labels_for_groups_maps_known_groups():
    labels = tier_labels_for_groups(["homelab-media", "homelab-cloud", "missing"])
    assert "Media" in labels
    assert "Cloud" in labels
    assert "missing" not in labels
