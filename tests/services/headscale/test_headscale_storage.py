from __future__ import annotations

from pathlib import Path

import yaml
from toolkit.core.config.config import Config
from toolkit.core.manifest.catalog import load_service_catalog
from toolkit.core.manifest.databases import compile_database_bindings

ROOT = Path(__file__).resolve().parents[3]


def test_headscale_owns_one_durable_sqlite_store_without_external_database_credentials() -> None:
    manifest = load_service_catalog().require("headscale")
    compose = yaml.safe_load((ROOT / "toolkit/services/headscale/compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["headscale"]

    assert manifest.depends_on == ("authelia", "caddy")
    assert {secret.name for secret in manifest.required_secrets} == {"HEADSCALE_OIDC_CLIENT_SECRET"}
    assert service["depends_on"] == {"authelia": {"condition": "service_healthy"}}
    assert set(service["environment"]) == {"TZ"}
    assert service["volumes"] == [
        "${HEADSCALE_DATA_SOURCE:-./data/headscale}:/var/lib/headscale",
        "${HEADSCALE_CONFIG_SOURCE:-./generated/headscale}:/etc/headscale:ro",
    ]
    assert manifest.data_specs[0].snapshot is False
    assert len(manifest.backup_exports) == 1
    assert manifest.backup_exports[0].artifact == "headscale.sqlite.gz"
    assert manifest.backup_exports[0].database_path == "db.sqlite"
    assert all(binding.service != "headscale" for binding in compile_database_bindings(Config()))
    assert manifest.operator_bookmark is None
    assert {metric.key for metric in manifest.management.metrics} == {
        "registered_nodes",
        "online_nodes",
        "users",
    }
    assert manifest.management.resources[0].key == "mesh_nodes"
