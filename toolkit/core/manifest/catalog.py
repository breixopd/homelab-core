"""Manifest discovery and repository-wide invariants."""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import ValidationError

from toolkit.core.images.references import parse_immutable_image_reference
from toolkit.core.manifest.schema import ServiceManifest


class ManifestCatalogError(RuntimeError):
    pass


_COMPOSE_ENV_SOURCE = re.compile(r"^\$\{(?P<name>[A-Z][A-Z0-9_]*)(?::-(?P<default>[^}]*))?\}(?:/(?P<subpath>[^:]+))?$")
_COMPOSE_ENV_REFERENCE = re.compile(r"\$\{([A-Z][A-Z0-9_]*)")
_LOG_MAX_SIZE = re.compile(r"^[1-9][0-9]*[kmg]$")


@dataclass(frozen=True, slots=True)
class ComposeMount:
    compose_service: str
    source_kind: str
    source: str
    source_default: str
    source_subpath: str
    target: str
    read_only: bool


@dataclass(frozen=True, slots=True)
class ServiceCatalog:
    manifests: tuple[ServiceManifest, ...]
    sources: tuple[tuple[str, Path], ...] = ()

    def __post_init__(self) -> None:
        _validate_catalog(self.manifests)

    @property
    def names(self) -> tuple[str, ...]:
        return tuple(manifest.name for manifest in self.manifests)

    def require(self, name: str) -> ServiceManifest:
        for manifest in self.manifests:
            if manifest.name == name:
                return manifest
        raise KeyError(name)

    def provider(self, capability: str) -> ServiceManifest | None:
        """Return the unique service that provides a framework capability."""
        return next((manifest for manifest in self.manifests if capability in manifest.provides), None)

    def require_provider(self, capability: str) -> ServiceManifest:
        provider = self.provider(capability)
        if provider is None:
            raise KeyError(capability)
        return provider

    def service_root(self, name: str) -> Path:
        for service, root in self.sources:
            if service == name:
                return root
        raise KeyError(f"service source unavailable for {name!r}")

    def compose_path(self, name: str) -> Path:
        return self.service_root(name) / "compose.yaml"

    def compose_applications(self) -> tuple[Path, ...]:
        return tuple(path for manifest in self.manifests if (path := self.compose_path(manifest.name)).is_file())


def _services_root(root: Path | None) -> Path:
    if root is None:
        return Path(__file__).resolve().parents[2] / "services"
    resolved = root.resolve()
    for candidate in (resolved / "toolkit" / "services", resolved / "services", resolved):
        if candidate.is_dir() and any(candidate.glob("*/service.yaml")):
            return candidate
    return resolved


@lru_cache(maxsize=32)
def _load_cached(root: str, add_on_roots: tuple[tuple[str, str], ...]) -> ServiceCatalog:
    services_root = Path(root)
    manifests: list[ServiceManifest] = []
    source_roots: dict[str, Path] = {}
    seen: set[str] = set()
    manifest_sources = [(f"built-in:{path.parent.name}", path) for path in sorted(services_root.glob("*/service.yaml"))]
    manifest_sources.extend((f"entry-point:{name}", Path(path) / "service.yaml") for name, path in add_on_roots)
    for source, path in manifest_sources:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ManifestCatalogError(f"cannot read {path}: {exc}") from exc
        if not isinstance(raw, dict):
            raise ManifestCatalogError(f"{path} must contain a YAML mapping")
        try:
            manifest = ServiceManifest.model_validate(raw)
        except ValidationError as exc:
            raise ManifestCatalogError(f"invalid service manifest {path}: {exc}") from exc
        if manifest.name != path.parent.name:
            raise ManifestCatalogError(f"manifest name {manifest.name!r} must match directory {path.parent.name!r}")
        if manifest.name in seen:
            raise ManifestCatalogError(f"duplicate service manifest {manifest.name!r} from {source}")
        plugin_root = path.parent.resolve()
        compose_path = path.parent / "compose.yaml"
        _validate_compose_logging(manifest, compose_path)
        _validate_runtime_compose_profiles(manifest, compose_path)
        _validate_image_build(manifest, compose_path, plugin_root.parent)
        _validate_backup_exports(manifest, compose_path)
        _validate_storage_mounts(manifest, compose_path)
        _validate_host_publications(manifest, compose_path)
        _validate_secret_projections(manifest, compose_path)
        _validate_runtime_variables(manifest, compose_path)
        _validate_service_endpoint(manifest, compose_path)
        _validate_database_provider(manifest, compose_path)
        seen.add(manifest.name)
        source_roots[manifest.name] = plugin_root
        manifests.append(manifest)
    if not manifests:
        raise ManifestCatalogError(f"no service manifests found under {services_root}")
    manifests.sort(key=lambda item: (item.priority, item.name))
    catalog = ServiceCatalog(tuple(manifests), tuple(sorted(source_roots.items())))
    _validate_compose_host_sources(catalog)
    _validate_generated_artifact_sources(catalog)
    return catalog


def load_service_catalog(root: Path | None = None) -> ServiceCatalog:
    from toolkit.services import installed_service_bundles

    resolved_root = root.resolve() if root is not None else None
    include_add_ons = root is None or bool(resolved_root and (resolved_root / "pyproject.toml").is_file())
    add_on_roots = (
        tuple((name, str(bundle.root.resolve())) for name, bundle in installed_service_bundles())
        if include_add_ons
        else ()
    )
    return _load_cached(str(_services_root(root)), add_on_roots)


def provider_service_name(capability: str, root: Path | None = None) -> str:
    """Resolve a required framework capability to its owning service name."""
    return load_service_catalog(root).require_provider(capability).name


