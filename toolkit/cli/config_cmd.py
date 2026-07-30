from __future__ import annotations

import io
import os
import re
import tarfile
import tempfile
from pathlib import Path, PurePosixPath

import click
import yaml

from toolkit.core.config.config import (
    Config,
    config_path_is_sensitive,
    load_config,
    redact_sensitive_config,
    save_config,
    save_local_config,
)
from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT, config_path, secrets_path, sops_config_path
from toolkit.core.manifest.routes import compile_routes, route_scope

_ARCHIVE_ROOT_FILES = frozenset({"config.yaml", "secrets.enc.yaml", ".sops.yaml", "DEPLOY_INSTRUCTIONS.md"})
_ARCHIVE_STACK_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}\.ya?ml$")
_MAX_ARCHIVE_FILES = 256
_MAX_ARCHIVE_FILE_BYTES = 4 * 1024 * 1024
_MAX_ARCHIVE_TOTAL_BYTES = 32 * 1024 * 1024


@click.group()
def config():
    """Configuration management."""
    pass


@config.command()
@click.pass_context
def show(ctx: click.Context):
    """Display current configuration."""
    root = Path(ctx.obj["root"])
    cfg = load_config(config_path(root))
    public = redact_sensitive_config(cfg.model_dump(mode="json"))
    click.echo(yaml.dump(public, default_flow_style=False, sort_keys=False))


@config.command()
@click.option(
    "--preset",
    help="Apply all, minimal, or an installed category ID",
)
@click.option("--yes", "-y", is_flag=True, help="Overwrite existing config.yaml")
@click.pass_context
def init(ctx, preset, yes):
    """Create default config.yaml (optionally from a preset)."""
    root = Path(ctx.obj["root"])
    from toolkit.core.bootstrap.runtime_assets import ensure_runtime_assets

    path = config_path(root)
    from toolkit.cli.config_mutation import cli_configuration_mutation

    with cli_configuration_mutation(root, "config-init"):
        if path.exists() and not yes:
            click.echo(f"Config already exists: {path}")
            return
        if preset:
            from toolkit.cli.install_cmd import _preset_config

            cfg = _preset_config(preset)
            click.echo(f"Applied preset '{preset}' → {path}")
            click.echo(f"  Categories: {', '.join(cfg.enabled_categories)}")
            click.echo(f"  Nodes: {', '.join(cfg.enabled_nodes)}")
        else:
            cfg = Config()
        save_config(cfg, path)
        ensure_runtime_assets(root)
    click.echo(f"Created: {path}")


@config.command()
@click.pass_context
def validate(ctx: click.Context):
    """Validate config.yaml against schema."""
    root = Path(ctx.obj["root"])
    try:
        cfg = load_config(config_path(root))
        nodes = ", ".join(cfg.enabled_nodes)
        click.echo(f"Valid. Domain: {cfg.domain}, Nodes: {nodes}, Categories: {len(cfg.enabled_categories)}")
    except Exception as e:
        raise click.ClickException(f"Invalid: {e}") from e


@config.command("set")
@click.argument("key_value", nargs=-1)
@click.pass_context
def config_set(ctx, key_value):
    """Set config values (key=value pairs)."""
    root = Path(ctx.obj["root"])
    cp = config_path(root)
    if not cp.exists():
        raise click.ClickException("No config.yaml. Run: homelab-toolkit config init")
    if not key_value:
        raise click.ClickException("Provide at least one dotted key=value update")

    from toolkit.cli.config_mutation import cli_configuration_mutation

    rendered: list[tuple[str, object]] = []
    with cli_configuration_mutation(root, "config-set"):
        raw = load_config(cp).model_dump(mode="python")
        for item in key_value:
            if "=" not in item:
                raise click.ClickException(f"Invalid format: {item} (expected key=value)")
            key, serialized = item.split("=", 1)
            parts = key.split(".")
            if any(not part or part.startswith("_") for part in parts):
                raise click.ClickException(f"Invalid configuration path: {key}")
            value = yaml.safe_load(serialized)
            target = raw
            for part in parts[:-1]:
                existing = target.get(part)
                if existing is None:
                    existing = {}
                    target[part] = existing
                if not isinstance(existing, dict):
                    raise click.ClickException(f"Configuration path is not a mapping: {'.'.join(parts[:-1])}")
                target = existing
            target[parts[-1]] = value
            rendered.append((key, value))

        try:
            updated = Config.model_validate(raw)
        except ValueError as exc:
            raise click.ClickException(f"Invalid configuration update: {exc}") from exc
        save_config(updated, cp)
        save_local_config(updated, root)

    for key, value in rendered:
        display = "<redacted>" if config_path_is_sensitive(key) else value
        click.echo(f"  {key} = {display}")
    click.echo("Config updated. Run 'generate' to apply.")


