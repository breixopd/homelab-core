"""Headscale mesh: internal LAN routes, subnet routing, and split DNS."""

from __future__ import annotations

import importlib as _importlib
import json
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.core.registry.mesh import (
    mesh_infra_login_server,
    mesh_lan_cidr,
    mesh_router_tag,
)

_headscale_bootstrap = _importlib.import_module("toolkit.services.headscale.bootstrap")
headscale_preauth_key_for_deploy = _headscale_bootstrap.headscale_preauth_key_for_deploy

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

    router_ip = service_address(cfg, "headscale")

    def run(cmd: list[str], timeout: int = 120) -> tuple[int, str, str]:
        remote = " ".join(shlex.quote(c) for c in cmd)
        return ssh_run_on_vm(cfg, router_ip, remote, root=root, timeout=timeout)

    return run


def _run_cmd(cfg: Config, root: Path) -> RunFn:
    if cfg.is_multi_node:
        return _remote_run_factory(cfg, root)
    return _local_run


def _ensure_tailscale_installed(run: RunFn, logs: list[str]) -> bool:
    rc, _, _ = run(["tailscale", "version"], 15)
    if rc == 0:
        run(["systemctl", "start", "tailscaled"], 20)
        return True
    logs.append("Mesh router: Tailscale is missing; rerun the managed guest deployment")
    return False


def _enable_ip_forward(run: RunFn, logs: list[str]) -> None:
    rc, out, _ = run(["sysctl", "-n", "net.ipv4.ip_forward"], 10)
    if rc == 0 and out.strip() == "1":
        return
    rc, _, err = run(["sysctl", "-w", "net.ipv4.ip_forward=1"], 10)
    if rc == 0:
        logs.append("Mesh router: enabled IPv4 forwarding")
    else:
        logs.append(f"Mesh router: ip_forward warn ({err[:80]})")


def _tailscale_coordination_healthy(run: RunFn) -> bool:
    """False when the local tailscale client cannot reach its control plane."""
    rc, out, _ = run(["tailscale", "status", "--json"], 20)
    if rc != 0 or not out.strip():
        return False
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return False
    health = data.get("Health") or []
    if any("coordination server" in str(item).lower() for item in health):
        return False
    backend = str(data.get("BackendState") or "").lower()
    return backend in ("", "running")


def _routes_already_advertised(run: RunFn, cidr: str) -> bool:
    rc, out, _ = run(["tailscale", "status", "--json"], 20)
    if rc != 0 or not out.strip():
        return False
    try:
        data = json.loads(out)
        routes = data.get("Self", {}).get("PrimaryRoutes") or data.get("Self", {}).get("Routes") or []
        if isinstance(routes, list) and cidr in routes:
            return True
        allowed = str(data.get("Self", {}).get("AllowedIPs") or "")
        return cidr in allowed
    except json.JSONDecodeError:
        return False


def _mesh_router_node(cfg: Config) -> str:
    from toolkit.core.manifest.placement import service_node

    return service_node(cfg, "headscale")


def _ssh_proxmox(cfg: Config, root: Path, command: str, *, timeout: int = 60) -> tuple[int, str, str]:
    from toolkit.core.infra.zfs_detect import _ssh_proxmox

    return _ssh_proxmox(cfg, root, command, command_timeout=timeout)


def ensure_infra_tun_device(cfg: Config, root: Path, run: RunFn, logs: list[str]) -> bool:
    """Ensure the mesh-router machine exposes ``/dev/net/tun``."""
    rc, _, _ = run(["test", "-e", "/dev/net/tun"], 10)
    if rc == 0:
        return True

    router_node = _mesh_router_node(cfg)
    machine = cfg.machines[router_node]
    if machine.kind != "lxc":
        logs.append(f"Mesh router: /dev/net/tun is missing on VM {router_node}; automatic passthrough is LXC-only")
        return False
    vmid = machine.vmid
    logs.append(f"Mesh router: enabling /dev/net/tun on {router_node} LXC {vmid}")
    rc, out, err = _ssh_proxmox(cfg, root, f"pct config {vmid}", timeout=30)
    if rc != 0:
        logs.append(f"Mesh router: pct config failed ({(err or out)[:80]})")
        return False

    if "dev0:" not in out:
        rc, _, err = _ssh_proxmox(cfg, root, f"pct set {vmid} -dev0 /dev/net/tun", timeout=30)
        if rc != 0 and "already" not in (err or "").lower():
            logs.append(f"Mesh router: pct set tun failed ({err[:80]})")
            return False
        logs.append(f"Mesh router: rebooting {router_node} for TUN passthrough")
        _ssh_proxmox(cfg, root, f"pct reboot {vmid}", timeout=60)
    else:
        logs.append(f"Mesh router: TUN configured; rebooting {router_node}")
        _ssh_proxmox(cfg, root, f"pct reboot {vmid}", timeout=60)

    import time

    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
    from toolkit.core.manifest.placement import service_address

    router_ip = service_address(cfg, "headscale")
    for _ in range(36):
        time.sleep(10)
        rc, _, _ = ssh_run_on_vm(cfg, router_ip, "test -e /dev/net/tun", root=root, timeout=20)
        if rc == 0:
            logs.append(f"Mesh router: /dev/net/tun ready on {router_node}")
            return True
    logs.append(f"Mesh router: {router_node} TUN not ready after reboot")
    return False


