from __future__ import annotations

from pathlib import Path

import pytest
from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.manifest.catalog import ServiceCatalog
from toolkit.core.manifest.schema import ServiceManifest
from toolkit.core.manifest.storage import StorageInventoryError, compile_storage_inventory


def _manifest(*, vm: str = "apps", enabled_when: list[dict[str, object]] | None = None) -> ServiceManifest:
    return ServiceManifest.model_validate(
        {
            "name": "example",
            "label": "Example",
            "description": "Example stateful service",
            "icon": "box",
            "category": "cloud",
            "placement": vm,
            "priority": 10,
            "stateful": True,
            "enabled_when": enabled_when or [],
            "routes": [],
            "data_specs": [
                {
                    "name": "example-data",
                    "source_env": "EXAMPLE_DATA_SOURCE",
                    "target": "/var/lib/example",
                    "size_estimate_gb": 12,
                    "snapshot": True,
                },
                {
                    "name": "example-cache",
                    "volume": "example-cache",
                    "target": "/cache",
                    "size_estimate_gb": 3,
                    "snapshot": False,
                },
            ],
        }
    )


def test_storage_inventory_resolves_role_host_paths(tmp_path: Path) -> None:
    env = tmp_path / "generated" / "apps" / ".env"
    env.parent.mkdir(parents=True)
    env.write_text("EXAMPLE_DATA_SOURCE=/opt/homelab/data/example\n", encoding="utf-8")

    inventory = compile_storage_inventory(
        Config(domain="example.com"),
        tmp_path,
        catalog=ServiceCatalog((_manifest(),)),
    )

    assert inventory.roles == ("apps",)
    assert inventory.snapshot_size_estimate_gb == 12
    assert inventory.assets[0].host_path == Path("/opt/homelab/data/example")
    assert inventory.assets[0].manage_permissions is True
    assert inventory.assets[0].host_uid == 0
    assert inventory.assets[0].host_gid == 0
    assert inventory.assets[1].host_path is None
    assert inventory.assets[1].snapshot is False


def test_storage_inventory_rejects_missing_or_relative_runtime_source(tmp_path: Path) -> None:
    env = tmp_path / "generated" / "apps" / ".env"
    env.parent.mkdir(parents=True)
    env.write_text("EXAMPLE_DATA_SOURCE=relative/data\n", encoding="utf-8")

    with pytest.raises(StorageInventoryError, match="absolute"):
        compile_storage_inventory(Config(), tmp_path, catalog=ServiceCatalog((_manifest(),)))

    env.write_text("OTHER=value\n", encoding="utf-8")
    with pytest.raises(StorageInventoryError, match="EXAMPLE_DATA_SOURCE"):
        compile_storage_inventory(Config(), tmp_path, catalog=ServiceCatalog((_manifest(),)))


def test_storage_inventory_excludes_disabled_services(tmp_path: Path) -> None:
    inventory = compile_storage_inventory(
        Config(services=ServicesConfig(cloud=False)),
        tmp_path,
        catalog=ServiceCatalog((_manifest(enabled_when=[{"path": "services.cloud", "equals": True}]),)),
    )

    assert inventory.assets == ()


def test_storage_inventory_expands_secondary_runtime_assets_to_each_node(tmp_path: Path) -> None:
    raw = _manifest().model_dump(mode="json")
    raw["runtimes"] = {"example-agent": {"placements": ["media", "apps"]}}
    raw["data_specs"] = [
        {
            "name": "agent-cache",
            "source_env": "AGENT_CACHE_SOURCE",
            "target": "/cache",
            "runtime_service": "example-agent",
            "size_estimate_gb": 2,
            "snapshot": False,
        }
    ]
    manifest = ServiceManifest.model_validate(raw)
    for node in ("media", "apps"):
        env = tmp_path / "generated" / node / ".env"
        env.parent.mkdir(parents=True)
        env.write_text(f"AGENT_CACHE_SOURCE=/opt/homelab/{node}/cache\n", encoding="utf-8")

    inventory = compile_storage_inventory(Config(), tmp_path, catalog=ServiceCatalog((manifest,)))

    assert [(asset.role, asset.host_path) for asset in inventory.assets] == [
        ("media", Path("/opt/homelab/media/cache")),
        ("apps", Path("/opt/homelab/apps/cache")),
    ]


def test_storage_inventory_can_compile_one_role_without_other_role_environments(tmp_path: Path) -> None:
    raw = _manifest().model_dump(mode="json")
    raw["runtimes"] = {"example-agent": {"placements": ["media", "apps"]}}
    raw["data_specs"] = [
        {
            "name": "agent-cache",
            "source_env": "AGENT_CACHE_SOURCE",
            "target": "/cache",
            "runtime_service": "example-agent",
            "size_estimate_gb": 2,
            "snapshot": False,
        }
    ]
    manifest = ServiceManifest.model_validate(raw)
    env = tmp_path / "generated" / "apps" / ".env"
    env.parent.mkdir(parents=True)
    env.write_text("AGENT_CACHE_SOURCE=/opt/homelab/apps/cache\n", encoding="utf-8")

    inventory = compile_storage_inventory(
        Config(),
        tmp_path,
        catalog=ServiceCatalog((manifest,)),
        roles={"apps"},
    )

    assert [(asset.role, asset.host_path) for asset in inventory.assets] == [("apps", Path("/opt/homelab/apps/cache"))]
