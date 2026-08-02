"""Render least-privilege runtime secret bundles for guest roles."""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

from toolkit.core.config.storage import env_path, hook_bundle_path
from toolkit.core.deploy.destructive_guard import write_sensitive_file

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

CONTROLLER_ONLY_SECRETS = frozenset(
    {
        "PROXMOX_API_TOKEN_ID",
        "PROXMOX_API_TOKEN_SECRET",
        "CLOUDFLARE_API_TOKEN",
    }
)
_ENV_REFERENCE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")
_RUNTIME_ENV_KEYS = frozenset(
    {
        "COMPOSE_PROFILES",
        "HOMELAB_NODE_CORES",
        "HOMELAB_NODE_MEM_MB",
        "HOMELAB_NODE",
    }
)


def _compose_service_names(path: Path) -> tuple[str, ...]:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    services = document.get("services") if isinstance(document, dict) else None
    return tuple(services) if isinstance(services, dict) else ()


def _manifest_role_secrets(root: Path, vm: str, config: Config) -> set[str]:
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_runtime_nodes
    from toolkit.core.manifest.routes import service_is_enabled

    catalog_root = root if any((root / "toolkit" / "services").glob("*/service.yaml")) else None
    required: set[str] = set()
    catalog = load_service_catalog(catalog_root)
    for manifest in catalog.manifests:
        if not service_is_enabled(config, manifest, catalog):
            continue
        compose = catalog.compose_path(manifest.name)
        runtime_names = _compose_service_names(compose) if compose.is_file() else (manifest.name,)
        if any(vm in manifest_runtime_nodes(config, manifest, name) for name in runtime_names):
            required.update(secret.name for secret in manifest.required_secrets)
    from toolkit.core.manifest.databases import compile_database_bindings
    from toolkit.core.manifest.placement import manifest_node

    required.update(
        binding.password_env
        for binding in compile_database_bindings(config, catalog)
        if manifest_node(config, catalog.require(binding.provider)) == vm
    )
    return required


def required_role_environment(root: Path, vm: str, config: Config | None = None) -> set[str]:
    """Return Compose, hook, and runner environment names required by one role."""
    required: set[str] = set()
    role_model = root / "generated" / vm / "compose.yaml"
    if role_model.is_file():
        required.update(_ENV_REFERENCE.findall(role_model.read_text(encoding="utf-8")))
        config_file = root / "config.yaml"
        cfg = config
        if cfg is None and config_file.is_file():
            from toolkit.core.config.config import load_config

            cfg = load_config(config_file)
        if cfg is not None:
            required.update(_manifest_role_secrets(root, vm, cfg))
        return (required | _RUNTIME_ENV_KEYS) - CONTROLLER_ONLY_SECRETS

    cfg = config
    config_file = root / "config.yaml"
    if cfg is None:
        if config_file.is_file():
            from toolkit.core.config.config import load_config

            cfg = load_config(config_file)
        else:
            from toolkit.core.config.config import Config

            cfg = Config()
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_runtime_nodes
    from toolkit.core.manifest.routes import service_is_enabled

    catalog = load_service_catalog()
    for manifest in catalog.manifests:
        if not service_is_enabled(cfg, manifest):
            continue
        compose = catalog.compose_path(manifest.name)
        if not compose.is_file():
            continue
        runtime_names = _compose_service_names(compose) or (manifest.name,)
        if not any(vm in manifest_runtime_nodes(cfg, manifest, name) for name in runtime_names):
            continue
        required.update(_ENV_REFERENCE.findall(compose.read_text(encoding="utf-8")))
    if config_file.is_file():
        from toolkit.core.config.config import load_config
        from toolkit.core.projects.database import project_database_nodes
        from toolkit.core.projects.secrets import project_database_secret_name

        cfg = load_config(config_file)
        required.update(
            project_database_secret_name(project.subdomain)
            for project in cfg.projects.entries
            if vm in project_database_nodes(cfg, project)
        )
    required.update(_manifest_role_secrets(root, vm, cfg))
    return (required | _RUNTIME_ENV_KEYS) - CONTROLLER_ONLY_SECRETS


def render_guest_bundle(root: Path, vm: str) -> Path:
    """Project the generated role env into the exact least-privilege guest bundle.

    Compose may transform secret values before writing its environment (for
    example, Vaultwarden requires a PHC hash). Hooks still need the original
    credential, so authoritative controller secrets replace matching values
    only after the role boundary has been calculated.
    """
    source = env_path(vm, root)
    if not source.is_file():
        raise FileNotFoundError(f"Generated role environment missing: {source}")
    required = required_role_environment(root, vm)
    selected: dict[str, str] = {}
    for line in source.read_text(encoding="utf-8").splitlines():
        name, separator, _value = line.partition("=")
        if separator and name in required:
            selected[name] = line

    from toolkit.core.config.storage import secrets_path
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    controller_store = secrets_path(root)
    if controller_store.is_file():
        for name, value in load_secrets_plaintext(controller_store).items():
            if name in required:
                escaped = value.replace("\\", "\\\\").replace("'", "\\'")
                selected[name] = f"{name}='{escaped}'"
    path = hook_bundle_path(vm, root)
    write_sensitive_file(path, "".join(f"{selected[name]}\n" for name in sorted(selected)))
    path.parent.chmod(0o700)
    return path


def assert_guest_bundle_safe(bundle: Path) -> None:
    content = bundle.read_text(encoding="utf-8")
    leaked = [name for name in CONTROLLER_ONLY_SECRETS if f"{name}=" in content]
    if leaked:
        raise ValueError(f"Guest bundle contains controller-only secrets: {', '.join(sorted(leaked))}")
