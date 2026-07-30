"""Compile service-owned generated artifacts into node-scoped transfer records."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.manifest.catalog import ServiceCatalog


@dataclass(frozen=True, slots=True)
class CompiledGeneratedArtifact:
    path: str
    service: str
    node: str
    enabled: bool
    kind: str
    mode: str
    sensitive: bool
    host_uid: int = 0
    host_gid: int = 0

    @property
    def source_path(self) -> str:
        return f"generated/{self.path}"

    def as_ansible_vars(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class CompiledConfigSource:
    """A service-owned path below ``config/`` and its runtime owner node."""

    path: str
    service: str
    node: str
    enabled: bool

    def as_ansible_vars(self) -> dict[str, object]:
        return asdict(self)


def compile_generated_artifacts(
    config: Config,
    catalog: ServiceCatalog,
    root: Path | None = None,
) -> tuple[CompiledGeneratedArtifact, ...]:
    """Resolve every declared artifact without reading or materializing secrets."""
    from toolkit.core.manifest.placement import manifest_node, manifest_runtime_nodes, resolve_node_selector
    from toolkit.core.manifest.routes import service_is_enabled

    compiled: list[CompiledGeneratedArtifact] = []
    for manifest in catalog.manifests:
        enabled = service_is_enabled(config, manifest, catalog)
        try:
            node = manifest_node(config, manifest)
        except ValueError:
            node = resolve_node_selector(config, manifest.placement, enabled_only=False)
            enabled = False
        for artifact in manifest.generated_artifacts:
            runtime_service = getattr(artifact, "runtime_service", "")
            if runtime_service:
                artifact_nodes = manifest_runtime_nodes(
                    config,
                    manifest,
                    runtime_service,
                    primary_node=node,
                )
            else:
                artifact_nodes = (node,)
            compiled.extend(
                CompiledGeneratedArtifact(
                    path=artifact.path.removeprefix("generated/"),
                    service=manifest.name,
                    node=artifact_node,
                    enabled=enabled,
                    kind=artifact.kind,
                    mode=(
                        artifact.mode or ("0600" if artifact.sensitive else "0500" if artifact.executable else "0644")
                    ),
                    sensitive=artifact.sensitive,
                    host_uid=artifact.host_uid if artifact.host_uid is not None else 0,
                    host_gid=artifact.host_gid if artifact.host_gid is not None else 0,
                )
                for artifact_node in artifact_nodes
            )
    if root is not None:
        from toolkit.core.manifest.ownership import current_ownership, ownership_tombstones

        for item in ownership_tombstones(root, current_ownership(config, catalog)).generated:
            compiled.append(
                CompiledGeneratedArtifact(
                    path=item.path.removeprefix("generated/"),
                    service=item.service,
                    node=item.node,
                    enabled=False,
                    kind="file",
                    mode="0600",
                    sensitive=True,
                    host_uid=0,
                    host_gid=0,
                )
            )
    return tuple(sorted(compiled, key=lambda item: (item.path, item.service, item.node)))


def compile_config_sources(
    config: Config,
    catalog: ServiceCatalog,
    root: Path | None = None,
) -> tuple[CompiledConfigSource, ...]:
    """Resolve manifest-owned ``config/`` sources to their service nodes.

    Only declared paths are scoped; undeclared configuration remains static
    repository content and is intentionally handled by the broad sync.
    Disabled services are retained in the result as ``enabled=False`` so
    consumers can remove stale paths on managed nodes without hardcoding
    service names.
    """
    from toolkit.core.manifest.placement import manifest_node, resolve_node_selector
    from toolkit.core.manifest.routes import service_is_enabled
    from toolkit.core.manifest.variables import compile_manifest_host_sources

    compiled: list[CompiledConfigSource] = []
    for manifest in catalog.manifests:
        enabled = service_is_enabled(config, manifest, catalog)
        try:
            node = manifest_node(config, manifest)
        except ValueError:
            node = resolve_node_selector(config, manifest.placement, enabled_only=False)
            enabled = False
        resolved = compile_manifest_host_sources(config, manifest, ".", catalog=catalog)
        for source_name, source in manifest.host_sources.items():
            active_path = resolved[source_name].removeprefix("./").lstrip("/")
            declared_paths = {source.path, *(variant.path for variant in source.variants)}
            for relative in declared_paths:
                if not relative.startswith("config/"):
                    continue
                compiled.append(
                    CompiledConfigSource(
                        path=relative,
                        service=manifest.name,
                        node=node,
                        enabled=enabled and relative == active_path,
                    )
                )
    if root is not None:
        from toolkit.core.manifest.ownership import current_ownership, ownership_tombstones

        compiled.extend(
            CompiledConfigSource(path=item.path, service=item.service, node=item.node, enabled=False)
            for item in ownership_tombstones(root, current_ownership(config, catalog)).config
        )
    return tuple(sorted(compiled, key=lambda item: (item.path, item.service, item.node)))