def _resolve_infra_headscale_login(cfg: Config, run: RunFn, logs: list[str]) -> str | None:
    """Pin and verify the private TLS route used by the mesh-router node."""
    from toolkit.core.manifest.catalog import provider_service_name
    from toolkit.core.manifest.placement import service_address

    hostname = f"vpn.{cfg.domain}"
    ingress_ip = service_address(cfg, provider_service_name("ingress"))
    marker = "homelab-headscale-private-ingress"
    update_hosts = (
        'tmp="$(mktemp)"; '
        'awk -v marker="$1" \'index($0, marker) == 0\' /etc/hosts >"$tmp"; '
        'printf "%s %s # %s\\n" "$2" "$3" "$1" >>"$tmp"; '
        'cat "$tmp" >/etc/hosts; rm -f "$tmp"'
    )
    # ``sh -c`` reserves the first argument after the script for ``$0``.  Keep
    # an explicit argv0 here so the marker, ingress address, and hostname land
    # in the positional parameters consumed by the script.  Without it the
    # marker was shifted, so the router never received the private TLS pin and
    # kept trying the stale loopback control URL.
    rc, _, error = run(["sh", "-c", update_hosts, "homelab-headscale-hosts", marker, ingress_ip, hostname], 20)
    if rc != 0:
        logs.append(f"Mesh router: could not pin private Headscale ingress ({error[:80]})")
        return None

    login = mesh_infra_login_server(cfg)
    rc, out, _ = run(["curl", "-fsS", "-o", "/dev/null", "-w", "%{http_code}", f"{login}/health"], 20)
    if rc == 0 and (out or "").strip() == "200":
        return login
    logs.append("Mesh router: Headscale is not reachable on the routing node")
    return None


def bootstrap_infra_subnet_router(cfg: Config, root: Path) -> list[str]:
    """Advertise the homelab LAN from the manifest-selected mesh node."""
    logs: list[str] = []
    if not cfg.is_multi_node or not cfg.category_enabled("security"):
        return logs
    if not getattr(cfg.fleet, "mesh_subnet_router", True):
        logs.append("Mesh router: disabled in config")
        return logs

    run = _run_cmd(cfg, root)
    cidr = mesh_lan_cidr(cfg)
    tag = mesh_router_tag(cfg)
    login = _resolve_infra_headscale_login(cfg, run, logs)
    if not login:
        return logs

    if _routes_already_advertised(run, cidr) and _tailscale_coordination_healthy(run):
        logs.append(f"Mesh router: already advertising {cidr}")
        return logs

    if not ensure_infra_tun_device(cfg, root, run, logs):
        return logs

    if not _ensure_tailscale_installed(run, logs):
        return logs
    _enable_ip_forward(run, logs)

    key = headscale_preauth_key_for_deploy(cfg, root, tags=[tag])
    if not key:
        logs.append("Mesh router: no preauth key (headscale up?)")
        return logs

    up_cmd = [
        "tailscale",
        "up",
        f"--login-server={login}",
        f"--authkey={key}",
        "--hostname=homelab-router",
        f"--advertise-routes={cidr}",
        "--accept-routes",
        "--accept-dns=false",
        "--reset",
        "--force-reauth",
    ]
    rc, out, err = run(up_cmd, 300)
    detail = (err or out or "").strip()
    if rc == 0:
        logs.append(f"Mesh router: advertising {cidr} for mesh clients")
    else:
        logs.append(f"Mesh router: tailscale up failed ({detail[:120]})")
    return logs


def mesh_internal_hosts(cfg: Config) -> list[tuple[str, str]]:
    """Human-friendly internal targets reachable once subnet routes are accepted."""
    hosts: list[tuple[str, str]] = []
    for role in cfg.enabled_nodes:
        ip = cfg.node_ip(role)
        hosts.append((role, ip))
    from toolkit.core.manifest.placement import service_address

    hosts.append(("adguard-dns", service_address(cfg, "adguard")))
    return hosts


