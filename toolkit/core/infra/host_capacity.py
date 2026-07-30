"""Host resource detection and deploy tuning (wave timeouts, load gates)."""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

_CAP_CACHE: dict[tuple[str, str, bool, str], HostCapacity] = {}


def _capacity_cache_key(cfg: Config | None, root: Path | None, fast: bool) -> tuple[str, str, bool, str]:
    """Build a cache key from desired state, not ``id(cfg)``.

    Object-id keys can be reused after a short-lived config is collected (most
    visibly in long-running controllers and test suites), returning capacity
    detected for an unrelated configuration.  The serialized config and node
    role keep the cache deterministic while still avoiding repeated SSH probes.
    """
    if cfg is None:
        config_key = ""
    else:
        config_key = cfg.model_dump_json(exclude_none=True, exclude_defaults=False)
    root_key = str(root.resolve()) if root else ""
    return config_key, root_key, fast, os.environ.get("HOMELAB_NODE", "").strip()


def _cache_store(
    cap: HostCapacity,
    *,
    cfg: Config | None,
    root: Path | None,
    fast: bool,
    cacheable: bool,
) -> HostCapacity:
    if cacheable and cfg is not None:
        _CAP_CACHE[_capacity_cache_key(cfg, root, fast)] = cap
    return cap


@dataclass(frozen=True, slots=True)
class HostCapacity:
    cpu_cores: int
    mem_total_mb: int
    load_1m: float
    wave_timeout_s: int
    inter_wave_sleep_s: int
    max_pull_parallel: int
    load_threshold: float
    source: str = "local"

    @property
    def overloaded(self) -> bool:
        return self.load_1m > self.load_threshold

    def warning_message(self) -> str | None:
        if not self.overloaded:
            return None
        return (
            f"Load average {self.load_1m:.1f} exceeds {self.load_threshold:.1f} "
            f"({self.cpu_cores} cores) — deploy may be slow; consider waiting"
        )


def _read_loadavg() -> float:
    try:
        return float(Path("/proc/loadavg").read_text().split()[0])
    except OSError:
        return 0.0


def _read_mem_total_kb() -> int:
    try:
        for line in Path("/proc/meminfo").read_text().splitlines():
            if line.startswith("MemTotal:"):
                return int(line.split()[1])
    except OSError:
        pass
    return 0


def _cpu_cores() -> int:
    try:
        return os.cpu_count() or 2
    except TypeError:
        return 2


def _capacity_from_raw(
    cores: int,
    mem_kb: int,
    load_1m: float,
    *,
    source: str = "local",
) -> HostCapacity:
    mem_mb = mem_kb // 1024
    threshold = float(cores * 2)

    if mem_mb < 4096:
        wave_timeout = 180
        inter_sleep = 8
        max_pull = 2
    elif mem_mb < 8192:
        wave_timeout = 150
        inter_sleep = 6
        max_pull = 3
    else:
        wave_timeout = 180
        inter_sleep = 4
        max_pull = 4

    if cores <= 2:
        wave_timeout += 30
        inter_sleep += 2
        max_pull = max(1, max_pull - 1)

    if load_1m > threshold:
        inter_sleep += 10
        wave_timeout += 30

    wave_timeout = max(180, wave_timeout)

    return HostCapacity(
        cpu_cores=cores,
        mem_total_mb=mem_mb,
        load_1m=load_1m,
        wave_timeout_s=wave_timeout,
        inter_wave_sleep_s=inter_sleep,
        max_pull_parallel=max_pull,
        load_threshold=threshold,
        source=source,
    )


def configured_capacity_estimate(cfg: Config) -> HostCapacity | None:
    """Return an offline capacity estimate when both operator overrides exist.

    Dry-run planning must not contact a Proxmox host.  A partial capacity override
    is intentionally not completed from the controller's resources because that
    would make a remote plan misleading.
    """
    capacity = cfg.host_capacity
    if capacity.cpu_cores is None or capacity.mem_total_mb is None:
        return None

    estimate = _capacity_from_raw(
        capacity.cpu_cores,
        capacity.mem_total_mb * 1024,
        0.0,
        source="configured-offline",
    )
    if capacity.load_threshold is None:
        return estimate
    return HostCapacity(
        cpu_cores=estimate.cpu_cores,
        mem_total_mb=estimate.mem_total_mb,
        load_1m=estimate.load_1m,
        wave_timeout_s=estimate.wave_timeout_s,
        inter_wave_sleep_s=estimate.inter_wave_sleep_s,
        max_pull_parallel=estimate.max_pull_parallel,
        load_threshold=float(capacity.load_threshold),
        source=estimate.source,
    )


