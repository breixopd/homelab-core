"""Service-owned identity access and invite presentation contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from toolkit.core.manifest.schema import InviteCardManifest, ServiceManifest


def _service(values: dict) -> ServiceManifest:
    return ServiceManifest.model_validate(
        {
            "name": "example",
            "label": "Example",
            "description": "Example service",
            "icon": "box",
            "category": "cloud",
            "placement": "apps",
            "priority": 50,
            **values,
        }
    )


def test_invite_card_accepts_safe_fragment_path() -> None:
    card = InviteCardManifest(
        group="homelab-cloud",
        path="/#/signup",
        blurb="Private service",
        sign_in="Create an account",
    )

    assert card.path == "/#/signup"


@pytest.mark.parametrize("path", ["signup", "//other.example/path", "/path?token=secret", "/{template}"])
def test_invite_card_rejects_unsafe_paths(path: str) -> None:
    with pytest.raises(ValidationError):
        InviteCardManifest(
            group="homelab-cloud",
            path=path,
            blurb="Private service",
            sign_in="Create an account",
        )


def test_invite_card_group_must_be_an_explicit_service_group_override() -> None:
    with pytest.raises(ValidationError, match="invite group must be included"):
        _service(
            {
                "identity": {
                    "access_groups": ["homelab-admin"],
                    "invite": {
                        "group": "homelab-cloud",
                        "blurb": "Private service",
                        "sign_in": "Create an account",
                    },
                }
            }
        )


def test_first_login_provisioning_requires_a_pending_message() -> None:
    with pytest.raises(ValidationError, match="first-login provisioning requires a message"):
        _service(
            {
                "identity": {
                    "provisioning": [
                        {
                            "id": "account",
                            "mode": "first_login",
                            "disabled_message": "Service disabled",
                        }
                    ]
                }
            }
        )


def test_identity_provisioning_ids_are_unique() -> None:
    entry = {
        "id": "account",
        "mode": "plugin",
        "disabled_message": "Service disabled",
    }
    with pytest.raises(ValidationError, match="provisioning IDs must be unique"):
        _service({"identity": {"provisioning": [entry, entry]}})
