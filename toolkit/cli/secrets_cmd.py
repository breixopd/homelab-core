from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import click

from toolkit.core.config.config import load_config
from toolkit.core.config.storage import config_path, secrets_path
from toolkit.core.ops.vpn import SUPPORTED_PROVIDERS, apply_nordvpn_secrets, filter_vpn_specs
from toolkit.core.secrets.secrets import (
    SecretTier,
    generate_all_secrets,
    get_required_secrets,
    load_secrets_plaintext,
    save_secrets_plaintext,
    secret_storage_mode,
)


@click.group()
def secrets():
    """Secret management."""
    pass


@contextmanager
def _secret_mutation_lease(root: Path, operation: str):
    from toolkit.core.deploy.operation_lease import LeaseBusyError, OperationLease

    try:
        lease = OperationLease.acquire(root, operation)
    except LeaseBusyError as exc:
        raise click.ClickException("Another deployment or mutation is already running") from exc
    try:
        yield
    finally:
        lease.release()


def _validate_secret_value(name: str, value: str) -> str:
    if not value:
        raise click.ClickException("Secret value cannot be empty; use secrets unset to remove it")
    maximum = 65_536 if name == "PROXMOX_HOST_SSH_KEY" else 4_096
    if len(value) > maximum:
        raise click.ClickException(f"Secret value exceeds the {maximum}-character limit")
    if name != "PROXMOX_HOST_SSH_KEY" and any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise click.ClickException("Secret value contains unsupported control characters")
    return value


@secrets.command()
@click.pass_context
def show(ctx: click.Context):
    """Show which secrets are set/unset."""
    root = Path(ctx.obj["root"])
    cfg = load_config(config_path(root))
    existing = load_secrets_plaintext(secrets_path(root))
    specs = get_required_secrets(cfg)
    specs = filter_vpn_specs(specs, existing)

    click.echo(f"Storage: {secret_storage_mode(secrets_path(root))}")

    for spec in specs:
        configured = bool(existing.get(spec.name)) or (
            spec.tier == SecretTier.DERIVED and bool(existing.get("SSO_USER_PASSWORD"))
        )
        status = "set" if configured else "UNSET"
        tier = {
            SecretTier.GENERATED: "auto",
            SecretTier.DERIVED: "derived",
            SecretTier.USER: "user",
        }[spec.tier]
        click.echo(f"  {spec.name:40s} [{tier:4s}] {status}")


@secrets.command("generate")
@click.pass_context
def generate_secrets(ctx: click.Context):
    """Generate auto-generated secrets (preserves existing). Auto-initializes SOPS if needed."""
    root = Path(ctx.obj["root"])
    from toolkit.core.secrets.secrets import ensure_sops_ready

    cfg = load_config(config_path(root))
    with _secret_mutation_lease(root, "secret-generate"):
        ensure_sops_ready(root)
        existing = load_secrets_plaintext(secrets_path(root))
        if cfg.owner_password:
            existing["SSO_USER_PASSWORD"] = cfg.owner_password
        specs = get_required_secrets(cfg)
        result = generate_all_secrets(specs, existing)

        new_count = sum(1 for k in result if k not in existing or not existing[k])
        save_secrets_plaintext(result, secrets_path(root))
    click.echo(
        f"Generated {new_count} new secrets. Total: {len(result)}. Storage: {secret_storage_mode(secrets_path(root))}"
    )


@secrets.command("set")
@click.argument("name")
@click.argument("value", required=False)
@click.pass_context
def set_secret(ctx: click.Context, name: str, value: str | None):
    """Set a single secret (prompts securely when VALUE is omitted)."""
    root = Path(ctx.obj["root"])
    from toolkit.core.secrets.secrets import ensure_sops_ready

    cfg = load_config(config_path(root))
    specs = {spec.name: spec for spec in get_required_secrets(cfg)}
    if name not in specs:
        raise click.ClickException(f"Unknown secret {name!r}; declare it in a service manifest first")

    if value is None:
        value = click.prompt(f"Value for {name}", hide_input=True)
    value = _validate_secret_value(name, value)

    with _secret_mutation_lease(root, "secret-update"):
        ensure_sops_ready(root)
        existing = load_secrets_plaintext(secrets_path(root))
        action = "Updated" if existing.get(name) else "Set"
        existing[name] = value
        save_secrets_plaintext(existing, secrets_path(root))
    click.echo(f"{action} {name}. Run 'homelab-toolkit generate' to refresh .env files.")