def detect_host_capacity(
    *,
    cpu_cores: int | None = None,
    mem_total_kb: int | None = None,
    load_1m: float | None = None,
    cfg: Config | None = None,
    root: Path | None = None,
    fast: bool = True,
) -> HostCapacity:
    """Read capacity from Proxmox host (remote deploy), config overrides, or local /proc.

    When ``fast`` is true (default on the controller), SSH probes use short timeouts
    and fall back to conservative defaults instead of blocking generate/deploy.
    """
    cacheable = cpu_cores is None and mem_total_kb is None and load_1m is None
    if cacheable:
        cache_key = _capacity_cache_key(cfg, root, fast)
        if cache_key in _CAP_CACHE:
            return _CAP_CACHE[cache_key]

    hc = getattr(cfg, "host_capacity", None) if cfg is not None else None

    vm_role = os.environ.get("HOMELAB_NODE", "").strip()
    lxc_cores_env = os.environ.get("HOMELAB_NODE_CORES", "").strip()
    lxc_mem_env = os.environ.get("HOMELAB_NODE_MEM_MB", "").strip()
    if vm_role and lxc_cores_env.isdigit():
        cores = int(lxc_cores_env)
        mem_kb = int(lxc_mem_env) * 1024 if lxc_mem_env.isdigit() else _read_mem_total_kb()
        la = _read_loadavg()
        return _cache_store(
            _capacity_from_raw(cores, mem_kb, la, source=f"lxc-{vm_role}"),
            cfg=cfg,
            root=root,
            fast=fast,
            cacheable=cacheable,
        )

    if root is not None and vm_role:
        env_file = root / "generated" / vm_role / ".env"
        if env_file.is_file():
            env_map: dict[str, str] = {}
            for line in env_file.read_text().splitlines():
                if "=" in line and not line.strip().startswith("#"):
                    k, _, v = line.partition("=")
                    env_map[k.strip()] = v.strip().strip('"').strip("'")
            lc = env_map.get("HOMELAB_NODE_CORES", "")
            lm = env_map.get("HOMELAB_NODE_MEM_MB", "")
            if lc.isdigit():
                mem_kb = int(lm) * 1024 if lm.isdigit() else _read_mem_total_kb()
                return _cache_store(
                    _capacity_from_raw(int(lc), mem_kb, _read_loadavg(), source=f"env-{vm_role}"),
                    cfg=cfg,
                    root=root,
                    fast=fast,
                    cacheable=cacheable,
                )

    if vm_role:
        # Running on a guest: local /proc values ARE this LXC's resources.
        # Never fail a guest deploy just because resource hints are missing.
        return _cache_store(
            _capacity_from_raw(_cpu_cores(), _read_mem_total_kb(), _read_loadavg(), source=f"local-{vm_role}"),
            cfg=cfg,
            root=root,
            fast=fast,
            cacheable=cacheable,
        )

    if cpu_cores is not None or mem_total_kb is not None or load_1m is not None:
        cores = cpu_cores if cpu_cores is not None else _cpu_cores()
        mem_kb = mem_total_kb if mem_total_kb is not None else _read_mem_total_kb()
        la = load_1m if load_1m is not None else _read_loadavg()
        cap = _capacity_from_raw(cores, mem_kb, la, source="injected")
        if hc and hc.load_threshold is not None:
            return HostCapacity(
                cpu_cores=cap.cpu_cores,
                mem_total_mb=cap.mem_total_mb,
                load_1m=cap.load_1m,
                wave_timeout_s=cap.wave_timeout_s,
                inter_wave_sleep_s=cap.inter_wave_sleep_s,
                max_pull_parallel=cap.max_pull_parallel,
                load_threshold=float(hc.load_threshold),
                source=cap.source,
            )
        return cap

    if hc and (hc.cpu_cores is not None or hc.mem_total_mb is not None):
        cores = hc.cpu_cores if hc.cpu_cores is not None else _cpu_cores()
        mem_kb = (hc.mem_total_mb * 1024) if hc.mem_total_mb is not None else _read_mem_total_kb()
        la = _read_loadavg()
        cap = _capacity_from_raw(cores, mem_kb, la, source="config")
        if hc.load_threshold is not None:
            return HostCapacity(
                cpu_cores=cap.cpu_cores,
                mem_total_mb=cap.mem_total_mb,
                load_1m=cap.load_1m,
                wave_timeout_s=cap.wave_timeout_s,
                inter_wave_sleep_s=cap.inter_wave_sleep_s,
                max_pull_parallel=cap.max_pull_parallel,
                load_threshold=float(hc.load_threshold),
                source=cap.source,
            )
        return cap

    use_remote = (
        cfg is not None
        and cfg.proxmox.provision_machines
        and hc is not None
        and hc.use_proxmox_host
        and not os.environ.get("HOMELAB_NODE")
    )
    if use_remote and cfg is not None:
        probe_retries = 1 if fast else cfg.ssh.retries
        connect_timeout = 6 if fast else cfg.ssh.connect_timeout
        command_timeout = 10 if fast else cfg.ssh.command_timeout
        remote = detect_proxmox_capacity(
            cfg,
            root,
            retries=probe_retries,
            connect_timeout=connect_timeout,
            command_timeout=command_timeout,
        )
        if remote is not None:
            cap = _capacity_from_raw(*remote, source="proxmox")
            if hc and hc.load_threshold is not None:
                return _cache_store(
                    HostCapacity(
                        cpu_cores=cap.cpu_cores,
                        mem_total_mb=cap.mem_total_mb,
                        load_1m=cap.load_1m,
                        wave_timeout_s=cap.wave_timeout_s,
                        inter_wave_sleep_s=cap.inter_wave_sleep_s,
                        max_pull_parallel=cap.max_pull_parallel,
                        load_threshold=float(hc.load_threshold),
                        source=cap.source,
                    ),
                    cfg=cfg,
                    root=root,
                    fast=fast,
                    cacheable=cacheable,
                )
            return _cache_store(cap, cfg=cfg, root=root, fast=fast, cacheable=cacheable)

        lxc = detect_lxc_capacity(
            cfg,
            root,
            max_hosts=1 if fast else None,
            retries=1 if fast else cfg.ssh.retries,
            timeout=10 if fast else cfg.ssh.command_timeout,
        )
        if lxc is not None:
            cap = _capacity_from_raw(*lxc, source="lxc")
            if hc and hc.load_threshold is not None:
                return _cache_store(
                    HostCapacity(
                        cpu_cores=cap.cpu_cores,
                        mem_total_mb=cap.mem_total_mb,
                        load_1m=cap.load_1m,
                        wave_timeout_s=cap.wave_timeout_s,
                        inter_wave_sleep_s=cap.inter_wave_sleep_s,
                        max_pull_parallel=cap.max_pull_parallel,
                        load_threshold=float(hc.load_threshold),
                        source=cap.source,
                    ),
                    cfg=cfg,
                    root=root,
                    fast=fast,
                    cacheable=cacheable,
                )
            return _cache_store(cap, cfg=cfg, root=root, fast=fast, cacheable=cacheable)

        from toolkit.core.infra.proxmox_ssh import configured_proxmox_ssh_key

        if fast:
            return _cache_store(
                _capacity_from_raw(_cpu_cores(), _read_mem_total_kb(), _read_loadavg(), source="local-fast-fallback"),
                cfg=cfg,
                root=root,
                fast=fast,
                cacheable=cacheable,
            )

        host = resolve_proxmox_host(cfg, root)
        ssh_user = cfg.proxmox.ssh.user
        ssh_port = cfg.proxmox.ssh.port
        key = configured_proxmox_ssh_key(cfg, root)
        raise RuntimeError(
            f"Cannot detect host capacity: SSH connection to Proxmox host failed.\n"
            f"  Host: {host or '(unresolved)'}\n"
            f"  Command: ssh -i {key} -p {ssh_port} {ssh_user}@{host or '<host>'} "
            f"'{_REMOTE_CAPACITY_CMD}'\n"
            f"Troubleshooting:\n"
            f"  1. Verify the Proxmox host is reachable: ping {host or '<proxmox-ip>'}\n"
            f"  2. Check Proxmox SSH credentials (proxmox.ssh in config.local.yaml)\n"
            f"  3. Test manually: ssh -i <key_file> -p {ssh_port} <user>@{host or '<proxmox-ip>'} nproc\n"
            f"  4. To skip auto-detection, set explicit capacity in config.yaml:\n"
            f"     host_capacity:\n"
            f"       cpu_cores: <N>\n"
            f"       mem_total_mb: <N>"
        )

    raise RuntimeError(
        "Cannot detect host capacity: auto-detection requires SSH access to the Proxmox host.\n"
        "  Either enable proxmox.provision_machines (and ensure SSH connectivity) or\n"
        "  set explicit capacity overrides in config.yaml:\n"
        "    host_capacity:\n"
        "      cpu_cores: <N>\n"
        "      mem_total_mb: <N>"
    )


