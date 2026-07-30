"""Validation primitives for independently released service modules."""

from __future__ import annotations

import json
import re
from typing import TYPE_CHECKING

from toolkit.core.verify.models import VerifyCheck

if TYPE_CHECKING:
    from toolkit.core.manifest.schema import IntegrationContractManifest

_CAPABILITY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")


def validate_integration_contract(
    service: str,
    expected: IntegrationContractManifest,
    body: str,
) -> VerifyCheck:
    """Fail closed when a module does not implement its declared core contract."""
    try:
        payload = json.loads(body)
    except (json.JSONDecodeError, UnicodeError):
        return VerifyCheck(service, "integration_contract", False, "invalid contract JSON")
    if not isinstance(payload, dict) or payload.get("service") != service:
        return VerifyCheck(service, "integration_contract", False, "contract service identity mismatch")
    contract = payload.get("integration_contract")
    if not isinstance(contract, dict):
        return VerifyCheck(service, "integration_contract", False, "contract object missing")
    version = contract.get("version")
    compatibility = contract.get("compatibility")
    capabilities = contract.get("capabilities")
    if (
        not isinstance(version, int)
        or isinstance(version, bool)
        or version != expected.version
        or compatibility != expected.compatibility
        or not isinstance(capabilities, list)
        or not capabilities
        or len(capabilities) > 64
        or any(not isinstance(value, str) or not _CAPABILITY.fullmatch(value) for value in capabilities)
        or len(capabilities) != len(set(capabilities))
    ):
        return VerifyCheck(service, "integration_contract", False, "contract version or shape mismatch")
    missing = sorted(set(expected.capabilities) - set(capabilities))
    if missing:
        return VerifyCheck(
            service,
            "integration_contract",
            False,
            "missing capabilities: " + ", ".join(missing),
        )
    return VerifyCheck(
        service,
        "integration_contract",
        True,
        f"compatible {compatibility}; {len(capabilities)} capabilities",
    )
