"""Headscale post-deploy bootstrap: preauth keys, mesh join, and mesh node management."""

from __future__ import annotations

import json
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

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


def headscale_preauth_key(*, tags: list[str] | None = None) -> str | None:
    """Return a reusable Headscale preauth key, creating one if needed."""
    users_rc, users_out = docker_exec("headscale", ["headscale", "-o", "json", "users", "list"])
    if users_rc != 0:
        return None
    try:
        users = json.loads(users_out or "[]")
    except json.JSONDecodeError:
        return None

    if not users:
        create_rc, _ = docker_exec("headscale", ["headscale", "users", "create", "homelab"])
        if create_rc != 0:
            return None
        users_rc, users_out = docker_exec("headscale", ["headscale", "-o", "json", "users", "list"])
        try:
            users = json.loads(users_out or "[]")
        except json.JSONDecodeError:
            return None

    user_id = users[0].get("id") if users else None
    if not user_id:
        return None

    keys_rc, keys_out = docker_exec(
        "headscale",
        ["headscale", "-o", "json", "preauthkeys", "list", "--user", str(user_id)],
    )
    if keys_rc == 0 and keys_out:
        try:
            keys = json.loads(keys_out)
            from toolkit.core.registry.mesh import preauth_key_tags_match

            reusable = [
                k
                for k in keys
                if k.get("reusable")
                and not k.get("used")
                and not k.get("expired")
                and preauth_key_tags_match(k, tags)
                and _unmasked_preauth_key(k.get("key"))
            ]
            if reusable:
                return _unmasked_preauth_key(reusable[0].get("key"))
        except json.JSONDecodeError:
            pass

    create_cmd = [
        "headscale",
        "preauthkeys",
        "create",
        "--user",
        str(user_id),
        "--reusable",
        "--expiration",
        "168h",
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
        if isinstance(data, dict) and data.get("key"):
            return str(data["key"])
        if isinstance(data, list):
            for row in data:
                if row.get("reusable") and not row.get("used") and not row.get("expired") and row.get("key"):
                    return str(row["key"])
    except json.JSONDecodeError:
        pass
    import re

    match = re.search(r'"key":\s*"([^"]+)"', text)
    if match:
        return match.group(1)
    line = text.splitlines()[-1].strip()
    return line or None


def _unmasked_preauth_key(value: object) -> str | None:
    """Return a reusable bearer key only when Headscale disclosed it in full."""
    key = str(value or "").strip()
    return key if key and "*" not in key else None


def headscale_preauth_key_for_deploy(cfg: Config, root: Path, *, tags: list[str] | None = None) -> str | None:
    """Return a Headscale preauth key from local docker (single-host) or infra LXC."""
    if not cfg.is_multi_node:
        return headscale_preauth_key(tags=tags)
    import shlex

    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
    from toolkit.core.manifest.placement import service_address

    infra_ip = service_address(cfg, "headscale")
    list_cmd = "docker exec headscale headscale -o json preauthkeys list"
    rc, out, _ = ssh_run_on_vm(cfg, infra_ip, list_cmd, root=root, timeout=60)
    if rc == 0 and out:
        try:
            from toolkit.core.registry.mesh import preauth_key_tags_match

            keys = json.loads(out)
            for row in keys if isinstance(keys, list) else []:
                user = row.get("user") or {}
                if user.get("id") not in (None, 1) and user.get("name") not in (None, "homelab"):
                    continue
                if (
                    row.get("reusable")
                    and not row.get("used")
                    and not row.get("expired")
                    and preauth_key_tags_match(row, tags)
                    and _unmasked_preauth_key(row.get("key"))
                ):
                    return _unmasked_preauth_key(row.get("key"))
        except json.JSONDecodeError:
            pass
    tag_args = "".join(f" --tags {shlex.quote(t.strip())}" for t in (tags or []) if t.strip())
    create_cmd = f"docker exec headscale headscale -o json preauthkeys create -u 1 --reusable -e 168h{tag_args}"
    rc, out, _ = ssh_run_on_vm(cfg, infra_ip, create_cmd, root=root, timeout=60)
    if rc != 0:
        return None
    return _parse_headscale_preauth_output(out or "")


def bootstrap_headscale_preauth(*, tags: list[str] | None = None) -> list[str]:
    logs: list[str] = []
    key = headscale_preauth_key(tags=tags)
    if key:
        # Preauth keys are bearer credentials. Never include even a prefix in
        # deployment output: logs are persisted and routinely forwarded to
        # dashboards, CI, and support tooling.
        logs.append("Headscale: preauth key ready")
    else:
        logs.append("Headscale: preauth key create failed")
    return logs


def ensure_controller_mesh_joined(
    cfg: Config, *, preauth_key: str | None = None, root: Path | None = None, fleet: bool = False
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
                if control and login_server in control:
                    logs.append("Headscale: controller mesh active")
                    return logs
                if control and "tailscale.com" in control:
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
            f"--authkey={key}",
            "--hostname=homelab-controller",
            "--accept-routes",
            "--reset",
        ]
    else:
        up_cmd = personal_mesh_up_args(cfg)

    up = subprocess.run(up_cmd, capture_output=True, text=True, timeout=120, check=False)
    if up.returncode == 0:
        logs.append("Headscale: controller joined mesh (OIDC)" if not fleet else "Headscale: fleet node joined mesh")
    else:
        detail = (up.stderr or up.stdout or "tailscale up failed")[:160]
        if fleet and ("Access denied" in detail or "sudo" in detail.lower()):
            logs.append("Headscale: run `homelab-toolkit mesh join-cmd --fleet` and execute the printed command")
        elif not fleet and "needs login" not in detail.lower():
            logs.append(f"Headscale: OIDC mesh join — run `homelab-toolkit mesh join-cmd` ({detail})")
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

    run = _run_cmd(cfg, root)
    logs: list[str] = []

    rc, out, _ = run(
        ["docker", "exec", "headscale", "headscale", "users", "list", "-o", "json"],
        30,
    )
    if rc != 0:
        return [f"Mesh approve: headscale users list failed ({(out or '')[:100]})"]

    try:
        users = json.loads(out or "[]")
    except json.JSONDecodeError:
        users = []

    known = {str(u.get("name") or u.get("Name") or "").strip() for u in users if isinstance(u, dict)}
    if hs_user not in known:
        rc_c, out_c, err_c = run(
            ["docker", "exec", "headscale", "headscale", "users", "create", hs_user, "--force"],
            30,
        )
        if rc_c != 0:
            detail = (err_c or out_c or "").strip()[:120]
            return [f"Mesh approve: could not create user {hs_user!r} ({detail})"]
        logs.append(f"Mesh approve: created Headscale user {hs_user!r}")

    rc_r, out_r, err_r = run(
        [
            "docker",
            "exec",
            "headscale",
            "headscale",
            "nodes",
            "register",
            "--user",
            hs_user,
            "--key",
            key,
            "--force",
        ],
        45,
    )
    detail = (err_r or out_r or "").strip()
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