def resolve_proxmox_host(cfg: Config, root: Path | None = None) -> str:
    """Resolve Proxmox host IP for SSH capacity probes."""
    from toolkit.core.infra.proxmox_ssh import resolve_proxmox_control_host

    resolved = resolve_proxmox_control_host(cfg)
    if resolved:
        return resolved

    if root is not None:
        generated = root / "automation" / "ansible" / "group_vars" / "generated.yml"
        if generated.is_file():
            for line in generated.read_text().splitlines():
                m = re.match(r"^proxmox_control_host:\s*['\"]?([^'\"#\s]+)", line.strip())
                if m:
                    return m.group(1)

    return ""


def _parse_capacity_stdout(stdout: str) -> tuple[int, int, float] | None:
    lines = [ln.strip() for ln in stdout.strip().splitlines() if ln.strip()]
    if len(lines) < 3:
        return None
    try:
        return int(lines[0]), int(lines[1]), float(lines[2])
    except ValueError:
        return None


_REMOTE_CAPACITY_CMD = "nproc; awk '/MemTotal/ {print $2}' /proc/meminfo; awk '{print $1}' /proc/loadavg"


def detect_proxmox_capacity(
    cfg: Config,
    root: Path | None = None,
    *,
    retries: int | None = None,
    connect_timeout: int | None = None,
    command_timeout: int | None = None,
) -> tuple[int, int, float] | None:
    """SSH to Proxmox host and read nproc, MemTotal, loadavg. Returns None on failure."""
    from toolkit.core.infra.proxmox_ssh import build_proxmox_ssh_command

    if retries is None:
        retries = cfg.proxmox.ssh.retries
    if connect_timeout is None:
        connect_timeout = cfg.proxmox.ssh.connect_timeout
    if command_timeout is None:
        command_timeout = cfg.proxmox.ssh.command_timeout

    host = resolve_proxmox_host(cfg, root)
    if not host:
        return None

    try:
        cmd = build_proxmox_ssh_command(
            cfg,
            root,
            _REMOTE_CAPACITY_CMD,
            host=host,
            connect_timeout=connect_timeout,
        )
    except ValueError:
        return None
    import time

    for attempt in range(max(1, retries)):
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=command_timeout, check=False)
        except (OSError, subprocess.TimeoutExpired):
            result = None
        if result is not None and result.returncode == 0:
            parsed = _parse_capacity_stdout(result.stdout)
            if parsed is not None:
                return parsed
        if attempt + 1 < retries:
            time.sleep(min(5 * (attempt + 1), 15))
    return None


