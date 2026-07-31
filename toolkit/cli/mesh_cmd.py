from __future__ import annotations

import importlib as _importlib
import os
import re
import subprocess
from pathlib import Path

import click

from toolkit.cli import load_root_config
from toolkit.core.registry.mesh import mesh_lan_cidr, personal_mesh_up_args

_headscale_bootstrap = _importlib.import_module("toolkit.services.headscale.bootstrap")
_headscale_mesh = _importlib.import_module("toolkit.services.headscale.mesh")
approve_mesh_registration = _headscale_bootstrap.approve_mesh_registration
personal_headscale_username = _headscale_bootstrap.personal_headscale_username
headscale_preauth_key_for_deploy = _headscale_bootstrap.headscale_preauth_key_for_deploy
ensure_controller_mesh_joined = _headscale_bootstrap.ensure_controller_mesh_joined
headscale_control_state_verified = _headscale_bootstrap.headscale_control_state_verified
bootstrap_infra_subnet_router = _headscale_mesh.bootstrap_infra_subnet_router
mesh_internal_hosts = _headscale_mesh.mesh_internal_hosts
probe_mesh_internal = _headscale_mesh.probe_mesh_internal

_REGISTER_KEY_RE = re.compile(r"(?:[?&]key=|/register/)([A-Za-z0-9_:-]+)")


def _extract_registration_key(text: str) -> str:
    match = _REGISTER_KEY_RE.search(text or "")
    return match.group(1) if match else ""


def _redact_registration_keys(text: str) -> str:
    return _REGISTER_KEY_RE.sub(lambda match: match.group(0).replace(match.group(1), "<REDACTED>"), text)


def _safe_mesh_log(line: str, *secrets: str | None) -> str:
    """Redact bearer material and keep recovery hints on real commands."""
    safe = _redact_registration_keys(line).replace("mesh join-cmd", "mesh join")
    for secret in secrets:
        if secret:
            safe = safe.replace(secret, "<REDACTED>")
    return safe


def _print_personal_join_help(cfg) -> None:
    click.echo("")
    click.echo("Headscale registration page:")
    click.echo(f"  1. Prefer OIDC: on vpn.{cfg.domain}, use Sign in / OpenID (Authelia) — node auto-registers.")
    click.echo("  2. Manual approve: homelab-toolkit mesh approve --key <KEY from page>")
    click.echo(f"     (default user: {personal_headscale_username(cfg)})")
    click.echo("After join: accept subnet routes in Tailscale, then `homelab-toolkit mesh doctor`.")


@click.group()
def mesh():
    """Headscale mesh — join devices, reach internal LAN, onboard fleet."""
    pass


