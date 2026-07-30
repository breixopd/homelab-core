from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


@click.command()
@click.option("--preset", help="Skip wizard: all, minimal, or an installed category ID")
@click.option("--smoke-test", is_flag=True, help="CI/local: merged node .env at <root>/.env for compose validation")
@click.option("--yes", "-y", is_flag=True, help="Non-interactive (skip confirmation in wizard)")
@click.option("--force", is_flag=True, help="Re-run wizard even when config.yaml exists")
@click.pass_context
def install(ctx, preset, smoke_test, yes, force):
    """Interactive first-run setup wizard (auto-skips when config exists)."""
    import os

    root = Path(ctx.obj["root"])
    from toolkit.core.config.config import load_config, save_config
    from toolkit.core.config.storage import config_path, env_path, secrets_path
    from toolkit.core.generate.generate import generate_all, generate_configs, render_env
    from toolkit.core.secrets.secrets import generate_all_secrets, get_required_secrets, save_secrets_plaintext

    cp = config_path(root)

    # Auto-skip when config already exists
    if cp.exists() and not force:
        click.echo("config.yaml already exists. Current configuration:")
        click.echo(f"  Path: {cp}")
        cfg = load_config(cp)
        click.echo(f"  Domain: {cfg.domain}")
        click.echo(f"  Services: {', '.join(cfg.enabled_categories)}")
        click.echo(f"  Nodes: {', '.join(cfg.enabled_nodes)}")
        click.echo("\nUse --force to re-run the wizard, or:")
        click.echo("  homelab-toolkit config edit    # edit config.yaml")
        click.echo("  homelab-toolkit config show    # view full config")
        click.echo("  homelab-toolkit secrets generate  # regenerate secrets")
        click.echo("  homelab-toolkit deploy all     # deploy everything")
        return

    click.echo("=== Homelab Toolkit Setup ===\n")

    if preset:
        cfg = _preset_config(preset)
        from toolkit.core.manifest.setup import setup_credentials_from_environment

        optional_secrets = setup_credentials_from_environment(cfg, os.environ)
    else:
        cfg, optional_secrets = _interactive_wizard(skip_confirm=yes)

    from toolkit.cli.secrets_cmd import _secret_mutation_lease

    with _secret_mutation_lease(root, "install"):
        cp = config_path(root)
        save_config(cfg, cp)
        click.echo(f"\nConfig saved to {cp}")

        click.echo("Generating secrets...")
        specs = get_required_secrets(cfg)
        secrets_data = generate_all_secrets(specs)
        secrets_data.update(optional_secrets)
        save_secrets_plaintext(secrets_data, secrets_path(root))
        click.echo(f"  {len(secrets_data)} secrets generated")
        if optional_secrets:
            click.echo(f"  Including {len(optional_secrets)} user-provided: {', '.join(sorted(optional_secrets))}")

        click.echo("Generating .env files and configs...")
        generate_all(root)
        generate_configs(cfg, root)

    if smoke_test:

        def _parse_env_file(path: Path) -> dict[str, str]:
            out: dict[str, str] = {}
            if not path.exists():
                return out
            for line in path.read_text().splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                out[key.strip()] = val.strip()
            return out

        merged: dict[str, str] = {}
        merged.update(_parse_env_file(env_path(cfg.control_node, root)))
        for node in cfg.enabled_nodes:
            if node == cfg.control_node:
                continue
            for k, v in _parse_env_file(env_path(node, root)).items():
                if k not in merged:
                    merged[k] = v
        (root / ".env").write_text(render_env(merged))
        click.echo(f"\nSmoke-test: wrote {root / '.env'} (merged node env for compose validation)")

    click.echo("\n=== Setup Complete ===")
    click.echo(f"  Domain: {cfg.domain}")
    click.echo(f"  Services: {', '.join(cfg.enabled_categories)}")
    click.echo(f"  Nodes: {', '.join(cfg.enabled_nodes)}")
    click.echo("\nNext steps:")
    click.echo("  1. Review config:  homelab-toolkit config show")
    click.echo("  2. Deploy:         homelab-toolkit deploy all")
    click.echo("  3. Check status:   homelab-toolkit status")