def detect_lxc_capacity(
    cfg: Config,
    root: Path | None = None,
    *,
    max_hosts: int | None = None,
    nodes: tuple[str, ...] | None = None,
    retries: int | None = None,
    timeout: int | None = None,
) -> tuple[int, int, float] | None:
    """Probe the first reachable LXC guest for local capacity metrics."""
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

    if retries is None:
        retries = cfg.ssh.retries
    if timeout is None:
        timeout = cfg.ssh.command_timeout

    candidates: list[str] = []
    for node in nodes or tuple(cfg.enabled_nodes):
        if node not in cfg.enabled_nodes:
            raise ValueError(f"capacity probe targets unknown or disabled node {node!r}")
        candidates.append(cfg.node_ip(node))
    if max_hosts is not None:
        candidates = candidates[: max(0, max_hosts)]

    for ip in candidates:
        rc, out, _ = ssh_run_on_vm(
            cfg,
            ip,
            _REMOTE_CAPACITY_CMD,
            root=root,
            timeout=timeout,
            retries=retries,
        )
        if rc != 0:
            continue
        parsed = _parse_capacity_stdout(out)
        if parsed is not None:
            return parsed
    return None


def conservative_fallback_capacity() -> HostCapacity:
    """Safe deploy tuning when Proxmox/LXC probes are unavailable."""
    return _capacity_from_raw(4, 8_000_000, 0.0, source="fallback")


def detect_zfs_storage_capacity(
    cfg: Config,
    root: Path | None = None,
) -> dict | None:
    """Detect ZFS storage capacity from the Proxmox host for LXC resource planning.

    Returns a dict with pool info (name, free_gb, total_gb, raid_level) or None
    if detection fails. Used for deployment pacing and capacity warnings.
    """
    from toolkit.core.infra.zfs_detect import detect_zfs_pools

    result = detect_zfs_pools(cfg, root)
    if not result.ok or not result.pools:
        return None

    primary = next((p for p in result.pools if p.name == result.primary_pool), result.pools[0])
    free_gb = _parse_zfs_size_to_gb(primary.free)
    total_gb = _parse_zfs_size_to_gb(primary.size)

    return {
        "pool": primary.name,
        "free_gb": free_gb,
        "total_gb": total_gb,
        "raid_level": primary.raid_level,
        "health": primary.health,
        "pool_count": len(result.pools),
        "disk_count": len(result.disks),
    }