@config.command("edit")
@click.pass_context
def config_edit(ctx):
    """Edit and atomically validate config.yaml."""
    root = Path(ctx.obj["root"])
    cp = config_path(root)
    if not cp.exists():
        click.echo("No config.yaml. Run: config init")
        return

    editor = os.environ.get("EDITOR", "nano")
    from toolkit.cli.config_mutation import cli_configuration_mutation
    from toolkit.core.config.mutations import config_revision

    revision = config_revision(root)
    original = cp.read_text(encoding="utf-8")
    edited = click.edit(text=original, editor=editor, extension=".yaml")
    if edited is None or edited == original:
        click.echo("Config unchanged.")
        return
    try:
        candidate = Config.model_validate(yaml.safe_load(edited) or {})
    except (ValueError, yaml.YAMLError) as exc:
        raise click.ClickException(f"Invalid configuration; no changes saved: {exc}") from exc
    with cli_configuration_mutation(root, "config-edit"):
        if config_revision(root) != revision:
            raise click.ClickException("Configuration changed while the editor was open; no changes saved")
        save_config(candidate, cp)
    click.echo("Config updated. Run 'generate' to apply.")


@config.command("rollback")
@click.pass_context
def config_rollback(ctx):
    """Restore .env files from last backup."""
    root = Path(ctx.obj["root"])
    from toolkit.core.config.storage import env_path

    restored = 0
    cfg = load_config(config_path(root))
    for vm in cfg.enabled_nodes:
        ef = env_path(vm, root)
        from toolkit.core.config.storage import backup_path

        backup = backup_path(ef)
        if backup.exists():
            backup.replace(ef)
            click.echo(f"  Restored {vm}/.env from backup")
            restored += 1

    if restored:
        click.echo(f"\nRestored {restored} .env files. Restart services to apply.")
    else:
        click.echo("No backups found.")


@config.command("exposure")
@click.option("--markdown", "as_md", is_flag=True, help="Print recommendation markdown")
@click.pass_context
def config_exposure(ctx: click.Context, as_md: bool):
    """Report compiled public/private route and authentication policy."""
    root = Path(ctx.obj["root"])
    cfg = load_config(config_path(root))
    routes = compile_routes(cfg)
    if as_md:
        click.echo("| Service | URL | Exposure | Auth | Scope |")
        click.echo("|---|---|---|---|---|")
        for route in routes:
            click.echo(
                f"| `{route.service}` | `https://{route.host}` | {route.exposure} | "
                f"{route.auth.mode} | {route_scope(route)} |"
            )
        return
    click.echo(f"{'Service':<24} {'Exposure':<9} {'Auth':<14} {'Scope':<42} URL")
    click.echo("-" * 120)
    for route in routes:
        click.echo(
            f"{route.service:<24} {route.exposure:<9} {route.auth.mode:<14} "
            f"{route_scope(route):<42} https://{route.host}"
        )