def _preset_config(preset: str) -> Config:
    """Build config from preset name."""
    from toolkit.core.config.config import Config, ServicesConfig

    def category_selection(*enabled: str) -> ServicesConfig:
        from toolkit.core.compose.registry import all_categories, load_all

        load_all()
        selected = set(enabled)
        return ServicesConfig.model_validate(
            {category.name: category.always_on or category.name in selected for category in all_categories()}
        )

    if preset == "all":
        return Config()
    if preset == "minimal":
        return _scope_machines_to_enabled_services(Config(services=category_selection()))
    from toolkit.core.compose.registry import all_categories, load_all

    load_all()
    category_names = {category.name for category in all_categories()}
    if preset not in category_names:
        raise click.BadParameter(f"Unknown preset or category: {preset}", param_hint="--preset")
    return _scope_machines_to_enabled_services(Config(services=category_selection(preset)))


def _scope_machines_to_enabled_services(cfg: Config) -> Config:
    """Disable machines that own no enabled service in a reduced preset."""
    from toolkit.core.config.config import Config
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_node
    from toolkit.core.manifest.routes import service_is_enabled

    catalog = load_service_catalog()
    required = {cfg.control_node}
    required.update(
        manifest_node(cfg, manifest) for manifest in catalog.manifests if service_is_enabled(cfg, manifest, catalog)
    )
    machines = {
        machine_id: machine.model_copy(update={"enabled": machine_id in required})
        for machine_id, machine in cfg.machines.items()
    }
    return Config.model_validate({**cfg.model_dump(mode="python"), "machines": machines})


def _interactive_wizard(*, skip_confirm: bool = False):
    """Interactive CLI wizard."""
    from toolkit.core.config.config import Config, ServicesConfig, StorageConfig
    from toolkit.core.machines import load_default_machines

    # Step 1: Domain
    click.echo("Step 1: Domain Configuration")
    domain = click.prompt("  Domain name", default="localhost")
    if not _validate_domain(domain):
        raise click.BadParameter("Invalid domain format. Use example: 'homelab.local' or 'example.com'")
    email = click.prompt("  Email (for SSL certs)", default=f"admin@{domain}")
    timezone = click.prompt("  Timezone", default="UTC")

    # Step 2: Services
    click.echo("\nStep 2: Service Categories")
    services = {}
    from toolkit.core.compose.registry import all_categories, load_all

    load_all()
    categories = sorted(all_categories(), key=lambda category: (category.priority, category.name))
    for category in categories:
        if category.always_on:
            services[category.name] = True
            click.echo(f"  {category.label}: enabled (required infrastructure)")
            continue
        prompt = category.label
        if category.description:
            prompt = f"{prompt} - {category.description}"
        services[category.name] = click.confirm(f"  Enable {prompt}?", default=True)

    # Step 3: Service settings
    click.echo("\nStep 3: Service Configuration")
    selected_services = ServicesConfig.model_validate(services)
    service_settings = _collect_service_settings(selected_services)

    # Step 4: Infrastructure
    click.echo("\nStep 4: Infrastructure")
    machines = load_default_machines()
    for machine_id, machine in machines.items():
        enabled = click.confirm(f"  Enable {machine_id} ({machine.kind.upper()})?", default=machine.enabled)
        address = machine.address
        if enabled:
            address = click.prompt(f"    {machine_id} address", default=machine.address)
        machines[machine_id] = machine.model_copy(update={"enabled": enabled, "address": address})

    # Step 5: Storage
    click.echo("\nStep 5: Storage")
    fs = click.prompt("  Filesystem", type=click.Choice(["zfs", "ext4", "xfs"]), default="zfs")
    raid = click.prompt(
        "  RAID level",
        type=click.Choice(["mirror", "raidz1", "raidz2", "stripe", "none"]),
        default="mirror",
    )
    disk_count = click.prompt("  Number of disks", type=int, default="2")
    disk_size = click.prompt("  Total raw capacity (GB)", type=int, default="4000")

    # Build config
    cfg = Config(
        domain=domain,
        email=email,
        timezone=timezone,
        services=selected_services,
        service_settings=service_settings,
        machines=machines,
        storage=StorageConfig(
            filesystem=fs,
            raid_level=raid,
            disk_count=disk_count,
            raw_disks_gb=disk_size,
        ),
    )

    # Step 6: Service credentials
    optional_secrets = _collect_service_credentials(cfg)

    # Summary
    click.echo("\n--- Summary ---")
    click.echo(f"  Domain:     {cfg.domain}")
    click.echo(f"  Categories: {', '.join(cfg.enabled_categories)}")
    click.echo(f"  Nodes:      {', '.join(cfg.enabled_nodes)}")
    click.echo(f"  Storage:    {cfg.storage.filesystem} {cfg.storage.raid_level}")
    if cfg.storage.usable_gb > 0:
        click.echo(f"  Usable:     {cfg.storage.usable_gb} GB")

    if not skip_confirm and not click.confirm("\nProceed with this configuration?", default=True):
        raise click.Abort()

    return cfg, optional_secrets