@mesh.command("join")
@click.option("--hostname", default="homelab-controller", show_default=True)
@click.option("--fleet", is_flag=True, help="Join with tagged fleet preauth key (VPS/NAS)")
@click.option("--approve-key", default=None, help="Registration key to approve after tailscale up (personal join)")
@click.option("--dry-run", "--print-only", is_flag=True, help="Print command only")
@click.pass_context
def join(ctx: click.Context, hostname: str, fleet: bool, approve_key: str | None, dry_run: bool):
    """Join this machine to Headscale (personal OIDC by default)."""
    root, cfg = load_root_config(ctx)
    preauth_key: str | None = None
    if fleet:
        tags = list(cfg.fleet.headscale_tags or ["tag:fleet-external"])
        if not dry_run:
            preauth_key = headscale_preauth_key_for_deploy(cfg, Path(root), tags=tags)
            if not preauth_key:
                raise SystemExit("Could not create Headscale preauth key")
        login = f"https://vpn.{cfg.domain}"
        cmd = [
            "tailscale",
            "up",
            f"--login-server={login}",
            f"--auth-key={preauth_key or '<REDACTED>'}",
            f"--hostname={hostname}",
            "--accept-routes",
            "--reset",
        ]
    else:
        cmd = personal_mesh_up_args(cfg, hostname=hostname)

    if dry_run:
        safe_cmd = ["--auth-key=<REDACTED>" if arg.startswith("--auth-key=") else arg for arg in cmd]
        click.echo(" ".join(safe_cmd))
        return

    os.environ["HOMELAB_JOIN_CONTROLLER_MESH"] = "1"
    if fleet:
        logs = ensure_controller_mesh_joined(
            cfg,
            preauth_key=preauth_key,
            root=Path(root),
            fleet=True,
            hostname=hostname,
        )
        for line in logs:
            click.echo(_safe_mesh_log(line, preauth_key))
        if not any("controller mesh active" in line or "fleet node joined mesh" in line for line in logs):
            raise SystemExit(1)
        return

    try:
        up = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    except FileNotFoundError:
        click.echo("Tailscale CLI is unavailable; install Tailscale, then retry `homelab-toolkit mesh join`.")
        raise SystemExit(1) from None
    except subprocess.TimeoutExpired:
        click.echo("Tailscale join timed out; no registration was approved.")
        raise SystemExit(1) from None
    combined = f"{up.stdout or ''}\n{up.stderr or ''}"
    if up.returncode == 0:
        if headscale_control_state_verified(f"https://vpn.{cfg.domain}"):
            _mesh_post_join_hints()
            return
        click.echo("Tailscale returned success, but Headscale control state was not verified.")
        raise SystemExit(1)
    reg_key = (approve_key or _extract_registration_key(combined)).strip()
    if up.returncode != 0 and "needs login" not in combined.lower():
        safe_combined = combined.replace(reg_key, "<REDACTED>") if reg_key else combined
        click.echo(_redact_registration_keys(safe_combined).strip()[:300])

    if reg_key:
        approve_logs = approve_mesh_registration(cfg, Path(root), key=reg_key)
        for line in approve_logs:
            click.echo(_safe_mesh_log(line, reg_key))
        if any("registered node" in line for line in approve_logs):
            _mesh_post_join_hints()
            return

    _print_personal_join_help(cfg)
    raise SystemExit(1)


@mesh.command("approve")
@click.option("--key", required=True, help="Registration key from the Headscale web page")
@click.option("--user", default=None, help="Headscale username (default: owner email local-part)")
@click.pass_context
def approve(ctx: click.Context, key: str, user: str | None):
    """Approve a pending mesh registration on infra (when OIDC auto-register did not complete)."""
    root, cfg = load_root_config(ctx)
    logs = approve_mesh_registration(cfg, Path(root), key=key, user=user)
    for line in logs:
        click.echo(_safe_mesh_log(line, key))
    if not any("registered node" in line for line in logs):
        raise SystemExit(1)


def _mesh_post_join_hints() -> None:
    click.echo("\nMesh join complete.")
    click.echo("  Accept routes: tailscale set --accept-routes")
    click.echo("  Verify:        homelab-toolkit mesh doctor")
    import getpass
    import os

    user = os.environ.get("USER") or getpass.getuser()
    click.echo(f"  Optional (passwordless tailscale CLI): sudo tailscale set --operator={user}")


@mesh.command("router")
@click.pass_context
def router(ctx: click.Context):
    """Ensure infra advertises the homelab LAN to mesh (automated subnet router)."""
    root, cfg = load_root_config(ctx)
    logs = bootstrap_infra_subnet_router(cfg, Path(root))
    for line in logs:
        click.echo(line)
    if not any("advertising" in line or "already advertising" in line for line in logs):
        raise SystemExit(1)


@mesh.command("doctor")
@click.pass_context
def doctor(ctx: click.Context):
    """Check mesh join + internal LAN reachability."""
    root, cfg = load_root_config(ctx)
    cidr = mesh_lan_cidr(cfg)
    click.echo(f"Homelab LAN: {cidr} (advertised from infra when router is up)")
    click.echo("Internal targets:")
    for name, ip in mesh_internal_hosts(cfg):
        click.echo(f"  {name:12} {ip}")
    proc = subprocess.run(["tailscale", "status"], capture_output=True, text=True, timeout=15, check=False)
    if proc.returncode == 0:
        click.echo("\nTailscale status:")
        click.echo(proc.stdout.strip() or "(empty)")
    else:
        click.echo("\nNot on mesh — run: homelab-toolkit mesh join")
    click.echo("\nProbes:")
    for label, ok, detail in probe_mesh_internal(cfg):
        mark = "ok" if ok else "FAIL"
        click.echo(f"  [{mark}] {label}: {detail}")
    if not cfg.is_multi_node:
        click.echo("(single-host mode — LAN probes are loopback/local)")