def clear_catalog_cache() -> None:
    _load_cached.cache_clear()
    from toolkit.services import installed_service_bundles

    installed_service_bundles.cache_clear()


def _validate_compose_logging(manifest: ServiceManifest, compose_path: Path) -> None:
    """Require rotation-capable logs for every plugin-owned container."""
    if not compose_path.is_file():
        return
    try:
        document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ManifestCatalogError(f"cannot read {compose_path}: {exc}") from exc
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        raise ManifestCatalogError(f"{compose_path} must contain a Compose services mapping")
    for runtime, service in services.items():
        logging = service.get("logging") if isinstance(service, dict) else None
        if not isinstance(logging, dict):
            raise ManifestCatalogError(f"service {manifest.name!r} runtime {runtime!r} requires bounded logging")
        driver = logging.get("driver")
        if driver == "local":
            continue
        options = logging.get("options")
        max_size = options.get("max-size") if isinstance(options, dict) else None
        max_file = options.get("max-file") if isinstance(options, dict) else None
        file_count = int(max_file) if isinstance(max_file, str) and max_file.isdigit() else 0
        if (
            driver != "json-file"
            or not isinstance(max_size, str)
            or not _LOG_MAX_SIZE.fullmatch(max_size)
            or file_count < 1
        ):
            raise ManifestCatalogError(
                f"service {manifest.name!r} runtime {runtime!r} requires bounded logging "
                "with the local driver or json-file max-size and max-file"
            )


def _validate_runtime_compose_profiles(manifest: ServiceManifest, compose_path: Path) -> None:
    if not manifest.runtimes:
        return
    if not compose_path.is_file():
        raise ManifestCatalogError(f"service {manifest.name!r} declares runtimes without a Compose model")
    try:
        document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ManifestCatalogError(f"cannot read {compose_path}: {exc}") from exc
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        raise ManifestCatalogError(f"{compose_path} must contain a Compose services mapping")
    for runtime_service, runtime in manifest.runtimes.items():
        service = services.get(runtime_service)
        if not isinstance(service, dict):
            raise ManifestCatalogError(f"service {manifest.name!r} places unknown runtime service {runtime_service!r}")
        if runtime.compose_profile is None:
            continue
        profiles = service.get("profiles", ())
        if not isinstance(profiles, list) or runtime.compose_profile not in profiles:
            raise ManifestCatalogError(
                f"service {manifest.name!r} runtime {runtime_service!r} requires Compose profile "
                f"{runtime.compose_profile!r}"
            )


def _repository_relative_service_dir(services_root: Path, service: str) -> str:
    if services_root.name == "services" and services_root.parent.name == "toolkit":
        return f"toolkit/services/{service}"
    if services_root.name == "services":
        return f"services/{service}"
    return service


def _validate_image_build(manifest: ServiceManifest, compose_path: Path, services_root: Path) -> None:
    if not compose_path.is_file():
        if manifest.image_build is not None or manifest.image_release is not None:
            raise ManifestCatalogError(f"service {manifest.name!r} declares image ownership without a Compose model")
        return
    try:
        document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ManifestCatalogError(f"cannot read {compose_path}: {exc}") from exc
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        raise ManifestCatalogError(f"{compose_path} must contain a Compose services mapping")
    built = [(name, service) for name, service in services.items() if isinstance(service, dict) and "build" in service]
    for runtime, service in services.items():
        if not isinstance(service, dict):
            raise ManifestCatalogError(f"service {manifest.name!r} runtime {runtime!r} must be a mapping")
        if "build" in service:
            continue
        image = service.get("image")
        if not isinstance(image, str):
            raise ManifestCatalogError(f"service {manifest.name!r} runtime {runtime!r} must declare an image or build")
        try:
            parse_immutable_image_reference(image)
        except ValueError as exc:
            raise ManifestCatalogError(
                f"service {manifest.name!r} runtime {runtime!r} requires an immutable image reference: {exc}"
            ) from exc
    contract = manifest.image_build
    if built and contract is None:
        raise ManifestCatalogError(f"service {manifest.name!r} has a Compose build without image_build")
    if contract is not None and not built:
        raise ManifestCatalogError(f"service {manifest.name!r} declares image_build without a Compose build")
    release = manifest.image_release
    if release is not None:
        service = services.get(release.compose_service)
        if not isinstance(service, dict):
            raise ManifestCatalogError(
                f"service {manifest.name!r} release image targets unknown Compose service {release.compose_service!r}"
            )
        if "build" in service:
            raise ManifestCatalogError(
                f"service {manifest.name!r} release image cannot target a locally built Compose service"
            )
        if service.get("image") != release.immutable_ref:
            raise ManifestCatalogError(
                f"service {manifest.name!r} Compose service {release.compose_service!r} must use immutable image "
                f"{release.immutable_ref!r}"
            )
    if contract is None:
        return

    if contract.repository_context:
        expected_context = "."
        if services_root.parent.name == "toolkit":
            repository_root = services_root.parent.parent
        elif services_root.name == "services":
            repository_root = services_root.parent
        else:
            repository_root = services_root
        context_path = repository_root
    else:
        relative_service_dir = _repository_relative_service_dir(services_root, manifest.name)
        expected_context = f"./{relative_service_dir}/{contract.context}"
        context_path = compose_path.parent / contract.context
    if not context_path.is_dir():
        raise ManifestCatalogError(f"service {manifest.name!r} image context does not exist: {context_path}")
    dockerfile_path = context_path / contract.dockerfile
    if not dockerfile_path.is_file():
        raise ManifestCatalogError(f"service {manifest.name!r} image Dockerfile does not exist: {dockerfile_path}")
    requirements_path = context_path / contract.requirements if contract.requirements else None
    if requirements_path is not None and not requirements_path.is_file():
        raise ManifestCatalogError(f"service {manifest.name!r} image requirements do not exist: {requirements_path}")

    for compose_service, service in built:
        build = service["build"]
        context = build if isinstance(build, str) else build.get("context") if isinstance(build, dict) else None
        if context != expected_context:
            raise ManifestCatalogError(
                f"service {manifest.name!r} Compose build context for {compose_service!r} must be {expected_context!r}"
            )
        dockerfile = build.get("dockerfile", "Dockerfile") if isinstance(build, dict) else "Dockerfile"
        if isinstance(dockerfile, str):
            dockerfile = dockerfile.removeprefix("./")
        if dockerfile != contract.dockerfile:
            raise ManifestCatalogError(
                f"service {manifest.name!r} Compose Dockerfile for {compose_service!r} must be {contract.dockerfile!r}"
            )
        image = service.get("image")
        environment_reference = "${" + contract.env_var
        if not isinstance(image, str) or not image.startswith(
            (environment_reference + ":-", environment_reference + ":?", environment_reference + "}")
        ):
            raise ManifestCatalogError(
                f"service {manifest.name!r} Compose image for {compose_service!r} must use {contract.env_var}"
            )


