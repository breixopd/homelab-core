"""SSH connectivity helpers for multi-VM deploy."""

from __future__ import annotations

from pathlib import Path

from toolkit.core.ansible.ansible_ssh import refresh_known_hosts_file, resolve_ansible_ssh_key, ssh_run_on_vm
from toolkit.core.config.config import Config


def probe_ssh_connectivity(
    cfg: Config,
    root: Path,
    *,
    targets: tuple[str, ...] | None = None,
) -> list[str]:
    """Probe Proxmox jump + each enabled LXC; refresh known_hosts on success."""
    lines: list[str] = []
    key = resolve_ansible_ssh_key(cfg, root)
    if not key:
        return ["SSH: no private key found (set ssh.key_file in config.yaml)"]
    lines.append(f"SSH: using guest key {key}")
    selected = targets or tuple(cfg.enabled_nodes)
    if any(vm not in cfg.enabled_nodes for vm in selected):
        raise ValueError("SSH probe target is not enabled")
    for vm in selected:
        ip = cfg.node_ip(vm)
        if not ip:
            lines.append(f"SSH: skip {vm} (no IP in config)")
            continue
        rc, out, err = ssh_run_on_vm(cfg, ip, "hostname", root=root, timeout=45, retries=2)
        if rc == 0:
            host = (out or "").strip()
            lines.append(f"SSH: OK {vm} ({ip}) → {host}")
        else:
            detail = (err or out or f"exit {rc}").strip().splitlines()[-1]
            lines.append(f"SSH: FAIL {vm} ({ip}) — {detail}")
    lines.extend(refresh_known_hosts_file(root, cfg))
    return lines


def ssh_ok(cfg: Config, root: Path, *, targets: tuple[str, ...] | None = None) -> bool:
    """True iff every enabled LXC responds to an SSH probe.

    The first ``SSH: using key ...`` line is informational (no ``OK`` marker),
    so a naive ``all("OK" in l for l in lines if ... and "FAIL" not in l)``
    poisoned the result with False on a *healthy* fleet. Track ``OK`` and
    ``FAIL`` explicitly so a clean fleet returns True.
    """
    ssh_lines = [line for line in probe_ssh_connectivity(cfg, root, targets=targets) if line.startswith("SSH:")]
    if not ssh_lines:
        return False
    has_ok = any(line.startswith("SSH: OK") for line in ssh_lines)
    has_fail = any("FAIL" in line for line in ssh_lines)
    return has_ok and not has_fail
