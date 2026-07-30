"""Resolve service-owned bootstrap credential fallbacks."""

from __future__ import annotations


def resolve_bootstrap_password(secrets: dict[str, str], key: str) -> str:
    """Resolve a credential and its service-manifest fallback, if declared."""
    value = (secrets.get(key) or "").strip()
    if value:
        return value

    from toolkit.core.manifest.catalog import load_service_catalog

    for manifest in load_service_catalog().manifests:
        for secret in manifest.required_secrets:
            if secret.name == key and secret.fallback_env is not None:
                return (secrets.get(secret.fallback_env) or "").strip()
    return ""
