"""Compile service-owned Ansible credentials for protected, ephemeral injection."""

from __future__ import annotations

from pathlib import Path


def deployment_secret_variables(root: Path) -> dict[str, str]:
    """Return enabled plugins' non-empty Ansible variables without persisting them."""
    from toolkit.core.config.config import load_config
    from toolkit.core.config.storage import config_path, secrets_path

    configured = config_path(root)
    encrypted = secrets_path(root)
    if not configured.is_file() or not encrypted.is_file():
        return {}

    from toolkit.core.secrets.rotation_context import load_previous_secret_values
    from toolkit.core.secrets.secrets import load_secrets_plaintext
    from toolkit.services import discover_service_plugins

    cfg = load_config(configured)
    secrets = load_secrets_plaintext(encrypted)
    previous_secrets = load_previous_secret_values(root)
    compiled: dict[str, str] = {}
    owners: dict[str, str] = {}
    for plugin in discover_service_plugins():
        if not plugin.is_enabled(cfg):
            continue
        for variable, value in plugin.ansible_secret_variables(cfg, secrets).items():
            if not value:
                continue
            previous = owners.get(variable)
            if previous is not None:
                raise ValueError(
                    f"Ansible secret variable {variable!r} is owned by both {previous!r} and {plugin.service!r}"
                )
            compiled[variable] = value
            owners[variable] = plugin.service
        if previous_secrets:
            for variable, value in plugin.ansible_secret_variables(cfg, previous_secrets).items():
                if not value:
                    continue
                previous_variable = f"{variable}_previous"
                previous_owner = owners.get(previous_variable)
                if previous_owner is not None:
                    raise ValueError(
                        f"Ansible secret variable {previous_variable!r} is owned by both "
                        f"{previous_owner!r} and {plugin.service!r}"
                    )
                compiled[previous_variable] = value
                owners[previous_variable] = plugin.service
    return compiled
