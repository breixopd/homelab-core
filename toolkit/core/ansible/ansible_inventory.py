"""Generate portable Ansible inventory from config.yaml (no hardcoded laptop paths)."""

from __future__ import annotations

import os
from pathlib import Path

import yaml

from toolkit.core.ansible.ansible_ssh import _is_directly_reachable, resolve_ansible_ssh_key
from toolkit.core.config.config import Config, config_path, load_config
from toolkit.core.infra.host_capacity import resolve_proxmox_host
from toolkit.core.infra.proxmox_ssh import resolve_proxmox_proxy_key

_SSH_KEEPALIVE = "-o ServerAliveInterval=30 -o ServerAliveCountMax=120"


def generated_extra_vars(root: Path) -> list[str]:
    """Ansible ``-e @file`` args for config-derived group_vars not auto-loaded by group name."""
    gv = root / "automation" / "ansible" / "group_vars"
    args: list[str] = []
    for name in ("generated.yml", "generated-routes.yml"):
        f = gv / name
        if f.is_file():
            args += ["-e", f"@{f}"]
    return args


def _yaml_dump(data: dict) -> str:
    """Dump inventory YAML with quoted SSH arg strings (avoid `-o` list parsing)."""

    class _Quoted(str):
        pass

    def _quoted_representer(dumper, value):
        return dumper.represent_scalar("tag:yaml.org,2002:str", value, style='"')

    yaml.add_representer(_Quoted, _quoted_representer)

    def _quote_ssh(obj) -> None:
        if isinstance(obj, dict):
            for key, val in list(obj.items()):
                if key == "ansible_ssh_common_args" and isinstance(val, str):
                    obj[key] = _Quoted(val)
                else:
                    _quote_ssh(val)
        elif isinstance(obj, list):
            for item in obj:
                _quote_ssh(item)

    import copy

    payload = copy.deepcopy(data)
    _quote_ssh(payload)
    return yaml.dump(payload, default_flow_style=False, sort_keys=False)


def _known_hosts_path(root: Path) -> Path:
    return root / "automation" / "ansible" / "inventory" / "known_hosts"


def _guest_ssh_args(cfg: Config, root: Path, *, prox_host: str, key: Path, direct: bool = False) -> str:
    kh = _known_hosts_path(root)
    kh_opt = f"-o UserKnownHostsFile={kh}" if kh.is_file() else ""
    base = (
        f"-o BatchMode=yes -o StrictHostKeyChecking=accept-new "
        f"-o IdentitiesOnly=yes -o IdentityAgent=none -o IdentityFile={key} {_SSH_KEEPALIVE} "
        f"-o ConnectTimeout={cfg.ssh.connect_timeout} {kh_opt}"
    ).strip()
    if direct:
        return base
    control_key = resolve_proxmox_proxy_key(cfg, root)
    if control_key is None:
        raise ValueError("a usable Proxmox SSH key is required for guest ProxyCommand access")
    control = cfg.proxmox.ssh
    proxy_inner = (
        f"ssh -i {control_key} -p {control.port} -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none "
        f"-o StrictHostKeyChecking=accept-new {_SSH_KEEPALIVE} "
        f"-o ConnectTimeout={control.connect_timeout} {kh_opt} "
        f"-W %h:%p {control.user}@{prox_host}"
    )
    return f'{base} -o ProxyCommand="{proxy_inner}"'


def render_inventory(
    cfg: Config,
    root: Path,
    *,
    machine_ips: dict[str, str] | None = None,
    proxmox_only: bool = False,
) -> dict:
    """Build inventory dict for YAML serialization."""
    root = root.resolve()
    prox = resolve_proxmox_host(cfg, root)
    if not prox:
        raise ValueError(
            "Could not resolve Proxmox control host. Set dns.public_ip, proxmox.api_url, "
            "or host_capacity.proxmox_host in config.yaml."
        )

    key = resolve_ansible_ssh_key(cfg, root)
    managed_node = bool(os.environ.get("HOMELAB_NODE"))
    kh = _known_hosts_path(root)

    all_vars: dict = {
        "ansible_become": True,
        "ansible_python_interpreter": "/usr/bin/python3",
        "base_domain": cfg.domain,
        "homelab_multi_node": cfg.is_multi_node,
        "ansible_ssh_common_args": (
            f"-o StrictHostKeyChecking=accept-new "
            f"{f'-o UserKnownHostsFile={kh}' if kh.is_file() else ''} {_SSH_KEEPALIVE} "
            f"-o ConnectTimeout={cfg.ssh.connect_timeout}".strip()
        ),
    }
    if key is not None:
        all_vars["ansible_ssh_private_key_file"] = str(key)

    proxmox_host_vars: dict = {
        "ansible_host": prox,
        "ansible_user": cfg.proxmox.ssh.user,
        "ansible_port": cfg.proxmox.ssh.port,
    }
    control_key = resolve_proxmox_proxy_key(cfg, root)
    if control_key is not None:
        proxmox_host_vars["ansible_ssh_private_key_file"] = str(control_key)

    children: dict = {
        "proxmox_hosts": {
            "hosts": {
                "pve-01": proxmox_host_vars,
            }
        },
    }

    if not proxmox_only:
        ips = dict(machine_ips or {})
        for node in cfg.enabled_nodes:
            ips.setdefault(node, cfg.node_ip(node))

        guest_groups: dict = {}
        guest_children: dict = {}
        for node in cfg.enabled_nodes:
            if node not in ips:
                continue
            machine = cfg.machines[node]
            host = machine.hostname
            host_vars: dict = {
                "ansible_host": ips[node],
                "ansible_user": machine.effective_ssh_user,
                "ansible_port": machine.ssh_port,
                "homelab_node_id": node,
                "homelab_machine_kind": machine.kind,
                "homelab_machine_labels": list(machine.labels),
            }
            if key is not None:
                host_vars["ansible_ssh_common_args"] = _guest_ssh_args(
                    cfg,
                    root,
                    prox_host=prox,
                    key=key,
                    direct=managed_node and _is_directly_reachable(ips[node], machine.cidr),
                )
            guest_groups[node] = {"hosts": {host: host_vars}}
            guest_children[node] = None

        children.update(guest_groups)
        children["guest_hosts"] = {"children": guest_children}

    # Keep optional groups present even when empty. Playbooks that target a
    # fleet group then produce a clean no-op instead of an Ansible warning.
    ext: dict = {}
    for ext_host in cfg.external_hosts:
        from toolkit.core.infra.fleet_roles import ansible_roles_for_services

        ext_host_vars: dict = {
            "ansible_host": ext_host.ip,
            "ansible_user": ext_host.ssh_user,
            "ansible_port": ext_host.ssh_port,
            "external_services": list(ext_host.services or []),
            "external_service_roles": ansible_roles_for_services(ext_host.services, kind=ext_host.kind, cfg=cfg),
        }
        if ext_host.integrations:
            ext_host_vars["host_integrations"] = ext_host.integrations
        if key is not None:
            ext_host_vars["ansible_ssh_private_key_file"] = str(key)
        ext[ext_host.name] = ext_host_vars
    children["external_hosts"] = {"hosts": ext}

    return {"all": {"vars": all_vars, "children": children}}