def _validate_compose_host_sources(catalog: ServiceCatalog) -> None:
    manifests = catalog.manifests
    owners = {name: manifest for manifest in manifests for name in manifest.host_sources}
    used: set[str] = set()
    for manifest in manifests:
        compose_path = catalog.compose_path(manifest.name)
        if not compose_path.is_file():
            continue
        for mount in _compose_mounts(compose_path):
            if mount.source_kind != "env" or mount.source == "INSTALL_ROOT":
                continue
            owner = owners.get(mount.source)
            if owner is None:
                raise ManifestCatalogError(
                    f"Compose host source {mount.source!r} used by service {manifest.name!r} has no manifest owner"
                )
            used.add(mount.source)
            if owner.placement != manifest.placement:
                raise ManifestCatalogError(
                    f"host source {mount.source!r} owned by {owner.name!r} cannot be consumed by "
                    f"{manifest.name!r} from placement {manifest.placement!r}"
                )
            expected_fallback = f"./{owner.host_sources[mount.source].path}"
            if mount.source_default != expected_fallback:
                raise ManifestCatalogError(
                    f"host source {mount.source!r} fallback must be {expected_fallback!r}, not {mount.source_default!r}"
                )
            if owner.host_sources[mount.source].static and not mount.read_only:
                raise ManifestCatalogError(f"static host source {mount.source!r} must be mounted read-only")
    unused = sorted(set(owners) - used)
    if unused:
        raise ManifestCatalogError("manifest host source " + ", ".join(unused) + " is not used by Compose")


def _validate_generated_artifact_sources(catalog: ServiceCatalog) -> None:
    manifests = catalog.manifests
    artifacts = {
        artifact.path: (manifest, artifact) for manifest in manifests for artifact in manifest.generated_artifacts
    }

    for manifest in manifests:
        artifact_paths = {artifact.path for artifact in manifest.generated_artifacts}
        for source_name, source in manifest.host_sources.items():
            if not source.path.startswith("generated/"):
                continue
            source_prefix = source.path.rstrip("/") + "/"
            if not any(path == source.path or path.startswith(source_prefix) for path in artifact_paths):
                raise ManifestCatalogError(
                    f"generated host source {source_name!r} owned by {manifest.name!r} has no declared artifact"
                )

        compose_path = catalog.compose_path(manifest.name)
        if not compose_path.is_file():
            continue
        for mount in _compose_mounts(compose_path):
            if (
                mount.source_kind != "env"
                or mount.source != "INSTALL_ROOT"
                or not mount.source_subpath.startswith("generated/")
            ):
                continue
            owned = artifacts.get(mount.source_subpath)
            if owned is None:
                raise ManifestCatalogError(
                    f"generated Compose source {mount.source_subpath!r} used by {manifest.name!r} "
                    "has no declared artifact"
                )
            owner, artifact = owned
            if owner.placement != manifest.placement:
                raise ManifestCatalogError(
                    f"generated artifact {mount.source_subpath!r} owned by {owner.name!r} cannot be consumed by "
                    f"{manifest.name!r} from placement {manifest.placement!r}"
                )
            if artifact.runtime_service and (
                owner.name != manifest.name or artifact.runtime_service != mount.compose_service
            ):
                raise ManifestCatalogError(
                    f"generated artifact {mount.source_subpath!r} is scoped to runtime "
                    f"{artifact.runtime_service!r}, not {mount.compose_service!r}"
                )
            runtime = manifest.runtimes.get(mount.compose_service)
            if runtime is not None and runtime.placements and not artifact.runtime_service:
                raise ManifestCatalogError(
                    f"generated artifact {mount.source_subpath!r} consumed by placed runtime "
                    f"{mount.compose_service!r} must declare runtime_service"
                )


def _validate_backup_exports(manifest: ServiceManifest, compose_path: Path) -> None:
    exports = tuple(export for export in manifest.backup_exports if export.strategy == "container")
    if not exports:
        return
    if not compose_path.is_file():
        raise ManifestCatalogError(f"service {manifest.name!r} declares a container backup without a Compose model")
    try:
        document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ManifestCatalogError(f"cannot read {compose_path}: {exc}") from exc
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        raise ManifestCatalogError(f"{compose_path} must contain a Compose services mapping")
    for export in exports:
        target = export.container or export.runtime_service or manifest.name
        if target not in services:
            raise ManifestCatalogError(
                f"service {manifest.name!r} backup export targets unknown Compose service {target!r}"
            )


