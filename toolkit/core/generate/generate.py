from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path

from toolkit.core.compose.docker import profiles_for_categories
from toolkit.core.compose.registry import enabled_categories, load_all
from toolkit.core.config.config import Config, load_config
from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT, env_path
from toolkit.core.secrets.secrets import generate_all_secrets, get_required_secrets, load_secrets_plaintext


def _deploy_install_root(repo_root: Path | str | None) -> Path:
    """Host paths baked into generated ``.env`` volume variables."""
    override = os.environ.get("GENERATE_INSTALL_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    default = Path(DEFAULT_HOMELAB_ROOT).resolve()
    if repo_root is None:
        return default
    repo = Path(repo_root).resolve()
    if repo == default:
        return repo
    repo_str = repo.as_posix()
    if "/tmp/" in repo_str or "pytest" in repo_str:
        return repo
    return default


def _load_generate_secrets(root: Path) -> dict[str, str]:
    """Load secrets for generation without silently downgrading security."""
    from toolkit.core.config.storage import secrets_path

    return load_secrets_plaintext(secrets_path(root))


def _existing_env_value(root: Path | None, vm: str, name: str) -> str:
    if root is None:
        return ""
    path = env_path(vm, root)
    if not path.is_file():
        return ""
    from dotenv import dotenv_values

    value = dotenv_values(path).get(name)
    return value if isinstance(value, str) else ""


def _compile_plugin_runtime_environment(
    config: Config,
    vm: str,
    secrets: dict[str, str],
    root: Path | None,
) -> dict[str, str]:
    from types import MappingProxyType

    from toolkit.services import RuntimeEnvironmentContext, enabled_plugin_runtimes

    compiled: dict[str, str] = {}
    owners: dict[str, str] = {}
    for _category, plugin, _runtimes in enabled_plugin_runtimes(config, vm):
        declared = set(plugin.manifest.runtime_variables)
        if not declared:
            continue
        scoped_secret_names = {secret.name for secret in plugin.manifest.required_secrets}
        scoped_secret_names.update(projection.source_env for projection in plugin.manifest.secret_projections)
        context = RuntimeEnvironmentContext(
            config=config,
            node=vm,
            root=root,
            secrets=MappingProxyType({name: value for name, value in secrets.items() if name in scoped_secret_names}),
            previous=MappingProxyType({name: _existing_env_value(root, vm, name) for name in declared}),
        )
        values = plugin.runtime_environment(context)
        actual = set(values)
        if actual != declared:
            missing = sorted(declared - actual)
            extra = sorted(actual - declared)
            detail = ", ".join((*[f"missing {name}" for name in missing], *[f"undeclared {name}" for name in extra]))
            raise ValueError(f"service {plugin.service!r} runtime environment contract mismatch: {detail}")
        for name, value in values.items():
            if not isinstance(value, str):
                raise TypeError(f"service {plugin.service!r} runtime variable {name!r} must be text")
            previous_owner = owners.get(name)
            if previous_owner is not None:
                raise ValueError(
                    f"runtime variable {name!r} is owned by both {previous_owner!r} and {plugin.service!r}"
                )
            owners[name] = plugin.service
            compiled[name] = value
    return compiled


def _env_escape(value: str) -> str:
    """Escape a value for safe .env file inclusion (Docker Compose compatible)."""
    if not isinstance(value, str):
        value = str(value)
    if not value:
        return ""
    if "$" in value:
        escaped = value.replace("\\", "\\\\").replace("'", "\\'").replace("\n", "\\n")
        return f"'{escaped}'"
    needs_quoting = any(c in value for c in ' #$"\\`\n\t')
    if needs_quoting:
        escaped = value
        escaped = escaped.replace("\\", "\\\\")
        escaped = escaped.replace('"', '\\"')
        escaped = escaped.replace("`", "\\`")
        escaped = escaped.replace("\n", "\\n")
        return f'"{escaped}"'
    return value


_SECRET_SUFFIXES = {".env", ".env.vpn", ".env.postgres-exporter", ".env.redis-exporter"}


def _atomic_write(path: Path, content: str, mode: int | None = None) -> None:
    """Write content atomically via temp file + rename.

    mode: explicit file mode. If None, uses 0o600 for secret files (named in
    _SECRET_SUFFIXES or ending in .env) and 0o644 for everything else.
    """
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    if mode is None:
        name = path.name
        is_secret = name in _SECRET_SUFFIXES or name.endswith(".env")
        mode = 0o600 if is_secret else 0o644
    tmp = path.with_suffix(f".tmp.{os.getpid()}")
    try:
        tmp.write_text(content)
        tmp.chmod(mode)
        tmp.replace(path)
    except Exception:
        if tmp.exists():
            tmp.unlink()
        raise


def _edge_network_vars(config: Config) -> dict[str, str]:
    import ipaddress

    from toolkit.core.infra.edge_network import edge_network_values, prometheus_egress_network_values

    subnet, caddy_ip = edge_network_values(config)
    prometheus_subnet, prometheus_ip = prometheus_egress_network_values(config)
    network = ipaddress.ip_network(subnet)
    dynamic_range = tuple(network.subnets(new_prefix=network.prefixlen + 1))[1]
    return {
        "EDGE_SUBNET": subnet,
        "EDGE_DYNAMIC_RANGE": str(dynamic_range),
        "CADDY_EDGE_IP": caddy_ip,
        "PROMETHEUS_EGRESS_SUBNET": prometheus_subnet,
        "PROMETHEUS_EGRESS_IP": prometheus_ip,
    }


def _build_env_vars(config: Config, vm: str, secrets: dict[str, str], root: Path | None = None) -> dict[str, str]:
    """Build the full .env key-value map for a given VM."""
    from toolkit.core.infra.autodetect import detect_compose_uid_gid, detect_timezone, detect_uid_gid

    env: dict[str, str] = {}

    # Global vars — use config timezone, auto-detect only as fallback when unset
    detected_tz = config.timezone or detect_timezone()
    detected_uid, detected_gid = detect_uid_gid()
    compose_uid, compose_gid = detect_compose_uid_gid()
    configured_puid = config.runtime.puid
    configured_pgid = config.runtime.pgid

    from toolkit.core.images.catalog import compose_image_env, resolve_image_tag

    env["BASE_DOMAIN"] = config.domain
    env.update(_edge_network_vars(config))
    image_tag = resolve_image_tag(Path(root or DEFAULT_HOMELAB_ROOT), config.images.tag)
    env.update(compose_image_env(config.images.registry, image_tag))
    env["TZ"] = detected_tz

    from toolkit.core.manifest.placement import service_address

    # VM-specific
    env["PRIVATE_IP"] = config.node_ip(vm) if vm in config.machines else "127.0.0.1"
    # Install root and volume source paths (always /opt/homelab on production guests)
    deploy_root = _deploy_install_root(root)
    env["INSTALL_ROOT"] = str(deploy_root)
    from toolkit.core.manifest.variables import compile_role_host_sources

    env.update(compile_role_host_sources(config, vm, deploy_root))
    if config.backups.enabled:
        if root is None:
            raise ValueError("repository root is required to generate backup TLS configuration")
        from toolkit.core.ops.backup_tls import ensure_kopia_server_certificate

        certificate = ensure_kopia_server_certificate(root, service_address(config, "kopia"))
        env["KOPIA_SERVER_CERT_FINGERPRINT"] = certificate.fingerprint
        if config.backups.target == "remote":
            from toolkit.core.ops.backup_ssh import ensure_backup_ssh_identity, write_remote_known_hosts

            host = next(
                (item for item in config.external_hosts if item.name == config.backups.storage_host),
                None,
            )
            if host is None:
                raise ValueError("configured remote backup storage host is missing")
            ensure_backup_ssh_identity(root)
            write_remote_known_hosts(root, host.ip, host.ssh_port)

    from toolkit.core.manifest.variables import (
        compile_role_secret_fallbacks,
        compile_role_secret_projections,
        compile_role_variables,
    )

    for name, value in compile_role_variables(config, vm).items():
        env.setdefault(name, value)
    projected_secrets = compile_role_secret_projections(config, vm, secrets)
    for name, value in projected_secrets.items():
        env.setdefault(name, value)
    fallback_secrets = compile_role_secret_fallbacks(config, vm, secrets)
    for name, value in fallback_secrets.items():
        env.setdefault(name, value)

    # Secrets are scoped to variables referenced by services assigned to this
    # role. Controller and other-role credentials never enter the runtime env.
    from toolkit.core.deploy.guest_bundle import required_role_environment
    from toolkit.core.projects.database import project_database_nodes
    from toolkit.core.projects.secrets import project_database_secret_name

    allowed_secrets = required_role_environment(Path(root or DEFAULT_HOMELAB_ROOT), vm, config)
    allowed_secrets.update(
        project_database_secret_name(project.subdomain)
        for project in config.projects.entries
        if vm in project_database_nodes(config, project)
    )
    for k, v in secrets.items():
        if k in allowed_secrets and k not in projected_secrets and (k not in env or not env[k]):
            env[k] = v

    env.update(_compile_plugin_runtime_environment(config, vm, secrets, root))

    env["PUID"] = str(configured_puid if configured_puid is not None else compose_uid)
    env["PGID"] = str(configured_pgid if configured_pgid is not None else compose_gid)
    return env


def render_env(env: dict[str, str]) -> str:
    """Render a dict of env vars as a .env file string."""
    lines = []
    for key in sorted(env):
        value = _env_escape(env[key])
        lines.append(f"{key}={value}")
    return "\n".join(lines) + "\n"


def write_env(config: Config, vm: str, secrets: dict[str, str], root: Path | None = None) -> Path:
    """Atomically replace a generated role environment."""
    output = env_path(vm, root)

    env_vars = _build_env_vars(config, vm, secrets, root)
    load_all()
    vm_cats = [c for c in enabled_categories(config) if c.runtime_node(config) == vm]
    profiles = profiles_for_categories(vm_cats, config)
    from toolkit.core.projects.compose import project_profiles_for_vm

    profiles = sorted(set(profiles) | set(project_profiles_for_vm(config, vm)))
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import runtime_profiles_for_node

    profiles = sorted(set(profiles) | set(runtime_profiles_for_node(config, load_service_catalog(), vm)))
    if profiles:
        env_vars["COMPOSE_PROFILES"] = ",".join(profiles)

    from toolkit.core.deploy.compose_limits import _vm_service_names, write_compose_limits
    from toolkit.core.infra.host_capacity import build_machine_resource_plans

    svc_counts = {v: len(_vm_service_names(config, v)) for v in config.enabled_nodes}
    plans = build_machine_resource_plans(config, svc_counts)
    if vm in plans:
        env_vars["HOMELAB_NODE_CORES"] = str(plans[vm].cores)
        env_vars["HOMELAB_NODE_MEM_MB"] = str(plans[vm].memory_mb)
    env_vars["HOMELAB_NODE"] = vm

    _atomic_write(output, render_env(env_vars))
    write_compose_limits(config, vm, root)
    return output


def generate_all(root: Path) -> dict[str, Path]:
    """Generate .env files for all enabled VMs. Returns map of vm→path."""
    from toolkit.core.config.storage import config_path
    from toolkit.core.config.storage import secrets_path as _secrets_path
    from toolkit.core.secrets.secrets import save_secrets_plaintext

    config = load_config(config_path(root))
    load_all()

    if (root / "stacks" / "platform.yaml").is_file():
        from toolkit.core.generate.compose_assemble import write_assembled_compose, write_role_compose_models

        write_assembled_compose(root, config)
        write_role_compose_models(root, config)

    # Load or generate secrets and persist them so generate_configs can read them
    raw_secrets = load_secrets_plaintext(_secrets_path(root))
    if config.owner_password:
        raw_secrets["SSO_USER_PASSWORD"] = config.owner_password
    specs = get_required_secrets(config)
    secrets = generate_all_secrets(specs, raw_secrets)
    save_secrets_plaintext(secrets, _secrets_path(root))

    results = {}
    for vm in config.enabled_nodes:
        path = write_env(config, vm, secrets, root)
        results[vm] = path

    from toolkit.core.deploy.guest_bundle import render_guest_bundle

    for vm in config.enabled_nodes:
        render_guest_bundle(root, vm)

    return results


def generate_configs(
    cfg: Config,
    root: Path,
    *,
    on_progress: Callable[[int, int, str], None] | None = None,
) -> list[Path]:
    """Dispatch generated runtime artifacts to enabled service owners."""
    load_all()
    from toolkit.core.generate.artifacts import generate_service_artifacts

    return generate_service_artifacts(cfg, root, _load_generate_secrets(root), on_progress=on_progress)


class GeneratedArtifactValidationError(RuntimeError):
    pass


def run_full_generate(root: Path, cfg: Config | None = None, *, validate: bool = True) -> dict[str, list[Path]]:
    """Single entry: generate env files + configs; optionally validate artifacts."""
    from toolkit.core.config.config import load_config
    from toolkit.core.config.storage import config_path
    from toolkit.core.generate.validate import validate_generated_artifacts

    root = root.resolve()
    if cfg is None:
        cfg = load_config(config_path(root))
    if (root / "toolkit" / "services").is_dir():
        from toolkit.core.manifest.catalog import load_service_catalog
        from toolkit.core.manifest.ownership import current_ownership, prune_local_ownership_tombstones

        prune_local_ownership_tombstones(root, current_ownership(cfg, load_service_catalog(root)))
    results: dict = dict(generate_all(root))
    written = generate_configs(cfg, root)
    results["configs"] = written
    if validate:
        report = validate_generated_artifacts(root)
        if report.errors:
            raise GeneratedArtifactValidationError("; ".join(report.errors))
    from toolkit.core.registry.reconcile import write_last_reconcile

    write_last_reconcile(root, cfg, trigger="generate")
    return results
