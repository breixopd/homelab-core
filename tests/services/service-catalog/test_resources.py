from __future__ import annotations

from toolkit.core.config.config import Config, StorageConfig
from toolkit.core.config.service_metadata import get_service_memory_tier
from toolkit.core.generate.resources import calculate_service_limits
from toolkit.core.infra.host_capacity import build_machine_resource_plans, format_resource_plan
from toolkit.core.machines import MachineSpec


def test_machine_resource_plans_use_declared_plugin_resources() -> None:
    cfg = Config()

    plans = build_machine_resource_plans(cfg, {"infra": 12, "media": 8, "apps": 6})

    assert plans["infra"].cores == cfg.machines["infra"].cores
    assert plans["infra"].memory_mb == cfg.machines["infra"].memory_mb
    assert plans["media"].data_gb == sum(disk.size_gb for disk in cfg.machines["media"].data_disks)
    assert plans["apps"].service_count == 6


def test_machine_resource_plans_include_arbitrary_machine_ids() -> None:
    cfg = Config()
    machines = dict(cfg.machines)
    machines["worker-east"] = MachineSpec(
        kind="vm",
        hostname="worker-east",
        address="10.10.10.25",
        gateway="10.10.10.1",
        vmid=825,
        admin_user="debian",
        cloud_image_datastore="local",
        cloud_image_format="qcow2",
        cloud_image_url="https://cloud.debian.org/images/cloud/bookworm/latest/debian-12-generic-amd64.qcow2",
        cloud_image_sha256="a" * 64,
        cores=7,
        memory_mb=12_345,
        root_disk_gb=77,
    )
    cfg = Config(machines=machines)

    plan = build_machine_resource_plans(cfg, {"worker-east": 4})["worker-east"]

    assert (plan.kind, plan.cores, plan.memory_mb, plan.root_disk_gb) == ("vm", 7, 12_345, 77)
    assert "worker-east" in format_resource_plan({"worker-east": plan})


def test_service_limits_tier_budget() -> None:
    result = calculate_service_limits(
        21_504,
        6,
        ["postgres", "grafana", "sonarr", "radarr", "tautulli", "caddy"],
    )
    heavy = int(result["postgres"]["mem_limit"].rstrip("m"))
    medium = int(result["grafana"]["mem_limit"].rstrip("m"))
    light = int(result["sonarr"]["mem_limit"].rstrip("m"))
    assert heavy > medium > light


def test_service_limits_manifest_floors() -> None:
    result = calculate_service_limits(
        12_288,
        6,
        ["wazuh-dashboard", "wazuh-indexer", "komodo-mongo", "recyclarr", "flaresolverr", "tdarr", "caddy"],
    )

    assert int(result["wazuh-dashboard"]["mem_limit"].rstrip("m")) >= 1_024
    assert int(result["komodo-mongo"]["mem_limit"].rstrip("m")) >= 384
    assert int(result["recyclarr"]["mem_limit"].rstrip("m")) >= 256
    assert int(result["flaresolverr"]["mem_limit"].rstrip("m")) >= 1_024
    assert float(result["tdarr"]["cpus"]) >= 0.5


def test_runtime_service_inherits_owner_resource_tier() -> None:
    assert get_service_memory_tier("qbittorrent-vpn") == "light"


def test_storage_capacity_math() -> None:
    assert StorageConfig(raw_disks_gb=4_000, disk_count=2, raid_level="mirror").usable_gb == 1_960
    assert StorageConfig(raw_disks_gb=8_000, disk_count=4, raid_level="raidz1").usable_gb == 5_880
    assert StorageConfig(raw_disks_gb=12_000, disk_count=6, raid_level="raidz2").usable_gb == 7_840
    assert StorageConfig(raw_disks_gb=4_000, disk_count=2, raid_level="stripe").usable_gb == 3_920
    assert StorageConfig(raw_disks_gb=2_000, disk_count=1, raid_level="none", filesystem="ext4").usable_gb == 1_900
    assert StorageConfig(raw_disks_gb=0).usable_gb == 0