def _compose_mounts(path: Path) -> set[ComposeMount]:
    """Return normalized persistent mounts from Compose YAML."""
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ManifestCatalogError(f"cannot read {path}: {exc}") from exc
    if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
        raise ManifestCatalogError(f"{path} must contain a Compose services mapping")
    mounts: set[ComposeMount] = set()
    for service_name, service in document["services"].items():
        if not isinstance(service, dict):
            continue
        for mount in service.get("volumes", ()):
            source: object
            target: object
            read_only = False
            if isinstance(mount, str):
                if mount.startswith("${") and (closing := mount.find("}")) >= 0:
                    separator = mount.find(":", closing + 1)
                    if separator < 0:
                        continue
                    source = mount[:separator]
                    remainder = mount[separator + 1 :].split(":")
                    target = remainder[0]
                    read_only = any("ro" in option.split(",") for option in remainder[1:])
                else:
                    parts = mount.split(":")
                    if len(parts) < 2:
                        continue
                    source, target = parts[0], parts[1]
                    read_only = any("ro" in option.split(",") for option in parts[2:])
            elif isinstance(mount, dict):
                source, target = mount.get("source"), mount.get("target")
                read_only = mount.get("read_only") is True
            else:
                continue
            if not isinstance(source, str) or not isinstance(target, str):
                continue
            env_match = _COMPOSE_ENV_SOURCE.fullmatch(source)
            mounts.add(
                ComposeMount(
                    compose_service=str(service_name),
                    source_kind="env" if env_match else "volume",
                    source=env_match.group("name") if env_match else source,
                    source_default=(env_match.group("default") or "") if env_match else "",
                    source_subpath=(env_match.group("subpath") or "") if env_match else "",
                    target=target,
                    read_only=read_only,
                )
            )
    return mounts


def _validate_storage_mounts(manifest: ServiceManifest, compose_path: Path) -> None:
    if not compose_path.is_file():
        if manifest.data_specs:
            raise ManifestCatalogError(f"stateful service {manifest.name!r} has no Compose model")
        return
    mounts = _compose_mounts(compose_path)
    writable = {mount for mount in mounts if not mount.read_only}
    declared_assets: list[tuple[tuple[str, str, str, str], str]] = []
    for asset in manifest.data_specs:
        source_kind = "env" if asset.source_env is not None else "volume"
        source = asset.source_env if asset.source_env is not None else asset.volume
        key = (source_kind, source or "", asset.source_subpath, asset.target)
        declared_assets.append((key, asset.runtime_service))
        if not any(
            (mount.source_kind, mount.source, mount.source_subpath, mount.target) == key
            and (not asset.runtime_service or asset.runtime_service == mount.compose_service)
            for mount in writable
        ):
            raise ManifestCatalogError(
                f"service {manifest.name!r} storage asset {asset.name!r} has no matching Compose mount"
            )
    undeclared = sorted(
        (
            mount
            for mount in writable
            if not any(
                (mount.source_kind, mount.source, mount.source_subpath, mount.target) == key
                and (not runtime_service or runtime_service == mount.compose_service)
                for key, runtime_service in declared_assets
            )
        ),
        key=lambda mount: (mount.source, mount.source_subpath, mount.target),
    )
    if undeclared:
        mount = undeclared[0]
        source = mount.source + (f"/{mount.source_subpath}" if mount.source_subpath else "")
        raise ManifestCatalogError(
            f"service {manifest.name!r} has undeclared writable Compose mount {source!r} -> {mount.target!r}"
        )


def _validate_host_publications(manifest: ServiceManifest, compose_path: Path) -> None:
    compose_listeners = [listener for listener in manifest.network_listeners if not listener.host_process]
    scrape_listeners = [scrape for scrape in manifest.prometheus if scrape.host_port and not scrape.host_integration]
    if not compose_listeners and not scrape_listeners:
        return
    if not compose_path.is_file():
        raise ManifestCatalogError(f"service {manifest.name!r} declares network listeners without a Compose model")
    try:
        document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ManifestCatalogError(f"cannot read {compose_path}: {exc}") from exc
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        raise ManifestCatalogError(f"{compose_path} must contain a Compose services mapping")

    from toolkit.core.compose.ports import compose_published_ports

    for listener in compose_listeners:
        runtime_service = listener.runtime_service or manifest.name
        service = services.get(runtime_service)
        published = compose_published_ports(service) if isinstance(service, dict) else []
        if not any(
            port.published == listener.port
            and port.protocol == listener.protocol
            and port.host_ip not in {"127.0.0.1", "::1", "localhost"}
            for port in published
        ):
            raise ManifestCatalogError(
                f"service {manifest.name!r} network listener {listener.id!r} requires a non-loopback "
                f"{listener.protocol} publication for {runtime_service}:{listener.port}"
            )
    for scrape in scrape_listeners:
        runtime_service = scrape.runtime_service or manifest.name
        service = services.get(runtime_service)
        published = compose_published_ports(service) if isinstance(service, dict) else []
        if not any(
            port.published == scrape.host_port
            and port.protocol == "tcp"
            and port.host_ip not in {"127.0.0.1", "::1", "localhost"}
            for port in published
        ):
            raise ManifestCatalogError(
                f"service {manifest.name!r} Prometheus scrape {scrape.id!r} requires a non-loopback "
                f"tcp publication for {runtime_service}:{scrape.host_port}"
            )