def _validate_domain(domain: str) -> bool:
    """Validate domain format (basic check, not RFC-complete)."""
    import re

    if not domain or len(domain) > 253:
        return False
    # Allow localhost, single-label, and FQDN
    _label = r"[a-zA-Z0-9]([a-zA-Z0-9\-]{0,61}[a-zA-Z0-9])?"
    pattern = rf"^(localhost|{_label}(\.{_label})*)$"
    return bool(re.match(pattern, domain))


def _collect_service_settings(services) -> dict[str, dict[str, bool | int | float | str]]:
    """Prompt for every manifest setting opted into first-run setup."""
    from toolkit.core.manifest.settings import validate_setting_value
    from toolkit.core.manifest.setup import setup_setting_definitions

    enabled_categories = services.model_dump(mode="python")
    values: dict[str, dict[str, bool | int | float | str]] = {}
    for manifest, setting in setup_setting_definitions():
        if not enabled_categories.get(manifest.category, False):
            continue
        label = f"  {manifest.label} - {setting.label}"
        if setting.type == "boolean":
            raw: object = click.confirm(label, default=bool(setting.default))
        elif setting.type == "select":
            raw = click.prompt(label, type=click.Choice(list(setting.choices)), default=str(setting.default))
        elif setting.type == "number":
            prompt_type = int if isinstance(setting.default, int) else float
            raw = click.prompt(label, type=prompt_type, default=str(setting.default))
        else:
            raw = click.prompt(label, default=str(setting.default))
        values.setdefault(manifest.name, {})[setting.key] = validate_setting_value(setting, raw)
    return values


def _collect_service_credentials(cfg) -> dict[str, str]:
    """Prompt for active manifest-owned service credentials."""
    from toolkit.core.manifest.setup import active_setup_secrets, prepare_bootstrap_credentials

    values: dict[str, str] = {}
    active = active_setup_secrets(cfg)
    if active:
        click.echo("\nStep 6: Service Credentials")
    for name, (_manifest, secret) in active.items():
        setup = secret.setup
        if setup is None:
            raise RuntimeError(f"active setup secret {name!r} has no setup contract")
        if setup.required:
            value = click.prompt(
                f"  {setup.label}",
                hide_input=setup.input == "password",
                show_default=False,
            )
        else:
            value = click.prompt(
                f"  {setup.label}",
                default="",
                hide_input=setup.input == "password",
                show_default=False,
            )
        if str(value).strip():
            values[name] = str(value).strip()
    return prepare_bootstrap_credentials(cfg, values)