@secrets.command("unset")
@click.argument("name")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.pass_context
def unset_secret(ctx: click.Context, name: str, yes: bool):
    """Remove a secret from the store."""
    root = Path(ctx.obj["root"])
    before_prompt = load_secrets_plaintext(secrets_path(root))
    if name not in before_prompt:
        click.echo(f"{name} is not set.")
        return
    if not yes:
        click.confirm(f"Remove {name}?", abort=True)
    with _secret_mutation_lease(root, "secret-update"):
        existing = load_secrets_plaintext(secrets_path(root))
        if name not in existing:
            click.echo(f"{name} is not set.")
            return
        del existing[name]
        save_secrets_plaintext(existing, secrets_path(root))
    click.echo(f"Removed {name}.")


@secrets.command("rotate")
@click.option("--name", "-n", multiple=True, help="Specific secret(s) to rotate")
@click.option("--all", "rotate_all", is_flag=True, help="Rotate all auto-generated secrets")
@click.option("--yes", "-y", is_flag=True, help="Skip confirmation prompt")
@click.option("--apply", is_flag=True, help="Take a safety dump, rotate, then run the full deploy and verify workflow")
@click.option("--dry-run", is_flag=True, help="Show which services would restart without applying")
@click.pass_context
def rotate(ctx, name, rotate_all, yes, apply, dry_run):
    """Rotate (regenerate) secrets.

    With ``--apply``: takes a database safety dump while the current
    credentials still work, rotates the selected values, then runs the same
    full deployment and verification workflow as ``deploy all``. A failed
    rotation restores the previous encrypted state and redeploys it.
    """
    import time as _time

    root = Path(ctx.obj["root"])

    cfg = load_config(config_path(root))
    specific = list(name) if name else None
    if not rotate_all and not specific:
        click.echo("Specify --name or --all")
        return

    if not yes and not dry_run:
        target = "all auto-generated secrets" if rotate_all else f"secrets: {', '.join(specific)}"
        suffix = " then generate + redeploy + verify" if apply else ""
        click.confirm(f"Rotate {target}{suffix}? This will regenerate them and require service restarts.", abort=True)

    if dry_run:
        click.echo("Dry-run: would rotate:")
        if rotate_all:
            click.echo("  all auto-generated secrets")
        else:
            for s in specific or []:
                click.echo(f"  {s}")
        click.echo("\nWould then: pre-rotation dump → rotate → full deploy → hooks → verify")
        return

    from toolkit.core.deploy.operation_lease import LeaseBusyError, OperationLease

    try:
        lease = OperationLease.acquire(root, "secret-rotation")
    except LeaseBusyError as exc:
        raise click.ClickException("Another deployment or mutation is already running") from exc
    try:
        _rotate_under_lease(ctx, root, cfg, specific, apply, _time.monotonic(), lease)
    finally:
        lease.release()


