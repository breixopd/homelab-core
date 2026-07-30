"""Resolve manifest-owned storage into a concrete per-node inventory."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from toolkit.core.config.config import Config
from toolkit.core.config.storage import env_path
from toolkit.core.manifest.catalog import ServiceCatalog, load_service_catalog
from toolkit.core.manifest.routes import service_is_enabled
from toolkit.core.manifest.schema import NodeId

StorageSourceKind = Literal["bind", "volume"]


class StorageInventoryError(RuntimeError):
    """Raised when generated runtime data cannot resolve a storage contract."""


@dataclass(frozen=True, slots=True)
class CompiledStorageAsset:
    service: str
    service_label: str
    role: NodeId
    name: str
    source_kind: StorageSourceKind
    source: str
    target: str
    host_path: Path | None
    size_estimate_gb: int
    snapshot: bool
    manage_permissions: bool
    host_uid: int
    host_gid: int


@dataclass(frozen=True, slots=True)
class StorageInventory:
    assets: tuple[CompiledStorageAsset, ...]

    @property
    def roles(self) -> tuple[NodeId, ...]:
        return tuple(sorted({asset.role for asset in self.assets}))

    @property
    def snapshot_size_estimate_gb(self) -> int:
        return sum(asset.size_estimate_gb for asset in self.assets if asset.snapshot)

    def for_role(self, role: NodeId) -> tuple[CompiledStorageAsset, ...]:
        return tuple(asset for asset in self.assets if asset.role == role)


def read_role_environment(path: Path) -> dict[str, str]:
    """Read a generated Compose environment without mutating process state."""
    if not path.is_file():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        name, value = stripped.split("=", 1)
        values[name] = value
    return values


def compile_storage_inventory(
    cfg: Config,
    root: Path,
    *,
    catalog: ServiceCatalog | None = None,
    roles: set[NodeId] | None = None,
) -> StorageInventory:
    """Resolve enabled storage declarations against generated role environments."""
    selected = catalog or load_service_catalog(root)
    from toolkit.core.manifest.placement import manifest_storage_nodes

    environments: dict[NodeId, dict[str, str]] = {}
    assets: list[CompiledStorageAsset] = []
    for manifest in selected.manifests:
        if not service_is_enabled(cfg, manifest):
            continue
        for asset in manifest.data_specs:
            for node in manifest_storage_nodes(cfg, manifest, asset.runtime_service):
                if roles is not None and node not in roles:
                    continue
                if node not in environments:
                    environments[node] = read_role_environment(env_path(node, root))
                host_path: Path | None = None
                if asset.source_env is not None:
                    value = environments[node].get(asset.source_env, "").strip()
                    if not value:
                        raise StorageInventoryError(
                            f"service {manifest.name!r} storage source {asset.source_env!r} is missing "
                            f"from generated/{node}/.env"
                        )
                    host_path = Path(value)
                    if not host_path.is_absolute():
                        raise StorageInventoryError(
                            f"service {manifest.name!r} storage source {asset.source_env!r} "
                            "must resolve to an absolute path"
                        )
                    if asset.source_subpath:
                        host_path /= asset.source_subpath
                    source_kind: StorageSourceKind = "bind"
                    source = asset.source_env
                else:
                    source_kind = "volume"
                    source = asset.volume or ""
                assets.append(
                    CompiledStorageAsset(
                        service=manifest.name,
                        service_label=manifest.label,
                        role=node,
                        name=asset.name,
                        source_kind=source_kind,
                        source=source,
                        target=asset.target,
                        host_path=host_path,
                        size_estimate_gb=asset.size_estimate_gb,
                        snapshot=asset.snapshot,
                        manage_permissions=asset.manage_permissions,
                        host_uid=asset.host_uid,
                        host_gid=asset.host_gid,
                    )
                )
    return StorageInventory(tuple(assets))