def _validate_secret_projections(manifest: ServiceManifest, compose_path: Path) -> None:
    if not manifest.secret_projections:
        return
    if not compose_path.is_file():
        raise ManifestCatalogError(f"service {manifest.name!r} projects runtime secrets without a Compose model")
    try:
        document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ManifestCatalogError(f"cannot read {compose_path}: {exc}") from exc
    references = set(_COMPOSE_ENV_REFERENCE.findall(yaml.safe_dump(document, sort_keys=True)))
    missing = sorted(
        projection.target_env for projection in manifest.secret_projections if projection.target_env not in references
    )
    if missing:
        raise ManifestCatalogError(
            f"service {manifest.name!r} projected secret environment is not referenced by Compose: "
            + ", ".join(missing)
        )


def _validate_runtime_variables(manifest: ServiceManifest, compose_path: Path) -> None:
    if not manifest.runtime_variables:
        return
    if not compose_path.is_file():
        raise ManifestCatalogError(f"service {manifest.name!r} declares runtime variables without a Compose model")
    if not (compose_path.parent / "plugin.py").is_file():
        raise ManifestCatalogError(f"service {manifest.name!r} declares runtime variables without a plugin")
    try:
        document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ManifestCatalogError(f"cannot read {compose_path}: {exc}") from exc
    references = set(_COMPOSE_ENV_REFERENCE.findall(yaml.safe_dump(document, sort_keys=True)))
    missing = sorted(set(manifest.runtime_variables) - references)
    if missing:
        raise ManifestCatalogError(
            f"service {manifest.name!r} runtime variable is not referenced by Compose: " + ", ".join(missing)
        )


def _validate_database_provider(manifest: ServiceManifest, compose_path: Path) -> None:
    contract = manifest.database_provider
    if contract is None:
        return
    if not compose_path.is_file():
        raise ManifestCatalogError(f"service {manifest.name!r} declares a database provider without a Compose model")
    try:
        document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ManifestCatalogError(f"cannot read {compose_path}: {exc}") from exc
    services = document.get("services") if isinstance(document, dict) else None
    endpoint = manifest.service_endpoint
    if endpoint is None:
        raise ManifestCatalogError(f"database provider {manifest.name!r} must declare a service endpoint")
    runtime = endpoint.compose_service or manifest.name
    service = services.get(runtime) if isinstance(services, dict) else None
    if not isinstance(service, dict):
        raise ManifestCatalogError(f"service {manifest.name!r} database provider runtime {runtime!r} is missing")

    references = set(_COMPOSE_ENV_REFERENCE.findall(yaml.safe_dump(service, sort_keys=True)))
    required = {
        contract.admin_username_env,
        contract.admin_password_env,
        contract.admin_database_env,
    }
    missing = sorted(required - references)
    if missing:
        raise ManifestCatalogError(
            f"service {manifest.name!r} database provider environment {', '.join(missing)} is not referenced by "
            f"runtime {runtime!r}"
        )


def _validate_service_endpoint(manifest: ServiceManifest, compose_path: Path) -> None:
    contract = manifest.service_endpoint
    if contract is None:
        return
    if not compose_path.is_file():
        raise ManifestCatalogError(f"service {manifest.name!r} declares a service endpoint without a Compose model")
    try:
        document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise ManifestCatalogError(f"cannot read {compose_path}: {exc}") from exc
    services = document.get("services") if isinstance(document, dict) else None
    runtime = contract.compose_service or manifest.name
    service = services.get(runtime) if isinstance(services, dict) else None
    if not isinstance(service, dict):
        raise ManifestCatalogError(f"service {manifest.name!r} endpoint runtime {runtime!r} is missing")
    if contract.published_port is None:
        return

    from toolkit.core.compose.ports import compose_published_ports

    published = compose_published_ports(service)
    if not any(
        port.published == contract.published_port
        and port.target == contract.container_port
        and port.protocol == "tcp"
        and port.host_ip not in {"127.0.0.1", "::1", "localhost"}
        for port in published
    ):
        raise ManifestCatalogError(
            f"service {manifest.name!r} endpoint requires non-loopback published port "
            f"{contract.published_port} to target {runtime}:{contract.container_port}"
        )