def _rotate_under_lease(ctx, root, cfg, specific, apply, t0, lease) -> None:
    """Rotate and, when requested, deploy while retaining one operation lease."""
    import time as _time

    from toolkit.core.secrets.secrets import rotate_secrets
    from toolkit.core.state.audit_log import AuditAction, audit

    previous_secrets = load_secrets_plaintext(secrets_path(root)) if apply else {}
    dump_path = None
    if apply:
        click.echo("\n→ Creating a pre-rotation database safety dump...")
        try:
            from toolkit.core.ops.db_safety import pre_deploy_dump

            dump_path = pre_deploy_dump(cfg, root)
            if dump_path:
                click.secho(f"  ✓ Dump: {dump_path}", fg="green")
            else:
                click.secho("  ⚠ Dump skipped (database provider not ready)", fg="yellow")
        except Exception as exc:
            click.secho(f"  ⚠ Dump failed (non-fatal): {exc}", fg="yellow")

    try:
        rotated = rotate_secrets(root, specific)
    except BaseException:
        if apply:
            save_secrets_plaintext(previous_secrets, secrets_path(root))
        raise
    previous_values = {name: previous_secrets.get(name) for name in rotated}
    click.echo(f"Rotated {len(rotated)} secrets:")
    for s in sorted(rotated):
        click.echo(f"  {s}")

    if not apply:
        click.echo("\nRun 'generate' to update .env files, then restart services.")
        return

    click.echo("\n→ Applying rotation through the full deployment workflow...")
    from toolkit.core.secrets.rotation_context import previous_secret_context

    try:
        with previous_secret_context(root, previous_values):
            deployment_ok = _invoke_rotation_deployment(ctx, operation_lease=lease)
    except BaseException:
        from toolkit.controller.settings_api import restore_secret_values

        with lease.shield_cancellation():
            restore_secret_values(root, previous_values, rotated)
            try:
                with previous_secret_context(root, rotated):
                    _invoke_rotation_deployment(ctx, operation_lease=lease)
            except BaseException:
                pass
        raise
    if not deployment_ok:
        click.secho("\n⚠ Rotation did not converge; restoring the previous encrypted secret state...", fg="yellow")
        from toolkit.controller.settings_api import restore_secret_values

        restore_secret_values(root, previous_values, rotated)
        with previous_secret_context(root, rotated):
            rollback_ok = _invoke_rotation_deployment(ctx, operation_lease=lease)
        audit(
            root,
            AuditAction.SECRET_ROTATE,
            actor="cli",
            ok=False,
            detail="rotation failed; previous secret state restored"
            + (" and redeployed" if rollback_ok else "; rollback deployment failed"),
            duration_s=round(_time.monotonic() - t0, 1),
            extra={
                "rotated": sorted(rotated)[:20],
                "rollback_deployed": rollback_ok,
                "dump": str(dump_path) if dump_path else None,
            },
        )
        if rollback_ok:
            click.secho("  ✓ Previous credentials restored and redeployed", fg="green")
        else:
            click.secho("  ✗ Previous credentials were restored, but rollback deployment failed", fg="red")
        ctx.exit(1)

    audit(
        root,
        AuditAction.SECRET_ROTATE,
        actor="cli",
        ok=True,
        detail=f"rotated and verified {len(rotated)} secret(s)",
        duration_s=round(_time.monotonic() - t0, 1),
        extra={
            "rotated": sorted(rotated)[:20],
            "dump": str(dump_path) if dump_path else None,
        },
    )
    click.secho(
        f"\n✓ Secret rotation E2E complete ({len(rotated)} secrets, all VMs redeployed, verify passed)",
        fg="green",
    )


def _invoke_rotation_deployment(ctx: click.Context, *, operation_lease=None) -> bool:
    """Run the canonical deploy pipeline and translate Click exits to a result."""
    from toolkit.cli.deploy_cmd import deploy_all

    arguments = {
        "as_json": False,
        "skip_infra": False,
        "skip_dns": False,
        "destroy_first": False,
        "yes": True,
        "log_file": None,
        "vm": None,
        "dry_run": False,
    }
    if operation_lease is not None:
        arguments["operation_lease"] = operation_lease
    try:
        ctx.invoke(deploy_all, **arguments)
    except click.exceptions.Exit as exc:
        return exc.exit_code == 0
    except Exception:
        return False
    return True


@secrets.command("export")
@click.option("--format", "fmt", type=click.Choice(["env", "json"]), default="env")
@click.pass_context
def export_secrets(ctx, fmt):
    """Export secrets to stdout."""
    root = Path(ctx.obj["root"])
    import json as json_mod

    secrets_data = load_secrets_plaintext(secrets_path(root))
    if not secrets_data:
        click.echo("No secrets found. Run 'secrets generate' first.")
        return

    if fmt == "json":
        click.echo(json_mod.dumps(secrets_data, indent=2))
    else:
        for key in sorted(secrets_data):
            val = secrets_data[key]
            if isinstance(val, str) and "\n" in val:
                click.echo(f"# SKIPPED {key}: multiline value (not compatible with .env format)")
                continue
            click.echo(f"{key}={val}")


