"""Manifest-owned operator guidance contracts."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from toolkit.core.manifest.schema import OperatorGuidanceManifest, ServiceManifest


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


def test_guidance_requires_matching_phase_and_category() -> None:
    with pytest.raises(ValidationError, match="pre-deploy guidance must be a prerequisite"):
        OperatorGuidanceManifest(
            id="account",
            phase="pre_deploy",
            category="Optional",
            title="Account",
            instructions="Create the upstream account.",
        )


def test_guidance_rejects_unknown_template_variables() -> None:
    with pytest.raises(ValidationError, match="supports only"):
        OperatorGuidanceManifest(
            id="account",
            phase="post_deploy",
            category="Required",
            title="Account",
            instructions="Visit {hostname}.",
        )


def test_guidance_url_requires_one_default_route() -> None:
    with pytest.raises(ValidationError, match="guidance URLs require exactly one default route"):
        _service(
            {
                "guidance": [
                    {
                        "id": "account",
                        "phase": "post_deploy",
                        "category": "Required",
                        "title": "Account",
                        "instructions": "Visit {url}.",
                        "route_url": True,
                    }
                ]
            }
        )


def test_guidance_ids_are_unique_within_service() -> None:
    entry = {
        "id": "account",
        "phase": "post_deploy",
        "category": "Required",
        "title": "Account",
        "instructions": "Complete account setup.",
    }
    with pytest.raises(ValidationError, match="guidance IDs must be unique"):
        _service({"guidance": [entry, entry]})