def _validate_catalog(manifests: tuple[ServiceManifest, ...]) -> None:
    from toolkit.core.compose.registry import all_categories, load_all
    from toolkit.core.config.config import Config
    from toolkit.core.manifest.settings import ServiceSettingError, validate_setting_value
    from toolkit.core.manifest.variables import compile_manifest_variables

    load_all()
    category_by_name = {category.name: category for category in all_categories()}
    known_categories = set(category_by_name)
    known_access_groups = {
        category.access_group.name for category in category_by_name.values() if category.access_group is not None
    }
    names = [manifest.name for manifest in manifests]
    if len(names) != len(set(names)):
        raise ManifestCatalogError("duplicate service manifest name")
    by_name = {manifest.name: manifest for manifest in manifests}
    known = set(by_name)
    declared_settings = {
        f"{manifest.name}.{setting.key}": setting for manifest in manifests for setting in manifest.management.settings
    }
    known_environments = (
        {secret.name for manifest in manifests for secret in manifest.required_secrets}
        | {name for manifest in manifests for name in manifest.variables}
        | {projection.target_env for manifest in manifests for projection in manifest.secret_projections}
        | {name for manifest in manifests for name in manifest.runtime_variables}
    )
    default_config = Config()
    service_nodes = {manifest.name: default_config.control_node for manifest in manifests}
    capability_owners: dict[str, str] = {}
    for manifest in manifests:
        for capability in manifest.provides:
            previous = capability_owners.get(capability)
            if previous is not None:
                raise ManifestCatalogError(
                    f"capability {capability!r} is provided by both {previous!r} and {manifest.name!r}"
                )
            capability_owners[capability] = manifest.name
    metrics_service = next((manifest for manifest in manifests if "metrics" in manifest.provides), None)
    ingress_service = next((manifest for manifest in manifests if "ingress" in manifest.provides), None)
    host_integration_owners: dict[str, str] = {}
    credential_owners: dict[str, str] = {}
    image_environment_owners: dict[str, str] = {}
    image_repository_owners: dict[str, str] = {}
    host_source_owners: dict[str, str] = {}
    config_source_path_owners: list[tuple[str, str]] = []
    internal_alias_owners: dict[str, str] = {}
    generated_artifact_owners: dict[str, str] = {}
    database_owners: dict[tuple[str, str], str] = {}
    database_role_owners: dict[tuple[str, str], str] = {}
    secret_projection_owners: dict[str, str] = {}
    for manifest in manifests:
        if manifest.category not in known_categories:
            raise ManifestCatalogError(f"service {manifest.name!r} belongs to unknown category {manifest.category!r}")
        category_group = category_by_name[manifest.category].service_group
        effective_groups = manifest.identity.access_groups or ((category_group,) if category_group else ())
        unknown_groups = sorted(set(effective_groups) - known_access_groups)
        if unknown_groups:
            raise ManifestCatalogError(
                f"service {manifest.name!r} references unknown access groups: {', '.join(unknown_groups)}"
            )
        for projection in manifest.secret_projections:
            previous = secret_projection_owners.get(projection.target_env)
            if previous is not None:
                raise ManifestCatalogError(
                    f"runtime secret {projection.target_env!r} is projected by both {previous!r} and {manifest.name!r}"
                )
            secret_projection_owners[projection.target_env] = manifest.name
        if manifest.identity.invite is not None and manifest.identity.invite.group not in effective_groups:
            raise ManifestCatalogError(
                f"service {manifest.name!r} invite group must be included in its effective access groups"
            )
        try:
            compile_manifest_variables(
                default_config,
                manifest,
                service_nodes=service_nodes,
                capability_providers=capability_owners,
            )
        except ValueError as exc:
            raise ManifestCatalogError(f"service {manifest.name!r} has an invalid manifest variable: {exc}") from exc
        predicates = [
            *manifest.enabled_when,
            *(variant.when for route in manifest.routes for variant in route.variants),
            *(predicate for listener in manifest.network_listeners for predicate in listener.enabled_when),
            *(variant.when for source in manifest.host_sources.values() for variant in source.variants),
            *(
                predicate
                for secret in manifest.required_secrets
                if secret.setup is not None
                for predicate in secret.setup.when
            ),
        ]
        for predicate in predicates:
            if predicate.setting is None:
                continue
            definition = declared_settings.get(predicate.setting)
            if definition is None:
                raise ManifestCatalogError(
                    f"service {manifest.name!r} references unknown service setting {predicate.setting!r}"
                )
            values = (predicate.equals,) if "equals" in predicate.model_fields_set else predicate.one_of
            try:
                for value in values:
                    validate_setting_value(definition, value)
            except ServiceSettingError as exc:
                raise ManifestCatalogError(
                    f"service {manifest.name!r} has an incompatible predicate value for {predicate.setting!r}: {exc}"
                ) from exc
        for secret in manifest.required_secrets:
            if secret.fallback_env is not None and secret.fallback_env not in known_environments:
                raise ManifestCatalogError(
                    f"service {manifest.name!r} secret {secret.name!r} references unknown fallback "
                    f"{secret.fallback_env!r}"
                )
            if secret.setup is None:
                continue
            for predicate in secret.setup.when:
                if predicate.setting is None:
                    raise ManifestCatalogError(
                        f"service {manifest.name!r} setup secret {secret.name!r} requires service-setting predicates"
                    )
                if "equals" in predicate.model_fields_set and predicate.equals is None:
                    raise ManifestCatalogError(
                        f"service {manifest.name!r} setup secret {secret.name!r} cannot compare with null"
                    )
                definition = declared_settings[predicate.setting]
                if not definition.setup:
                    raise ManifestCatalogError(
                        f"service {manifest.name!r} setup secret {secret.name!r} depends on a setting "
                        "not exposed in setup"
                    )
        for dependency in manifest.depends_on:
            if dependency not in known:
                raise ManifestCatalogError(f"service {manifest.name!r} depends on unknown service {dependency!r}")
            if dependency == manifest.name:
                raise ManifestCatalogError(f"service {manifest.name!r} cannot depend on itself")
        for binding in manifest.databases:
            provider = by_name.get(binding.provider)
            if provider is None:
                raise ManifestCatalogError(
                    f"service {manifest.name!r} references unknown database provider {binding.provider!r}"
                )
            if binding.provider not in manifest.depends_on:
                raise ManifestCatalogError(
                    f"service {manifest.name!r} database provider {binding.provider!r} must be a service dependency"
                )
            if provider.database_provider is None:
                raise ManifestCatalogError(
                    f"service {binding.provider!r} does not declare a database provider contract"
                )
            database_key = (binding.provider, binding.database)
            previous = database_owners.get(database_key)
            if previous is not None:
                raise ManifestCatalogError(
                    f"database {binding.database!r} on provider {binding.provider!r} is owned by both "
                    f"{previous!r} and {manifest.name!r}"
                )
            database_owners[database_key] = manifest.name
            role_key = (binding.provider, binding.username)
            previous = database_role_owners.get(role_key)
            if previous is not None:
                raise ManifestCatalogError(
                    f"database role {binding.username!r} on provider {binding.provider!r} is owned by both "
                    f"{previous!r} and {manifest.name!r}"
                )
            database_role_owners[role_key] = manifest.name
        for integration in manifest.integrations:
            provider = by_name.get(integration.service)
            if provider is None:
                raise ManifestCatalogError(
                    f"service {manifest.name!r} references unknown integration {integration.service!r}"
                )
            if integration.required and integration.service not in manifest.depends_on:
                raise ManifestCatalogError(
                    f"required integration {manifest.name!r} -> {integration.service!r} must be a service dependency"
                )
            if not integration.required and integration.service in manifest.depends_on:
                raise ManifestCatalogError(
                    f"optional integration {manifest.name!r} -> {integration.service!r} cannot be a service dependency"
                )
            if provider.service_endpoint is None:
                raise ManifestCatalogError(
                    f"service {manifest.name!r} integration {integration.service!r} requires a service endpoint"
                )
            if manifest.placement != provider.placement and provider.service_endpoint.published_port is None:
                raise ManifestCatalogError(
                    f"cross-node integration {manifest.name!r} -> {provider.name!r} requires a published port"
                )
        if ingress_service is not None and manifest.placement != ingress_service.placement:
            missing_host_ports = [
                route.subdomain if route.subdomain is not None else manifest.name
                for route in manifest.routes
                if not route.file_server_root and route.published_port is None
            ]
            if missing_host_ports:
                raise ManifestCatalogError(
                    f"cross-node routes for service {manifest.name!r} require published_port: "
                    + ", ".join(missing_host_ports)
                )
        if manifest.prometheus:
            if metrics_service is None:
                raise ManifestCatalogError(
                    f"service {manifest.name!r} declares a scrape endpoint but the catalog has no metrics provider"
                )
            for scrape in manifest.prometheus:
                if not scrape.host_integration and scrape.host_port is None:
                    raise ManifestCatalogError(f"service {manifest.name!r} scrape {scrape.id!r} requires host_port")
        for host_integration in manifest.host_integrations:
            previous = host_integration_owners.get(host_integration.id)
            if previous is not None:
                raise ManifestCatalogError(
                    f"host integration {host_integration.id!r} is owned by both {previous!r} and {manifest.name!r}"
                )
            host_integration_owners[host_integration.id] = manifest.name
        if manifest.image_build is not None:
            env_var = manifest.image_build.env_var
            previous = image_environment_owners.get(env_var)
            if previous is not None:
                raise ManifestCatalogError(
                    f"image environment {env_var!r} is owned by both {previous!r} and {manifest.name!r}"
                )
            image_environment_owners[env_var] = manifest.name
            repository = manifest.image_build.repository or manifest.name
            previous = image_repository_owners.get(repository)
            if previous is not None:
                raise ManifestCatalogError(
                    f"image repository {repository!r} is owned by both {previous!r} and {manifest.name!r}"
                )
            image_repository_owners[repository] = manifest.name
        if manifest.image_release is not None:
            repository = manifest.image_release.repository
            previous = image_repository_owners.get(repository)
            if previous is not None:
                raise ManifestCatalogError(
                    f"image repository {repository!r} is owned by both {previous!r} and {manifest.name!r}"
                )
            image_repository_owners[repository] = manifest.name
        for source_name, source in manifest.host_sources.items():
            previous = host_source_owners.get(source_name)
            if previous is not None:
                raise ManifestCatalogError(
                    f"host source {source_name!r} is owned by both {previous!r} and {manifest.name!r}"
                )
            host_source_owners[source_name] = manifest.name
            for path in {source.path, *(variant.path for variant in source.variants)}:
                if not path.startswith("config/"):
                    continue
                for previous_path, previous_owner in config_source_path_owners:
                    overlaps = (
                        path == previous_path
                        or path.startswith(previous_path.rstrip("/") + "/")
                        or previous_path.startswith(path.rstrip("/") + "/")
                    )
                    if overlaps:
                        raise ManifestCatalogError(
                            f"config host source path {path!r} owned by {manifest.name!r} overlaps "
                            f"{previous_path!r} owned by {previous_owner!r}"
                        )
                config_source_path_owners.append((path, manifest.name))
        for alias in manifest.internal_aliases:
            previous = internal_alias_owners.get(alias)
            if previous is not None:
                raise ManifestCatalogError(
                    f"internal DNS alias {alias!r} is owned by both {previous!r} and {manifest.name!r}"
                )
            internal_alias_owners[alias] = manifest.name
        for artifact in manifest.generated_artifacts:
            previous = generated_artifact_owners.get(artifact.path)
            if previous is not None:
                raise ManifestCatalogError(
                    f"generated artifact {artifact.path!r} is owned by both {previous!r} and {manifest.name!r}"
                )
            generated_artifact_owners[artifact.path] = manifest.name
        for credential in manifest.credentials:
            previous = credential_owners.get(credential.name)
            if previous is not None:
                raise ManifestCatalogError(
                    f"credential name {credential.name!r} is owned by both {previous!r} and {manifest.name!r}"
                )
            credential_owners[credential.name] = manifest.name
            if credential.password_env and credential.password_env not in known_environments:
                raise ManifestCatalogError(
                    f"service {manifest.name!r} credential {credential.name!r} references "
                    f"undeclared password environment {credential.password_env!r}"
                )
            if credential.username_env and credential.username_env not in known_environments:
                raise ManifestCatalogError(
                    f"service {manifest.name!r} credential {credential.name!r} references "
                    f"undeclared username environment {credential.username_env!r}"
                )
        # A service may use the edge's split policy while still owning a
        # native OIDC client (for example, RomM: browser UI behind Authelia,
        # native API and callback paths). Treat split as an OIDC-capable route
        # for catalog validation; the route compiler still enforces the
        # explicit passthrough paths.
        has_oidc_route = any(route.auth.mode == "oidc" for route in manifest.routes)
        has_split_oidc_route = manifest.oidc is not None and any(
            route.auth.mode == "split" for route in manifest.routes
        )
        if has_oidc_route and manifest.oidc is None:
            raise ManifestCatalogError(f"service {manifest.name!r} has an OIDC route without an OIDC manifest")
        if manifest.oidc is not None:
            declared_secrets = {secret.name for secret in manifest.required_secrets}
            if manifest.oidc.secret_env_var not in declared_secrets:
                raise ManifestCatalogError(
                    f"service {manifest.name!r} OIDC client secret is not a declared required secret"
                )
            if not (has_oidc_route or has_split_oidc_route):
                raise ManifestCatalogError(f"service {manifest.name!r} declares OIDC but has no OIDC route")

    known_integrations = set(host_integration_owners)
    integration_ordering = {
        integration.id: integration.after for manifest in manifests for integration in manifest.host_integrations
    }
    for integration_id, dependencies in integration_ordering.items():
        unknown = sorted(set(dependencies) - known_integrations)
        if unknown:
            raise ManifestCatalogError(
                f"host integration {integration_id!r} orders after unknown integration(s): {', '.join(unknown)}"
            )
    ordered: set[str] = set()
    ordering_stack: list[str] = []

    def visit_host_integration(integration_id: str) -> None:
        if integration_id in ordered:
            return
        if integration_id in ordering_stack:
            start = ordering_stack.index(integration_id)
            cycle = [*ordering_stack[start:], integration_id]
            raise ManifestCatalogError(f"host integration ordering cycle: {' -> '.join(cycle)}")
        ordering_stack.append(integration_id)
        for dependency in integration_ordering[integration_id]:
            visit_host_integration(dependency)
        ordering_stack.pop()
        ordered.add(integration_id)

    for integration_id in integration_ordering:
        visit_host_integration(integration_id)

    prometheus_job_paths: dict[str, str] = {}
    for manifest in manifests:
        for listener in manifest.network_listeners:
            for listener_source in listener.sources:
                if listener_source.startswith("@service:") and listener_source.removeprefix("@service:") not in known:
                    raise ManifestCatalogError(
                        f"service {manifest.name!r} network listener {listener.id!r} references unknown service "
                        f"{listener_source.removeprefix('@service:')!r}"
                    )
                if (
                    listener_source.startswith("@integration:")
                    and listener_source.removeprefix("@integration:") not in known_integrations
                ):
                    raise ManifestCatalogError(
                        f"service {manifest.name!r} network listener {listener.id!r} references unknown host "
                        f"integration {listener_source.removeprefix('@integration:')!r}"
                    )
        for scrape in manifest.prometheus:
            if scrape.host_integration and scrape.host_integration not in known_integrations:
                raise ManifestCatalogError(
                    f"service {manifest.name!r} Prometheus scrape {scrape.id!r} references unknown host "
                    f"integration {scrape.host_integration!r}"
                )
            job = scrape.job or manifest.name
            previous_path = prometheus_job_paths.setdefault(job, scrape.path)
            if previous_path != scrape.path:
                raise ManifestCatalogError(
                    f"Prometheus job {job!r} declares conflicting paths {previous_path!r} and {scrape.path!r}"
                )

    dependency_graph = {manifest.name: manifest.depends_on for manifest in manifests}
    visited: set[str] = set()
    visiting: list[str] = []

    def visit(service: str) -> None:
        if service in visited:
            return
        if service in visiting:
            start = visiting.index(service)
            cycle = [*visiting[start:], service]
            raise ManifestCatalogError(f"service dependency cycle: {' -> '.join(cycle)}")
        visiting.append(service)
        for dependency in dependency_graph[service]:
            visit(dependency)
        visiting.pop()
        visited.add(service)

    for service in dependency_graph:
        visit(service)

    default_routes: dict[str, str] = {}
    matched_routes: set[tuple[str, str, str]] = set()
    represented_hosts: set[str] = set()

    def claim_match(subdomain: str, kind: str, path: str) -> None:
        key = (subdomain, kind, path)
        if key in matched_routes:
            raise ManifestCatalogError(f"duplicate {kind} route {path!r} for host {subdomain or '<root>'!r}")
        matched_routes.add(key)

    for manifest in manifests:
        for route in manifest.routes:
            subdomain = manifest.name if route.subdomain is None else route.subdomain
            represented_hosts.add(subdomain)
            if route.match is None:
                previous = default_routes.get(subdomain)
                if previous is not None:
                    raise ManifestCatalogError(
                        f"host {subdomain or '<root>'!r} has more than one default route ({previous}, {manifest.name})"
                    )
                default_routes[subdomain] = manifest.name
                if route.auth.mode == "split":
                    for path in route.auth.passthrough_paths:
                        claim_match(subdomain, "exact", path)
                for denied in route.deny:
                    for path in denied.paths:
                        claim_match(subdomain, denied.kind, path)
                continue
            for path in route.match.paths:
                claim_match(subdomain, route.match.kind, path)
    missing_defaults = represented_hosts - set(default_routes)
    if missing_defaults:
        rendered = ", ".join(sorted(value or "<root>" for value in missing_defaults))
        raise ManifestCatalogError(f"hosts without a default route: {rendered}")
