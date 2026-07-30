"""Canonical service manifest catalog."""

from toolkit.core.manifest.catalog import ServiceCatalog, load_service_catalog
from toolkit.core.manifest.oidc import OIDCClientConfig, compile_oidc_clients
from toolkit.core.manifest.routes import CompiledRoute, compile_routes
from toolkit.core.manifest.schema import ServiceManifest
from toolkit.core.manifest.storage import CompiledStorageAsset, StorageInventory, compile_storage_inventory

__all__ = [
    "CompiledRoute",
    "CompiledStorageAsset",
    "OIDCClientConfig",
    "ServiceCatalog",
    "ServiceManifest",
    "StorageInventory",
    "compile_oidc_clients",
    "compile_routes",
    "compile_storage_inventory",
    "load_service_catalog",
]