def probe_mesh_internal(cfg: Config) -> list[tuple[str, bool, str]]:
    """Probe declared node and infrastructure endpoints through the mesh."""
    import socket

    from toolkit.core.manifest.catalog import provider_service_name
    from toolkit.core.manifest.placement import service_address

    probes = [(f"{node}-ssh", cfg.node_ip(node), 22) for node in cfg.enabled_nodes]
    probes.extend(
        [
            ("adguard-dns", service_address(cfg, "adguard"), 53),
            ("https-ingress", service_address(cfg, provider_service_name("ingress")), 443),
        ]
    )
    results: list[tuple[str, bool, str]] = []
    for label, host, port in probes:
        try:
            with socket.create_connection((host, port), timeout=4):
                results.append((label, True, f"{host}:{port}"))
        except OSError as exc:
            results.append((label, False, str(exc)[:80]))
    return results


def _mesh_routes_reachable(cfg: Config) -> bool:
    """Return whether the controller can reach the manifest-selected ingress node."""
    import socket

    from toolkit.core.manifest.catalog import provider_service_name
    from toolkit.core.manifest.placement import service_address

    if not cfg.is_multi_node:
        return True
    try:
        with socket.create_connection((service_address(cfg, provider_service_name("ingress")), 22), timeout=3):
            return True
    except OSError:
        return False


def _parse_curl_headers(output: str) -> tuple[int | None, dict[str, str]]:
    status: int | None = None
    headers: dict[str, str] = {}
    for line in output.splitlines():
        if line.upper().startswith("HTTP/"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                status = int(parts[1])
        elif ":" in line:
            key, value = line.split(":", 1)
            headers[key.strip().lower()] = value.strip()
    return status, headers


def _mesh_private_https_check(host: str, ingress_ip: str, auth_host: str) -> tuple[bool, str]:
    """Probe one private route through the mesh/LAN ingress address."""
    try:
        proc = subprocess.run(
            [
                "curl",
                "-skI",
                "--max-time",
                "12",
                "--resolve",
                f"{host}:443:{ingress_ip}",
                "-H",
                "X-Forwarded-Proto: https",
                f"https://{host}/",
            ],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)[:80]
    status, headers = _parse_curl_headers(proc.stdout or proc.stderr or "")
    location = headers.get("location", "")
    ok = status in (302, 307, 308) and auth_host in location
    return ok, f"HTTP {status} -> auth" if ok else f"HTTP {status or '?'}"


def controller_mesh_access_checks(cfg: Config, root: Path):
    """Verify controller access to private routes after mesh routes are accepted."""
    from toolkit.core.manifest.catalog import provider_service_name
    from toolkit.core.manifest.placement import service_address
    from toolkit.core.manifest.routes import private_routes
    from toolkit.services.sdk import VerifyCheck

    if not cfg.is_multi_node:
        return [VerifyCheck("mesh", "client", True, "single-host skip")]
    if not _mesh_routes_reachable(cfg):
        return [
            VerifyCheck(
                "mesh",
                "client",
                True,
                "skipped - controller has no route to homelab LAN (join mesh + accept routes)",
            )
        ]
    checks = [VerifyCheck("mesh", label, ok, detail) for label, ok, detail in probe_mesh_internal(cfg)]
    ingress_ip = service_address(cfg, provider_service_name("ingress"))
    auth_host = f"auth.{cfg.domain}"
    for check_name, host in sorted({(f"private-{route.service}", route.host) for route in private_routes(cfg)}):
        ok, detail = _mesh_private_https_check(host, ingress_ip, auth_host)
        checks.append(VerifyCheck("mesh", check_name, ok, detail))
    return checks


def fleet_node_online(cfg: Config, root: Path, node_name: str) -> bool | None:
    """Return whether one managed host is registered and online in Headscale."""
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
    from toolkit.core.manifest.placement import service_address

    address = service_address(cfg, "headscale") if cfg.is_multi_node else "localhost"
    if not address:
        return None
    try:
        rc, output, _ = ssh_run_on_vm(
            cfg,
            address,
            "docker exec headscale headscale -o json nodes list 2>/dev/null",
            root=root,
            timeout=25,
            retries=2,
        )
    except (OSError, TypeError, ValueError):
        return None
    if rc != 0 or not (output or "").strip():
        return None
    try:
        nodes = json.loads(output)
    except json.JSONDecodeError:
        return None
    if not isinstance(nodes, list):
        return None
    for node in nodes:
        if not isinstance(node, dict):
            continue
        registered = str(node.get("name") or node.get("givenName") or "").strip()
        if registered == node_name:
            return bool(node.get("online"))
    return False