@config.command("export")
@click.option(
    "--output",
    "-o",
    type=click.Path(dir_okay=False),
    default="homelab-backup.tar.gz",
    help="Output archive path (default: homelab-backup.tar.gz)",
)
@click.pass_context
def config_export(ctx, output):
    """Export config.yaml, secrets, and service definitions to a tar.gz archive."""
    root = Path(ctx.obj["root"])
    output_path = Path(output).resolve()

    files_to_export: list[tuple[str, str | bytes, bool]] = []

    # Core config files
    cp = config_path(root)
    if cp.exists():
        files_to_export.append(("config.yaml", cp.read_text(), False))
        click.echo("  ✓ config.yaml")
    else:
        click.echo("  ○ config.yaml not found — skipping")

    sp = secrets_path(root)
    if sp.exists():
        files_to_export.append(("secrets.enc.yaml", sp.read_text(), False))
        click.echo("  ✓ secrets.enc.yaml")
    else:
        click.echo("  ○ secrets.enc.yaml not found — skipping")

    sops = sops_config_path(root)
    if sops.exists():
        files_to_export.append((".sops.yaml", sops.read_text(), False))
        click.echo("  ✓ .sops.yaml")

    # Compose fragment source-of-truth (stacks/)
    stacks_dir = root / "stacks"
    if stacks_dir.is_dir():
        for frag in sorted(stacks_dir.iterdir()):
            if frag.is_file() and frag.suffix in (".yaml", ".yml"):
                rel = f"stacks/{frag.name}"
                files_to_export.append((rel, frag.read_text(), False))
                click.echo(f"  ✓ {rel}")

    # Deployment instructions
    deploy_instructions = (
        "# Homelab Toolkit — Deploy Instructions (auto-generated by config export)\n"
        f"# Exported from: {root}\n"
        f"# Date: {__import__('datetime').datetime.now().isoformat()}\n"
        "\n"
        "## How to restore\n"
        "1. Import this archive through the bounded toolkit importer:\n"
        f"     homelab-toolkit --root {DEFAULT_HOMELAB_ROOT} config import "
        "--input homelab-backup.tar.gz --yes\n"
        "2. Verify the config:\n"
        "     homelab-toolkit config validate\n"
        "3. Generate artifacts:\n"
        "     homelab-toolkit generate\n"
        "4. Deploy:\n"
        "     homelab-toolkit deploy all --dry-run   # review plan\n"
        "     homelab-toolkit deploy all -y           # execute\n"
        "\n"
        "## Files included\n"
    )
    for fpath, _content, _binary in files_to_export:
        deploy_instructions += f"  - {fpath}\n"
    deploy_instructions += "\n## Prerequisites\n"
    deploy_instructions += (
        "- Proxmox API token with appropriate permissions\n"
        "- Cloudflare API token for DNS\n"
        "- SSH keys configured in config.local.yaml\n"
        "- Python 3.11+ with homelab-toolkit installed\n"
    )

    files_to_export.append(("DEPLOY_INSTRUCTIONS.md", deploy_instructions, False))

    try:
        with tarfile.open(output_path, "w:gz") as tar:
            for arcname, content, is_binary in files_to_export:
                if is_binary and isinstance(content, str):
                    content = content.encode("utf-8")
                elif not is_binary and isinstance(content, bytes):
                    content = content.decode("utf-8")
                data = content if isinstance(content, bytes) else content.encode("utf-8")
                info = tarfile.TarInfo(name=arcname)
                info.size = len(data)
                tar.addfile(info, io.BytesIO(data))
        click.echo(f"\n✓ Export complete: {output_path}")
        click.echo(f"  Files: {len(files_to_export)}")
    except (OSError, PermissionError) as exc:
        raise click.ClickException(f"Export failed: {exc}") from exc


