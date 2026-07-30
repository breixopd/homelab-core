"""Manifest-owned operator bookmark contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from toolkit.core.manifest.schema import ServiceManifest


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


def test_bookmark_requires_an_unambiguous_default_route() -> None:
    with pytest.raises(ValidationError, match="bookmark requires exactly one default route"):
        _service(
            {
                "operator_bookmark": {
                    "section": "Quick actions",
                    "priority": 10,
                    "description": "Open Example",
                }
            }
        )


def test_bookmark_route_selector_must_match_a_default_route() -> None:
    with pytest.raises(ValidationError, match="route selector does not match"):
        _service(
            {
                "routes": [
                    {
                        "subdomain": "files",
                        "upstream": "example:8080",
                        "exposure": "private",
                        "auth": {"mode": "forward_auth"},
                    }
                ],
                "operator_bookmark": {
                    "section": "Storage",
                    "priority": 10,
                    "description": "Open Example",
                    "route_subdomain": "other",
                },
            }
        )
