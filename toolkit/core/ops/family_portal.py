"""Family-facing portal content (bookmarks scoped to LLDAP groups)."""

from __future__ import annotations

from toolkit.core.config.config import Config
from toolkit.core.identity.service_groups import HOMELAB_SERVICE_GROUPS, invite_sections_for_groups
from toolkit.core.ops.portal_bookmarks import PortalBookmark, PortalBookmarkGroup
from toolkit.services.sdk import authelia_public_url


def family_portal_groups(cfg: Config, groups: list[str]) -> list[PortalBookmarkGroup]:
    """Bookmark sections for a family user — only apps their groups unlock."""
    sections = invite_sections_for_groups(cfg, groups)
    if not sections:
        return [
            PortalBookmarkGroup(
                "Your homelab",
                (
                    PortalBookmark(
                        "Sign-in portal",
                        authelia_public_url(cfg),
                        "Manage your password and sessions",
                    ),
                ),
            )
        ]

    out: list[PortalBookmarkGroup] = []
    for tier, cards in sections:
        items = tuple(PortalBookmark(card.label, card.url, f"{card.blurb} — {card.sign_in}") for card in cards)
        out.append(PortalBookmarkGroup(tier, items))

    out.append(
        PortalBookmarkGroup(
            "Account",
            (
                PortalBookmark(
                    "Password & security",
                    authelia_public_url(cfg),
                    "Change password or enable 2FA",
                ),
            ),
        )
    )
    return out


def tier_labels_for_groups(groups: list[str]) -> list[str]:
    names = set(groups)
    return [g.label for g in HOMELAB_SERVICE_GROUPS if g.name in names]