@config.command("import")
@click.option(
    "--input",
    "-i",
    "input_path",
    type=click.Path(dir_okay=False, exists=True),
    required=True,
    help="Archive path to import (tar.gz)",
)
@click.option("--yes", "-y", is_flag=True, help="Overwrite existing files without prompting")
@click.pass_context
def config_import(ctx, input_path, yes):
    """Import a bounded toolkit archive and validate it before writing."""
    root = Path(ctx.obj["root"]).resolve()
    archive_path = Path(input_path).resolve()

    if not tarfile.is_tarfile(archive_path):
        raise click.ClickException(f"Not a valid tar archive: {archive_path}")

    click.echo(f"Importing from: {archive_path}")
    click.echo(f"Target root: {root}")

    with tarfile.open(archive_path, "r:gz") as tar:
        files: dict[str, bytes] = {}
        total_size = 0
        for index, member in enumerate(tar):
            if index >= _MAX_ARCHIVE_FILES:
                raise click.ClickException(f"Archive contains more than {_MAX_ARCHIVE_FILES} entries")
            if member.isdir():
                continue
            if not member.isfile():
                raise click.ClickException(f"Unsafe archive member type: {member.name!r}")

            member_path = PurePosixPath(member.name)
            parts = member_path.parts
            if member_path.is_absolute() or not parts or any(part in {"", ".", ".."} or "\\" in part for part in parts):
                raise click.ClickException(f"Unsafe archive member path: {member.name!r}")
            canonical = member_path.as_posix()
            supported = canonical in _ARCHIVE_ROOT_FILES or (
                len(parts) == 2 and parts[0] == "stacks" and _ARCHIVE_STACK_NAME.fullmatch(parts[1]) is not None
            )
            if not supported:
                raise click.ClickException(f"Unsupported archive member: {canonical!r}")
            if canonical in files:
                raise click.ClickException(f"Duplicate archive member: {canonical!r}")
            if member.size < 0 or member.size > _MAX_ARCHIVE_FILE_BYTES:
                raise click.ClickException(f"Archive member is too large: {canonical!r}")
            total_size += member.size
            if total_size > _MAX_ARCHIVE_TOTAL_BYTES:
                raise click.ClickException("Archive expanded content exceeds the 32 MiB limit")

            stream = tar.extractfile(member)
            if stream is None:
                raise click.ClickException(f"Could not read archive member: {canonical!r}")
            content = stream.read(_MAX_ARCHIVE_FILE_BYTES + 1)
            if len(content) != member.size or len(content) > _MAX_ARCHIVE_FILE_BYTES:
                raise click.ClickException(f"Archive member size is invalid: {canonical!r}")
            files[canonical] = content

    for name, content in files.items():
        if name == "DEPLOY_INSTRUCTIONS.md":
            continue
        try:
            parsed = yaml.safe_load(content.decode("utf-8"))
        except (UnicodeDecodeError, yaml.YAMLError) as exc:
            raise click.ClickException(f"Invalid YAML in archive member {name!r}: {exc}") from exc
        if name == "config.yaml":
            try:
                Config.model_validate(parsed or {})
            except Exception as exc:
                raise click.ClickException(f"Invalid configuration in archive: {exc}") from exc
        elif not isinstance(parsed, dict):
            raise click.ClickException(f"Invalid YAML document in archive member {name!r}: expected a mapping")

    from toolkit.cli.config_mutation import cli_configuration_mutation

    pending: list[tuple[str, Path, bytes]] = []
    with cli_configuration_mutation(root, "config-import"):
        for name, content in files.items():
            target_path = root.joinpath(*PurePosixPath(name).parts)
            parent = target_path.parent
            while parent != root:
                if parent.is_symlink():
                    raise click.ClickException(f"Refusing symbolic link in import destination: {parent}")
                parent = parent.parent
            if target_path.is_symlink():
                raise click.ClickException(f"Refusing symbolic link import destination: {target_path}")
            if target_path.exists() and not target_path.is_file():
                raise click.ClickException(f"Import destination is not a regular file: {target_path}")
            if target_path.exists() and not yes:
                click.echo(f"  ? {name} exists — use --yes to overwrite")
                continue
            pending.append((name, target_path, content))

        for name, target_path, content in pending:
            target_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            descriptor, temporary_name = tempfile.mkstemp(prefix=f".{target_path.name}.", dir=target_path.parent)
            temporary = Path(temporary_name)
            try:
                mode = 0o600 if name == "secrets.enc.yaml" else 0o644
                os.fchmod(descriptor, mode)
                with os.fdopen(descriptor, "wb", closefd=True) as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                temporary.replace(target_path)
            finally:
                temporary.unlink(missing_ok=True)
            click.echo(f"  ✓ {name}")

    if not pending:
        click.echo("No files imported (all exist, use --yes to overwrite).")
        return

    click.echo(f"\nImported {len(pending)} file(s).")

    # Validate the imported config
    cp = root / "config.yaml"
    if cp.exists():
        click.echo("\nValidating imported config...")
        try:
            cfg = load_config(cp)
            nodes = ", ".join(cfg.enabled_nodes)
            click.echo(f"  ✓ Valid. Domain: {cfg.domain}, Nodes: {nodes}, Categories: {len(cfg.enabled_categories)}")
        except Exception as e:
            raise click.ClickException(
                f"Validation error: {e}. Fix config.yaml and re-validate: homelab-toolkit config validate"
            ) from e
