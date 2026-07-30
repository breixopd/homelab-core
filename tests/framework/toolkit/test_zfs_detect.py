from __future__ import annotations

from toolkit.core.infra.zfs_detect import (
    PhysicalDisk,
    ZfsDataset,
    ZfsDetectionResult,
    ZfsPool,
    _parse_size_to_gb,
    _parse_zpool_status_for_raid,
)


def test_parse_size_to_gb_units():
    assert _parse_size_to_gb("3.62T") == int(3.62 * 1024)
    assert _parse_size_to_gb("500G") == 500
    assert _parse_size_to_gb("512M") == 0
    assert _parse_size_to_gb("bad") == 0


def test_zfs_detection_result_ok_requires_pools():
    empty = ZfsDetectionResult(errors=["ssh failed"])
    assert not empty.ok
    with_pool = ZfsDetectionResult(
        pools=[ZfsPool("tank", "1T", "100G", "900G", "ONLINE", "mirror")],
        primary_pool="tank",
    )
    assert with_pool.ok


def test_to_storage_config_picks_primary_pool():
    pool = ZfsPool(
        name="zfs-mirror",
        size="2T",
        allocated="200G",
        free="1.8T",
        health="ONLINE",
        raid_level="mirror",
        datasets=[ZfsDataset("zfs-mirror/homelab", "10G", "1.9T", "filesystem")],
    )
    disks = [PhysicalDisk("sda", "1T", "disk", "sata"), PhysicalDisk("sdb", "1T", "disk", "sata")]
    result = ZfsDetectionResult(pools=[pool], disks=disks, primary_pool="zfs-mirror")
    cfg = result.to_storage_config()
    assert cfg["zfs_enabled"] is True
    assert cfg["zfs_pool"] == "zfs-mirror"
    assert cfg["raid_level"] == "mirror"
    assert "sda" in cfg["zfs_disk_list"]


def test_parse_zpool_status_for_raid_mirror():
    pools = [ZfsPool("tank", "1T", "0", "1T", "ONLINE", "stripe")]
    status = """
  pool: tank
    state: ONLINE
    mirror-0
      sda
      sdb
"""
    warning = _parse_zpool_status_for_raid(status, pools)
    assert warning == ""
    assert pools[0].raid_level == "mirror"