@secrets.command("configure-vpn")
@click.option("--provider", type=click.Choice([*SUPPORTED_PROVIDERS], case_sensitive=False), default="nordvpn")
@click.pass_context
def configure_vpn(ctx: click.Context, provider: str):
    """Configure gluetun VPN credentials (NordVPN: access token only)."""
    root = Path(ctx.obj["root"])
    from toolkit.core.secrets.secrets import ensure_sops_ready

    provider = provider.strip().lower()
    existing = load_secrets_plaintext(secrets_path(root))
    if provider == "nordvpn":
        token = click.prompt(
            "NordVPN access token (Manual setup → Generate new token)",
            hide_input=True,
            default=existing.get("NORDVPN_TOKEN") or None,
            show_default=False,
        )
        token = _validate_secret_value("NORDVPN_TOKEN", token)
        with _secret_mutation_lease(root, "secret-configure-vpn"):
            ensure_sops_ready(root)
            current = load_secrets_plaintext(secrets_path(root))
            save_secrets_plaintext(apply_nordvpn_secrets(current, token), secrets_path(root))
        click.echo("Saved NordVPN token (WireGuard). Run 'homelab-toolkit generate' then redeploy media.")
        return

    updates = {"VPN_PROVIDER": provider}
    if provider == "custom":
        updates["VPN_TYPE"] = click.prompt("VPN type", type=click.Choice(["wireguard", "openvpn"]), default="wireguard")
        if updates["VPN_TYPE"] == "wireguard":
            updates["WIREGUARD_PRIVATE_KEY"] = click.prompt(
                "WireGuard private key",
                hide_input=True,
                default=existing.get("WIREGUARD_PRIVATE_KEY") or None,
                show_default=False,
            )
            updates["WIREGUARD_ADDRESSES"] = click.prompt(
                "WireGuard addresses",
                default=existing.get("WIREGUARD_ADDRESSES") or "10.2.0.2/32",
            )
    else:
        updates["VPN_TYPE"] = click.prompt(
            "VPN type",
            type=click.Choice(["wireguard", "openvpn"]),
            default=existing.get("VPN_TYPE") or "wireguard",
        )
        updates["VPN_USER"] = click.prompt(
            "VPN username",
            default=existing.get("VPN_USER") or None,
            show_default=bool(existing.get("VPN_USER")),
        )
        updates["VPN_PASSWORD"] = click.prompt(
            "VPN password",
            hide_input=True,
            default=existing.get("VPN_PASSWORD") or None,
            show_default=False,
        )
    updates = {name: _validate_secret_value(name, value) for name, value in updates.items()}
    with _secret_mutation_lease(root, "secret-configure-vpn"):
        ensure_sops_ready(root)
        current = load_secrets_plaintext(secrets_path(root))
        current.update(updates)
        current.pop("NORDVPN_TOKEN", None)
        save_secrets_plaintext(current, secrets_path(root))
    click.echo(f"Saved {provider} VPN credentials. Run 'homelab-toolkit generate' then redeploy media.")


@secrets.command("init")
@click.pass_context
def init_sops_cmd(ctx):
    """Initialize SOPS encryption (generate age key + .sops.yaml)."""
    from toolkit.core.secrets.secrets import init_sops

    root = Path(ctx.obj["root"])
    try:
        with _secret_mutation_lease(root, "secret-init-sops"):
            pubkey = init_sops(root)
        click.echo("SOPS initialized.")
        click.echo(f"  Public key: {pubkey}")
        click.echo(f"  Key file:   {root}/keys/age.key")
        click.echo(f"  SOPS config: {root}/.sops.yaml")
        click.echo("\nKeep keys/age.key safe — it's your only decryption key!")
    except FileNotFoundError:
        click.echo("age-keygen not found. Install age: https://github.com/FiloSottile/age")
    except RuntimeError as e:
        click.echo(f"Error: {e}")