def _refresh_known_hosts(root: Path, cfg: Config, *, extra_ips: list[str] | None = None) -> None:
    """Drop stale LXC host keys after reprovision (avoid WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED)."""
    import subprocess

    kh = _known_hosts_path(root)
    kh.parent.mkdir(parents=True, exist_ok=True)
    kh.touch(exist_ok=True)
    ips: set[str] = set(extra_ips or [])
    prox = resolve_proxmox_host(cfg, root)
    if prox:
        ips.add(prox)
    for node in cfg.enabled_nodes:
        ips.add(cfg.node_ip(node))
    for ip in ips:
        subprocess.run(["ssh-keygen", "-R", ip, "-f", str(kh)], capture_output=True, check=False)


def write_inventory(
    root: Path,
    cfg: Config | None = None,
    *,
    machine_ips: dict[str, str] | None = None,
    proxmox_only: bool = False,
) -> Path:
    """Write ``hosts.yml`` from config without mutating SSH trust state.

    Inventory rendering is a routine, declarative operation and must not remove
    pinned host keys.  Host-key discovery/rotation is intentionally an explicit
    operation via :func:`toolkit.core.ansible.ansible_ssh.refresh_known_hosts_file`.
    """
    root = root.resolve()
    if cfg is None:
        cfg = load_config(config_path(root))
    inv_dir = root / "automation" / "ansible" / "inventory"
    inv_dir.mkdir(parents=True, exist_ok=True)
    kh = inv_dir / "known_hosts"
    kh.touch(exist_ok=True)

    data = render_inventory(cfg, root, machine_ips=machine_ips, proxmox_only=proxmox_only)
    out = inv_dir / "hosts.yml"
    out.write_text(_yaml_dump(data))
    return out


def resolve_node_host_ip(root: Path, node: str, cfg: Config | None = None) -> str | None:
    """Return ``ansible_host`` for a node from inventory, then config."""
    inv_path = root / "automation" / "ansible" / "inventory" / "hosts.yml"
    if inv_path.is_file():
        try:
            data = yaml.safe_load(inv_path.read_text()) or {}
            children = data.get("all", {}).get("children", {})
            group = children.get(node, {})
            hosts = group.get("hosts", {}) if isinstance(group, dict) else {}
            if isinstance(hosts, dict):
                for entry in hosts.values():
                    if isinstance(entry, dict) and entry.get("ansible_host"):
                        return str(entry["ansible_host"]).strip()
        except yaml.YAMLError:
            pass

    if cfg is not None:
        try:
            return cfg.node_ip(node)
        except (AttributeError, KeyError, ValueError):
            return None
    return None


def ensure_group_vars_all(root: Path) -> Path:
    """Ensure group_vars/all.yml exists (copy from all.example if missing)."""
    gv = root / "automation" / "ansible" / "group_vars"
    target = gv / "all.yml"
    if target.is_file():
        return target
    for name in ("all.example.yml", "all.example"):
        example = gv / name
        if example.is_file():
            target.write_text(example.read_text())
            return target
    raise FileNotFoundError(f"No group_vars/all.yml or all.example found under {gv}")


def parse_tofu_machine_ips(infra_dir: Path) -> dict[str, str]:
    """Read machine addresses from OpenTofu output when state exists."""
    import json
    import shutil
    import subprocess

    tofu = shutil.which("tofu") or shutil.which("terraform")
    if not tofu or not infra_dir.is_dir():
        return {}
    if not (infra_dir / ".terraform").exists() and not (infra_dir / ".tofu").exists():
        return {}
    try:
        proc = subprocess.run(
            [tofu, "output", "-json", "machine_ips"],
            cwd=str(infra_dir),
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
        if proc.returncode != 0 or not proc.stdout.strip():
            return {}
        raw = json.loads(proc.stdout)
        return {k: str(v) for k, v in raw.items()}
    except (OSError, json.JSONDecodeError, subprocess.TimeoutExpired):
        return {}
