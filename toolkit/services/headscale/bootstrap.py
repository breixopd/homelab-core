"""Headscale post-deploy bootstrap: preauth keys, mesh join, and mesh node management."""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from toolkit.core.ops.automation import docker_exec

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

RunFn = Callable[[list[str], int], tuple[int, str, str]]


def _local_run(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except FileNotFoundError:
        return 127, "", f"{cmd[0]}: command not found"
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _remote_run_factory(cfg: Config, root: Path) -> RunFn:
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
    from toolkit.core.manifest.placement import service_address

    infra_ip = service_address(cfg, "headscale")

    def run(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
        remote = " ".join(shlex.quote(c) for c in cmd)
        return ssh_run_on_vm(cfg, infra_ip, remote, root=root, timeout=timeout)

    return run


def _run_cmd(cfg: Config, root: Path) -> RunFn:
    if cfg.is_multi_node:
        return _remote_run_factory(cfg, root)
    return _local_run


def _homelab_user_id(output: str) -> tuple[bool, str | None]:
    """Resolve the single exact homelab user, failing closed on bad JSON."""
    try:
        users = json.loads(output or "")
    except (TypeError, json.JSONDecodeError):
        return False, None
    if not isinstance(users, list):
        return False, None
    matches: list[str] = []
    for user in users:
        if not isinstance(user, dict):
            return False, None
        if user.get("name", user.get("Name")) != "homelab":
            continue
        user_id = user.get("id", user.get("ID"))
        if isinstance(user_id, bool) or user_id is None or not str(user_id).strip():
            return False, None
        matches.append(str(user_id).strip())
    if len(matches) > 1:
        return False, None
    return True, matches[0] if matches else None


def _fleet_join_recovery_command(login_server: str, *, fleet: bool) -> str:
    """Return a recovery command without embedding bearer key material."""
    del login_server
    suffix = " --fleet" if fleet else ""
    return f"sudo -E homelab-toolkit mesh join{suffix}"


def _local_homelab_user_id() -> str | None:
    """Resolve or create the exact local managed Headscale user."""
    users_rc, users_out = docker_exec("headscale", ["headscale", "-o", "json", "users", "list"])
    if users_rc != 0:
        return None
    valid, user_id = _homelab_user_id(users_out)
    if not valid:
        return None
    if user_id is None:
        create_rc, _ = docker_exec("headscale", ["headscale", "users", "create", "homelab"])
        if create_rc != 0:
            return None
        users_rc, users_out = docker_exec("headscale", ["headscale", "-o", "json", "users", "list"])
        if users_rc != 0:
            return None
        valid, user_id = _homelab_user_id(users_out)
        if not valid:
            return None
    return user_id


def _remote_homelab_user_id(cfg: Config, root: Path) -> str | None:
    """Resolve or create the exact managed user on the Headscale VM."""
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
    from toolkit.core.manifest.placement import service_address

    infra_ip = service_address(cfg, "headscale")
    users_cmd = "docker exec headscale headscale -o json users list"
    rc, users_out, _ = ssh_run_on_vm(cfg, infra_ip, users_cmd, root=root, timeout=60)
    if rc != 0:
        return None
    valid, user_id = _homelab_user_id(users_out)
    if not valid:
        return None
    if user_id is not None:
        return user_id
    create_user_cmd = "docker exec headscale headscale users create homelab"
    rc, _, _ = ssh_run_on_vm(cfg, infra_ip, create_user_cmd, root=root, timeout=60)
    if rc != 0:
        return None
    rc, users_out, _ = ssh_run_on_vm(cfg, infra_ip, users_cmd, root=root, timeout=60)
    if rc != 0:
        return None
    valid, user_id = _homelab_user_id(users_out)
    return user_id if valid else None


def headscale_preauth_key(*, tags: list[str] | None = None) -> str | None:
    """Create a short-lived, single-use key for one immediate enrollment."""
    user_id = _local_homelab_user_id()
    if user_id is None:
        return None
    create_cmd = [
        "headscale",
        "preauthkeys",
        "create",
        "--user",
        str(user_id),
        "--expiration",
        "1h",
    ]
    for tag in tags or []:
        tag = tag.strip()
        if tag:
            create_cmd.extend(["--tags", tag])

    create_rc, create_out = docker_exec("headscale", create_cmd)
    if create_rc != 0:
        return None
    return _parse_headscale_preauth_output(create_out or "")


def _parse_headscale_preauth_output(output: str) -> str | None:
    text = (output or "").strip()
    if not text:
        return None
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        key = data.get("key")
        return key.strip() if isinstance(key, str) and key.strip() else None
    if isinstance(data, list):
        for row in data:
            if isinstance(row, dict):
                key = row.get("key")
                if isinstance(key, str) and key.strip():
                    return key.strip()
    return None


def headscale_preauth_key_for_deploy(cfg: Config, root: Path, *, tags: list[str] | None = None) -> str | None:
    """Return a Headscale preauth key from local docker (single-host) or infra LXC."""
    if not cfg.is_multi_node:
        return headscale_preauth_key(tags=tags)
    import shlex

    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
    from toolkit.core.manifest.placement import service_address

    infra_ip = service_address(cfg, "headscale")
    user_id = _remote_homelab_user_id(cfg, root)
    if user_id is None:
        return None
    tag_args = "".join(f" --tags {shlex.quote(t.strip())}" for t in (tags or []) if t.strip())
    create_cmd = f"docker exec headscale headscale -o json preauthkeys create -u {shlex.quote(user_id)} -e 1h{tag_args}"
    rc, out, _ = ssh_run_on_vm(cfg, infra_ip, create_cmd, root=root, timeout=60)
    if rc != 0:
        return None
    return _parse_headscale_preauth_output(out or "")


def bootstrap_headscale_preauth(
    cfg: Config | None = None,
    root: Path | None = None,
    *,
    tags: list[str] | None = None,
) -> list[str]:
    """Verify enrollment prerequisites without creating an unused bearer key."""
    del tags
    logs: list[str] = []
    if cfg is not None and cfg.is_multi_node:
        user_id = _remote_homelab_user_id(cfg, root or Path("."))
    else:
        user_id = _local_homelab_user_id()
    if user_id is not None:
        logs.append("Headscale: preauth prerequisites ready")
    else:
        logs.append("Headscale: preauth prerequisites unavailable")
    return logs


def headscale_control_state_verified(login_server: str) -> bool:
    """Return whether Tailscale is running against the exact Headscale URL."""
    try:
        status = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        prefs = subprocess.run(
            ["tailscale", "debug", "prefs"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        status_data = json.loads(status.stdout or "{}") if status.returncode == 0 else {}
        prefs_data = json.loads(prefs.stdout or "{}") if prefs.returncode == 0 else {}
    except (FileNotFoundError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return False
    control_url = str(prefs_data.get("ControlURL") or "").rstrip("/")
    return status_data.get("BackendState") == "Running" and control_url == login_server.rstrip("/")


def _is_tailscale_saas_control_url(control_url: str) -> bool:
    """Match only HTTPS origins owned by tailscale.com."""
    try:
        parsed = urlsplit(control_url)
        hostname = parsed.hostname or ""
    except ValueError:
        return False
    return (
        parsed.scheme == "https"
        and not parsed.username
        and not parsed.password
        and (hostname == "tailscale.com" or hostname.endswith(".tailscale.com"))
    )


def ensure_controller_mesh_joined(
    cfg: Config,
    *,
    preauth_key: str | None = None,
    root: Path | None = None,
    fleet: bool = False,
    hostname: str = "homelab-controller",
) -> list[str]:
    """Join the deploy controller to Headscale (opt-in only).

    Personal join (``fleet=False``): OIDC browser login — node shows under your Authelia user.
    Fleet join (``fleet=True``): tagged preauth key — node shows as ``tagged-devices``.

    Set ``HOMELAB_JOIN_CONTROLLER_MESH=1`` before calling. Revert with ``tailscale logout``.
    """
    import os
    import subprocess

    from toolkit.core.registry.mesh import personal_mesh_up_args

    logs: list[str] = []
    if os.environ.get("HOMELAB_NODE"):
        return logs

    if os.environ.get("HOMELAB_JOIN_CONTROLLER_MESH", "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        logs.append("Headscale: controller mesh join skipped (set HOMELAB_JOIN_CONTROLLER_MESH=1 to enable)")
        return logs

    login_server = f"https://vpn.{cfg.domain}"
    try:
        proc = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            data = json.loads(proc.stdout)
            if data.get("BackendState") == "Running":
                control = ""
                try:
                    prefs = subprocess.run(
                        ["tailscale", "debug", "prefs"],
                        capture_output=True,
                        text=True,
                        timeout=10,
                        check=False,
                    )
                    if prefs.returncode == 0 and prefs.stdout.strip():
                        control = str(json.loads(prefs.stdout).get("ControlURL") or "")
                except (FileNotFoundError, json.JSONDecodeError, subprocess.TimeoutExpired):
                    pass
                if control.rstrip("/") == login_server:
                    logs.append("Headscale: controller mesh active")
                    return logs
                if _is_tailscale_saas_control_url(control):
                    logs.append(
                        "Headscale: controller still on Tailscale SaaS — run "
                        "'sudo tailscale logout' first if you intend to join Headscale"
                    )
                    return logs
    except (FileNotFoundError, json.JSONDecodeError, subprocess.TimeoutExpired):
        logs.append("Headscale: tailscale not on controller — skip mesh join")
        return logs

    if fleet:
        key = preauth_key
        if not key:
            deploy_root = root or Path(".")
            key = headscale_preauth_key_for_deploy(
                cfg, deploy_root, tags=list(cfg.fleet.headscale_tags or ["tag:fleet-external"])
            )
        if not key:
            logs.append("Headscale: no fleet preauth key")
            return logs
        up_cmd = [
            "tailscale",
            "up",
            f"--login-server={login_server}",
            f"--auth-key={key}",
            f"--hostname={hostname}",
            "--accept-routes",
            "--reset",
        ]
    else:
        up_cmd = personal_mesh_up_args(cfg, hostname=hostname)

    try:
        up = subprocess.run(up_cmd, capture_output=True, text=True, timeout=120, check=False)
    except subprocess.TimeoutExpired:
        logs.append("Headscale: tailscale up timed out; mesh state was not verified")
        return logs
    except FileNotFoundError:
        logs.append("Headscale: tailscale CLI is unavailable")
        return logs
    if up.returncode == 0:
        if headscale_control_state_verified(login_server):
            logs.append(
                "Headscale: controller joined mesh (OIDC)" if not fleet else "Headscale: fleet node joined mesh"
            )
        else:
            logs.append("Headscale: tailscale up returned success, but Headscale control state was not verified")
    else:
        detail = (up.stderr or up.stdout or "tailscale up failed")[:160]
        if fleet and ("Access denied" in detail or "sudo" in detail.lower()):
            logs.append(
                "Headscale: rerun the managed join with privileges: "
                f"`{_fleet_join_recovery_command(login_server, fleet=True)}`"
            )
        elif not fleet and "needs login" not in detail.lower():
            command = _fleet_join_recovery_command(login_server, fleet=False)
            logs.append(f"Headscale: OIDC mesh join — run `{command}` ({detail})")
        else:
            logs.append(f"Headscale: complete browser login if prompted ({detail})")
    return logs


# ── Headscale mesh management (moved from mesh_bootstrap) ────────────────────


def personal_headscale_username(cfg: Config) -> str:
    """Headscale local username for the homelab owner (matches OIDC strip-email default)."""
    email = (cfg.email or f"admin@{cfg.domain}").strip().lower()
    return email.split("@", 1)[0] if "@" in email else email


def approve_mesh_registration(
    cfg: Config,
    root: Path,
    *,
    key: str,
    user: str | None = None,
) -> list[str]:
    """Approve a pending Headscale web-auth registration (fallback when OIDC UI is skipped)."""
    key = key.strip()
    if not key:
        return ["Mesh approve: empty registration key"]

    hs_user = (user or personal_headscale_username(cfg)).strip()
    if not hs_user:
        return ["Mesh approve: could not resolve Headscale username"]

    from toolkit.core.manifest.placement import service_address
    from toolkit.services.sdk import docker_exec_on_vm

    vm_ip = service_address(cfg, "headscale") if cfg.is_multi_node else "127.0.0.1"
    logs: list[str] = []

    rc, out = docker_exec_on_vm(
        cfg,
        "headscale",
        ["headscale", "users", "list", "-o", "json"],
        vm_ip,
        root,
        timeout=30,
    )
    if rc != 0:
        return [f"Mesh approve: headscale users list failed ({(out or '')[:100]})"]

    try:
        users = json.loads(out or "")
    except (TypeError, json.JSONDecodeError):
        return ["Mesh approve: headscale users list returned invalid JSON"]
    if not isinstance(users, list) or any(not isinstance(item, dict) for item in users):
        return ["Mesh approve: headscale users list returned an invalid response"]

    known = [str(item.get("name") or item.get("Name") or "").strip() for item in users]
    if known.count(hs_user) > 1:
        return [f"Mesh approve: multiple Headscale users matched {hs_user!r}"]
    if hs_user not in known:
        rc_c, create_output = docker_exec_on_vm(
            cfg,
            "headscale",
            ["headscale", "users", "create", hs_user, "--force"],
            vm_ip,
            root,
            timeout=30,
        )
        if rc_c != 0:
            detail = (create_output or "").strip()[:120]
            return [f"Mesh approve: could not create user {hs_user!r} ({detail})"]
        logs.append(f"Mesh approve: created Headscale user {hs_user!r}")

    rc_r, register_output = docker_exec_on_vm(
        cfg,
        "headscale",
        [
            "/bin/busybox",
            "sh",
            "-ec",
            'IFS= read -r registration_key; exec headscale auth register --user "$1" --auth-id "$registration_key"',
            "homelab-headscale-register",
            hs_user,
        ],
        vm_ip,
        root,
        timeout=45,
        stdin=f"{key}\n",
    )
    detail = (register_output or "").strip()
    error_lines = [ln for ln in detail.splitlines() if "WRN " not in ln and ln.strip()]
    err_msg = "\n".join(error_lines).strip() or detail
    if rc_r == 0:
        logs.append(f"Mesh approve: registered node for user {hs_user!r}")
        return logs
    if "registration cache" in err_msg.lower():
        logs.append(
            "Mesh approve: registration key expired — re-run `tailscale up` / `mesh join`, "
            "then `mesh approve --key <KEY>` immediately or use OIDC on the Headscale page"
        )
    return [f"Mesh approve: register failed ({err_msg[:200]})"]


def headscale_oidc_cli_fallback(log_text: str) -> bool:
    """True when Headscale gave up on OIDC and is using manual registration."""
    if "falling back to CLI based authentication" not in log_text:
        return False
    start = log_text.rfind("Starting Headscale")
    fallback = log_text.rfind("falling back to CLI based authentication")
    return fallback >= 0 and (start < 0 or fallback > start)


def ensure_headscale_oidc_provider(cfg: Config, root: Path) -> list[str]:
    """Restart Headscale when it started before Authelia/Caddy and fell back to CLI auth."""
    if not cfg.category_enabled("security"):
        return []

    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
    from toolkit.core.manifest.placement import service_address

    logs: list[str] = []
    infra_ip = service_address(cfg, "headscale") if cfg.is_multi_node else "127.0.0.1"
    rc, log_out, _ = ssh_run_on_vm(cfg, infra_ip, "docker logs --tail 100 headscale 2>&1", root=root, timeout=30)
    log_text = log_out or ""
    if rc != 0 and not log_text.strip():
        logs.append("Headscale OIDC: could not read container logs")
        return logs
    if not headscale_oidc_cli_fallback(log_text):
        logs.append("Headscale OIDC: provider active")
        return logs

    logs.append("Headscale OIDC: CLI fallback detected — restarting after Authelia/Caddy are up")
    rc_r, _, err_r = ssh_run_on_vm(cfg, infra_ip, "docker restart headscale", root=root, timeout=60)
    if rc_r != 0:
        logs.append(f"Headscale OIDC: restart failed ({(err_r or '')[:120]})")
        return logs

    _, after, _ = ssh_run_on_vm(cfg, infra_ip, "docker logs --tail 100 headscale 2>&1", root=root, timeout=30)
    if headscale_oidc_cli_fallback(after or ""):
        logs.append("Headscale OIDC: still in CLI fallback after restart — check auth from headscale container")
    else:
        logs.append("Headscale OIDC: provider recovered after restart")
    return logs


def list_mesh_nodes(cfg: Config, root: Path) -> list[tuple[str, str]]:
    """Fetch (node_name, ipv4) for every Headscale node, online or offline.

    Used to seed AdGuard mesh rewrites so mesh node names resolve from every
    client (LAN/containers/mesh) via the single AdGuard source of truth.
    """
    if not cfg.is_multi_node or not cfg.category_enabled("security"):
        return []
    run = _run_cmd(cfg, root)
    rc, out, _ = run(
        ["docker", "exec", "headscale", "headscale", "nodes", "list", "-o", "json"],
        30,
    )
    if rc != 0 or not (out or "").strip():
        return []
    try:
        nodes = json.loads(out)
    except json.JSONDecodeError:
        return []
    result: list[tuple[str, str]] = []
    for node in nodes if isinstance(nodes, list) else []:
        if not isinstance(node, dict):
            continue
        name = str(node.get("name") or node.get("Name") or "").strip()
        if not name:
            continue
        ips = node.get("ip_addresses") or node.get("IPAddresses") or []
        ipv4 = ""
        for ip in ips:
            if isinstance(ip, str) and "." in ip and not ip.startswith("127."):
                ipv4 = ip
                break
        if name and ipv4:
            result.append((name, ipv4))
    return result