def _parse_zfs_size_to_gb(size_str: str) -> int:
    """Parse ZFS size string (e.g. '3.62T', '500G') to integer GB."""
    from toolkit.core.infra.zfs_detect import _parse_size_to_gb

    return _parse_size_to_gb(size_str)


@dataclass(frozen=True, slots=True)
class MachineResourcePlan:
    """Effective resource allocation from one machine plugin."""

    node: str
    kind: str
    cores: int
    memory_mb: int
    root_disk_gb: int
    data_gb: int
    service_count: int


def build_machine_resource_plans(
    cfg: Config,
    service_counts: dict[str, int],
) -> dict[str, MachineResourcePlan]:
    """Build plans from the exact resources declared by enabled machine plugins."""
    return {
        node: MachineResourcePlan(
            node=node,
            kind=cfg.machines[node].kind,
            cores=cfg.machines[node].cores,
            memory_mb=cfg.machines[node].memory_mb,
            root_disk_gb=cfg.machines[node].root_disk_gb,
            data_gb=sum(disk.size_gb for disk in cfg.machines[node].data_disks),
            service_count=service_counts.get(node, 0),
        )
        for node in cfg.enabled_nodes
    }


def format_resource_plan(plans: dict[str, MachineResourcePlan]) -> str:
    header = f"  {'Node':<16} {'Kind':<4} {'CPU':>4} {'RAM MB':>8} {'Root GB':>8} {'Data GB':>8} {'Svcs':>5}"
    lines = ["Machine Resource Plan:", header]
    lines.append("  " + "-" * 65)
    total_cores = total_mem = 0
    for node in sorted(plans):
        p = plans[node]
        lines.append(
            f"  {p.node:<16} {p.kind:<4} {p.cores:>4} {p.memory_mb:>8} "
            f"{p.root_disk_gb:>8} {p.data_gb:>8} {p.service_count:>5}"
        )
        total_cores += p.cores
        total_mem += p.memory_mb
    lines.append("  " + "-" * 65)
    lines.append(f"  {'Total':<21} {total_cores:>4} {total_mem:>8}")
    return "\n".join(lines)


def shell_export(cap: HostCapacity | None = None) -> str:
    """Bash `export` lines for staggered compose / deploy tuning."""
    c = cap or detect_host_capacity()
    return "\n".join(
        [
            f"export HOMELAB_CPU_CORES={c.cpu_cores}",
            f"export HOMELAB_MEM_MB={c.mem_total_mb}",
            f"export HOMELAB_LOAD_1M={c.load_1m}",
            f"export WAVE_TIMEOUT={c.wave_timeout_s}",
            f"export INTER_WAVE_SLEEP={c.inter_wave_sleep_s}",
            f"export MAX_PULL_PARALLEL={c.max_pull_parallel}",
            f"export LOAD_THRESHOLD={c.load_threshold}",
            f"export HOMELAB_CAPACITY_SOURCE={c.source}",
        ]
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Host capacity for homelab deploy tuning")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--shell", action="store_true", help="Print bash export statements")
    parser.add_argument("--root", type=Path, default=None, help="Homelab repo root for config")
    args = parser.parse_args(argv)

    cap: HostCapacity
    if args.root and (args.root / "config.yaml").exists():
        from toolkit.core.config.config import load_config

        cfg = load_config(args.root / "config.yaml")
        cap = detect_host_capacity(cfg=cfg, root=args.root)
    else:
        cap = _capacity_from_raw(_cpu_cores(), _read_mem_total_kb(), _read_loadavg(), source="local")

    if args.shell:
        print(shell_export(cap))
    elif args.json:
        print(json.dumps(asdict(cap), indent=2))
    else:
        print(
            f"source={cap.source} cores={cap.cpu_cores} mem_mb={cap.mem_total_mb} "
            f"load={cap.load_1m:.2f} wave_timeout={cap.wave_timeout_s}s "
            f"inter_wave={cap.inter_wave_sleep_s}s"
        )
        msg = cap.warning_message()
        if msg:
            print(f"WARNING: {msg}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
