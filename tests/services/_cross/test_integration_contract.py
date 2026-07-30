from __future__ import annotations

import json

import pytest
from toolkit.core.manifest.schema import IntegrationContractManifest
from toolkit.services.sdk import validate_integration_contract


@pytest.fixture
def expected() -> IntegrationContractManifest:
    return IntegrationContractManifest(
        version=1,
        compatibility="1.x",
        capabilities=("status", "metrics"),
    )


def test_contract_accepts_required_capability_superset(expected: IntegrationContractManifest) -> None:
    result = validate_integration_contract(
        "example",
        expected,
        json.dumps(
            {
                "service": "example",
                "integration_contract": {
                    "version": 1,
                    "compatibility": "1.x",
                    "capabilities": ["metrics", "status", "optional-feature"],
                },
            }
        ),
    )

    assert result.passed is True


@pytest.mark.parametrize(
    ("payload", "detail"),
    [
        ({"service": "other", "integration_contract": {}}, "identity mismatch"),
        ({"service": "example"}, "object missing"),
        (
            {
                "service": "example",
                "integration_contract": {"version": 2, "compatibility": "2.x", "capabilities": []},
            },
            "version or shape mismatch",
        ),
        (
            {
                "service": "example",
                "integration_contract": {"version": 1, "compatibility": "1.x", "capabilities": ["status"]},
            },
            "missing capabilities: metrics",
        ),
        (
            {
                "service": "example",
                "integration_contract": {
                    "version": 1,
                    "compatibility": "1.x",
                    "capabilities": ["status", "status"],
                },
            },
            "version or shape mismatch",
        ),
        (
            {
                "service": "example",
                "integration_contract": {
                    "version": 1,
                    "compatibility": "1.x",
                    "capabilities": ["status", "Invalid Capability"],
                },
            },
            "version or shape mismatch",
        ),
        (
            {
                "service": "example",
                "integration_contract": {
                    "version": 1,
                    "compatibility": "1.x",
                    "capabilities": ["status", {"name": "metrics"}],
                },
            },
            "version or shape mismatch",
        ),
    ],
)
def test_contract_fails_closed(payload: dict[str, object], detail: str, expected: IntegrationContractManifest) -> None:
    result = validate_integration_contract("example", expected, json.dumps(payload))

    assert result.passed is False
    assert detail in result.detail


def test_contract_rejects_invalid_json(expected: IntegrationContractManifest) -> None:
    result = validate_integration_contract("example", expected, "not-json")

    assert result.passed is False
    assert result.detail == "invalid contract JSON"
