"""Auto-detect ZFS pools on the Proxmox host via SSH.

Used at deploy time when config.yaml has no ``storage`` section or it is empty,
so the user doesn't have to manually discover and enter ZFS configuration.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


@dataclass
class ZfsDataset:
    name: str
    used: str
    available: str
    dtype: str  # filesystem, volume, snapshot


@dataclass
class ZfsPool:
    name: str
    size: str
    allocated: str
    free: str
    health: str
    raid_level: str  # single, mirror, raidz1, raidz2, raidz3, stripe
    datasets: list[ZfsDataset] = field(default_factory=list)


@dataclass
class PhysicalDisk:
    name: str
    size: str
    dtype: str  # disk, part
    transport: str  # sata, nvme, usb, etc.


@dataclass
class ZfsDetectionResult:
    pools: list[ZfsPool] = field(default_factory=list)
    disks: list[PhysicalDisk] = field(default_factory=list)
    primary_pool: str = ""  # The pool most likely used for homelab storage
    errors: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.errors) == 0 and len(self.pools) > 0

    def to_storage_config(self) -> dict:
        """Convert detection results into StorageConfig-compatible dict."""
        if not self.pools:
            return {}

        primary = next((p for p in self.pools if p.name == self.primary_pool), self.pools[0])

        # Count disks contributing to the primary pool
        disk_names: list[str] = []
        # Try to match disks to the pool based on common naming conventions
        for disk in self.disks:
            if disk.dtype == "disk" and disk.transport != "usb":
                disk_names.append(disk.name)
        if not disk_names:
            disk_names = [d.name for d in self.disks if d.dtype == "disk"]

        # Calculate raw disk size from pool size
        raw_gb = _parse_size_to_gb(primary.size)
        if raw_gb <= 0 and self.disks:
            # Fall back to summing disk sizes
            raw_gb = sum(_parse_size_to_gb(d.size) for d in self.disks if d.dtype == "disk")

        config: dict = {
            "zfs_enabled": True,
            "zfs_pool": primary.name,
            "zfs_disk_list": ",".join(disk_names) if disk_names else "sda,sdb",
            "filesystem": "zfs",
            "raid_level": primary.raid_level,
            "raw_disks_gb": raw_gb,
            "disk_count": len(disk_names) if disk_names else 1,
            "zfs_overhead_pct": 2.0,
            "media_mount": None,
        }
        return config


def _parse_size_to_gb(size_str: str) -> int:
    """Parse a ZFS/lsblk size string (e.g. '3.62T', '500G', '1.8T') to integer GB."""
    size_str = size_str.strip().upper()
    multipliers = {"T": 1024, "G": 1, "M": 1 / 1024, "K": 1 / (1024 * 1024)}
    for suffix, mult in multipliers.items():
        if size_str.endswith(suffix):
            try:
                return int(float(size_str[:-1]) * mult)
            except ValueError:
                return 0
    try:
        return int(float(size_str))
    except ValueError:
        return 0


def _resolve_proxmox_host_for_zfs(cfg: Config, root: Path | None = None) -> str:
    """Resolve Proxmox host IP for SSH ZFS probes."""
    from toolkit.core.infra.host_capacity import resolve_proxmox_host

    return resolve_proxmox_host(cfg, root)


def _ssh_proxmox(
    cfg: Config,
    root: Path | None,
    command: str,
    *,
    connect_timeout: int | None = None,
    command_timeout: int | None = None,
) -> tuple[int, str, str]:
    """SSH to Proxmox host and run a command. Returns (returncode, stdout, stderr)."""
    from toolkit.core.infra.proxmox_ssh import build_proxmox_ssh_command

    if connect_timeout is None:
        connect_timeout = cfg.proxmox.ssh.connect_timeout
    if command_timeout is None:
        command_timeout = cfg.proxmox.ssh.command_timeout

    host = _resolve_proxmox_host_for_zfs(cfg, root)
    if not host:
        return 255, "", "no Proxmox host resolved"

    try:
        cmd = build_proxmox_ssh_command(
            cfg,
            root,
            command,
            host=host,
            connect_timeout=connect_timeout,
        )
    except ValueError as exc:
        return 255, "", str(exc)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=command_timeout, check=False)
        return result.returncode, result.stdout, result.stderr
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 255, "", str(exc)


def detect_zfs_pools(cfg: Config, root: Path | None = None) -> ZfsDetectionResult:
    """SSH to the Proxmox host and detect all ZFS pools, datasets, and physical disks.

    Args:
        cfg: Homelab config with Proxmox host and SSH credentials.
        root: Repository root for resolving paths.

    Returns:
        ZfsDetectionResult with pools, datasets, disks, and any errors encountered.
    """
    result = ZfsDetectionResult()
    host = _resolve_proxmox_host_for_zfs(cfg, root)
    if not host:
        result.errors.append("no Proxmox host resolved — check config.yaml proxmox/api_url or dns/public_ip")
        return result

    # ── 1. Discover ZFS pools ──
    rc, stdout, stderr = _ssh_proxmox(
        cfg,
        root,
        "zpool list -H -o name,size,alloc,free,health 2>/dev/null",
    )
    if rc != 0:
        result.errors.append(f"zpool list failed: {stderr.strip() or f'exit {rc}'}")
        return result
    if not stdout.strip():
        result.errors.append("no ZFS pools found on Proxmox host")
        return result

    pool_names: list[str] = []
    for line in stdout.strip().splitlines():
        parts = line.split()
        if len(parts) < 5:
            continue
        name = parts[0]
        pool_names.append(name)
        pool = ZfsPool(
            name=name,
            size=parts[1],
            allocated=parts[2],
            free=parts[3],
            health=parts[4],
            raid_level="single",
        )
        result.pools.append(pool)

    if not result.pools:
        result.errors.append("no parsable ZFS pool entries")
        return result

    # ── 2. Detect RAID level per pool via zpool status ──
    rc, stdout, stderr = _ssh_proxmox(
        cfg,
        root,
        "zpool status 2>/dev/null",
    )
    if rc == 0 and stdout.strip():
        result.errors.append(_parse_zpool_status_for_raid(stdout, result.pools))

    # ── 3. Discover ZFS datasets ──
    rc, stdout, stderr = _ssh_proxmox(
        cfg,
        root,
        "zfs list -H -o name,used,available,type 2>/dev/null",
    )
    if rc == 0 and stdout.strip():
        for line in stdout.strip().splitlines():
            parts = line.split()
            if len(parts) < 4:
                continue
            ds = ZfsDataset(
                name=parts[0],
                used=parts[1],
                available=parts[2],
                dtype=parts[3],
            )
            # Attach dataset to its parent pool
            pool_name = ds.name.split("/")[0]
            for pool in result.pools:
                if pool.name == pool_name:
                    pool.datasets.append(ds)
                    break

    # ── 4. Discover physical disks ──
    rc, stdout, stderr = _ssh_proxmox(
        cfg,
        root,
        "lsblk -dn -o NAME,SIZE,TYPE,TRAN 2>/dev/null",
    )
    if rc == 0 and stdout.strip():
        for line in stdout.strip().splitlines():
            parts = line.split()
            if len(parts) < 3:
                continue
            transport = parts[3] if len(parts) > 3 else ""
            result.disks.append(
                PhysicalDisk(
                    name=parts[0],
                    size=parts[1],
                    dtype=parts[2],
                    transport=transport,
                )
            )

    # ── 5. Determine primary pool ──
    # Preference: "data" → "tank" → largest free space
    if result.pools:
        data_pool = next((p for p in result.pools if p.name == "data"), None)
        if data_pool:
            result.primary_pool = data_pool.name
        else:
            tank_pool = next((p for p in result.pools if p.name == "tank"), None)
            if tank_pool:
                result.primary_pool = tank_pool.name
            else:
                # Pick the pool with the most free space
                best = max(result.pools, key=lambda p: _parse_size_to_gb(p.free))
                result.primary_pool = best.name

    return result


def _parse_zpool_status_for_raid(status_output: str, pools: list[ZfsPool]) -> str:
    """Parse `zpool status` output to determine RAID level for each pool.

    Returns a warning string if parsing was incomplete, empty string on success.
    """
    current_pool: str | None = None
    pool_config_lines: dict[str, list[str]] = {}

    for line in status_output.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        # Detect pool header: "  pool: <name>"
        if stripped.startswith("pool:"):
            current_pool = stripped.split(":", 1)[1].strip()
            if current_pool not in pool_config_lines:
                pool_config_lines[current_pool] = []
        elif current_pool and ("mirror" in stripped or "raidz" in stripped):
            pool_config_lines.setdefault(current_pool, []).append(stripped)

    for pool in pools:
        lines = pool_config_lines.get(pool.name, [])
        if not lines:
            pool.raid_level = "single"
            continue

        # Check for RAID level indicators in the config lines
        raidz_levels: list[int] = []
        has_mirror = False
        for ln in lines:
            m = re.search(r"raidz(\d?)", ln.lower())
            if m:
                level = int(m.group(1)) if m.group(1) else 1
                raidz_levels.append(level)
            if "mirror" in ln.lower():
                has_mirror = True

        if raidz_levels:
            max_level = max(raidz_levels)
            pool.raid_level = f"raidz{max_level}" if max_level > 1 else "raidz1"
        elif has_mirror:
            pool.raid_level = "mirror"
        else:
            pool.raid_level = "stripe"

    return ""


def detect_and_merge_zfs(
    cfg: Config,
    root: Path | None = None,
    *,
    auto_apply: bool = False,
) -> tuple[Config, ZfsDetectionResult, str]:
    """Detect ZFS pools and merge into config if storage section is empty/missing.

    Args:
        cfg: Current config.
        root: Repository root.
        auto_apply: If True, auto-populate storage config without prompting.

    Returns:
        (possibly_modified_config, detection_result, message)
    """
    from toolkit.core.config.config import StorageConfig

    result = detect_zfs_pools(cfg, root)

    if not result.ok:
        return cfg, result, f"ZFS auto-detection failed: {'; '.join(result.errors)}"

    # Check if storage config is already populated (has meaningful values)
    storage = cfg.storage
    has_existing = (
        storage.zfs_enabled
        or storage.raw_disks_gb > 0
        or (storage.zfs_disk_list and storage.zfs_disk_list != "sda,sdb")
    )

    if has_existing:
        detail = (
            f"Storage config already populated (pool={storage.zfs_pool}, "
            f"raid={storage.raid_level}) — skipping auto-detection"
        )
        return cfg, result, detail

    # Build suggested config
    suggested = result.to_storage_config()
    if not suggested:
        return cfg, result, "No usable storage config derived from ZFS detection"

    if auto_apply:
        new_storage = StorageConfig(**suggested)
        cfg = cfg.model_copy(update={"storage": new_storage})
        pool_name = new_storage.zfs_pool
        raid = new_storage.raid_level
        gb = new_storage.usable_gb
        return cfg, result, f"Auto-applied ZFS storage config: pool={pool_name}, raid={raid}, usable={gb}GB"
    else:
        # Return the suggested config without modifying — caller handles prompt
        msg_parts = [
            f"Detected ZFS pool: {result.primary_pool} ({suggested['raid_level']}, {suggested['raw_disks_gb']}GB raw)",
        ]
        for pool in result.pools:
            msg_parts.append(f"  • {pool.name}: {pool.size} ({pool.free} free, {pool.health}, {pool.raid_level})")
        for pool in result.pools:
            for ds in pool.datasets:
                if ds.dtype == "filesystem":
                    msg_parts.append(f"    └ {ds.name} ({ds.used} used)")
        return cfg, result, "\n".join(msg_parts)
