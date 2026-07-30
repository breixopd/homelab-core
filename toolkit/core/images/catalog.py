"""Manifest-owned custom image discovery, placement, and Compose environment."""

from __future__ import annotations

import hashlib
import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from toolkit.core.manifest.schema import ImageSmokeTestManifest


@dataclass(frozen=True, slots=True)
class CustomImage:
    name: str
    repository: str
    context: str
    env_var: str
    dockerfile: str | None
    ci: bool
    platforms: tuple[str, ...]
    smoke_tests: tuple[ImageSmokeTestManifest, ...]
    requirements: str | None


DEFAULT_REGISTRY = "ghcr.io/breixopd"
DEFAULT_TAG = "latest"
_COMMIT = re.compile(r"^[0-9a-f]{40}$")


def _git_output(root: Path, *args: str) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(root), *args],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout if result.returncode == 0 else None


def resolve_image_tag(root: Path, configured: str) -> str:
    """Resolve the automatic first-party image tag for this source checkout."""
    if configured != "auto":
        return configured
    root = root.resolve()
    environment_sha = os.environ.get("GITHUB_SHA", "").lower()
    commit = (
        environment_sha
        if _COMMIT.fullmatch(environment_sha)
        else (_git_output(root, "rev-parse", "HEAD") or "").strip()
    )
    if not _COMMIT.fullmatch(commit):
        return DEFAULT_TAG
    status = _git_output(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status is None:
        return DEFAULT_TAG
    if not status:
        return f"sha-{commit}"

    fingerprint = hashlib.sha256(commit.encode("ascii"))
    fingerprint.update(status.encode("utf-8", errors="replace"))
    diff = _git_output(root, "diff", "--binary", "HEAD")
    if diff is not None:
        fingerprint.update(diff.encode("utf-8", errors="replace"))
    for line in status.splitlines():
        if not line.startswith("?? "):
            continue
        path = root / line[3:]
        try:
            if path.is_file() and path.stat().st_size <= 16 * 1024 * 1024:
                fingerprint.update(path.read_bytes())
        except OSError:
            continue
    return f"local-{fingerprint.hexdigest()[:16]}"


def _repository_root(root: Path | None) -> Path:
    return root.resolve() if root is not None else Path(__file__).resolve().parents[3]


def _service_directory(root: Path, service: str) -> Path:
    for candidate in (root / "toolkit" / "services" / service, root / "services" / service, root / service):
        if (candidate / "service.yaml").is_file():
            return candidate
    raise FileNotFoundError(f"Cannot locate service manifest directory for {service!r} below {root}")


def custom_images(root: Path | None = None) -> tuple[CustomImage, ...]:
    """Compile custom image targets from validated service manifests."""
    from toolkit.core.manifest.catalog import load_service_catalog

    repository = _repository_root(root)
    images: list[CustomImage] = []
    for manifest in load_service_catalog(repository).manifests:
        build = manifest.image_build
        if build is None:
            continue
        if build.repository_context:
            context_path = repository
        else:
            context_path = _service_directory(repository, manifest.name) / build.context
        context = str(context_path.relative_to(repository)) or "."
        dockerfile_path = context_path / build.dockerfile
        dockerfile = None if build.dockerfile == "Dockerfile" else str(dockerfile_path.relative_to(repository))
        requirements = str((context_path / build.requirements).relative_to(repository)) if build.requirements else None
        images.append(
            CustomImage(
                name=manifest.name,
                repository=build.repository or manifest.name,
                context=context,
                env_var=build.env_var,
                dockerfile=dockerfile,
                ci=build.ci,
                platforms=build.platforms,
                smoke_tests=build.smoke_tests,
                requirements=requirements,
            )
        )
    return tuple(images)


def image_ref(registry: str, name: str, tag: str = DEFAULT_TAG) -> str:
    reg = (registry or DEFAULT_REGISTRY).rstrip("/")
    return f"{reg}/{name}:{tag}"


def nodes_for_image(cfg, image: CustomImage, root: Path | None = None) -> tuple[str, ...]:
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_node, manifest_runtime_nodes

    manifest = load_service_catalog(root).require(image.name)
    nodes = [manifest_node(cfg, manifest)]
    for runtime_service in manifest.runtimes:
        nodes.extend(manifest_runtime_nodes(cfg, manifest, runtime_service))
    return tuple(dict.fromkeys(nodes))


def images_for_node(cfg, node: str, root: Path | None = None) -> list[CustomImage]:
    return [image for image in custom_images(root) if node in nodes_for_image(cfg, image, root)]


def expected_images_for_node(cfg, node: str, root: Path | None = None) -> list[CustomImage]:
    """Return manifest-enabled custom images required on a managed machine."""
    from toolkit.core.config.config import Config
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import service_is_enabled

    if not isinstance(cfg, Config):
        raise TypeError("cfg must be a Config instance")
    catalog = load_service_catalog(root)
    return [
        image
        for image in images_for_node(cfg, node, root)
        if service_is_enabled(cfg, catalog.require(image.name), catalog)
    ]


def unique_images_for_nodes(cfg, nodes: list[str], root: Path | None = None) -> list[CustomImage]:
    seen: set[str] = set()
    images: list[CustomImage] = []
    for node in nodes:
        for image in images_for_node(cfg, node, root):
            if image.name not in seen:
                seen.add(image.name)
                images.append(image)
    return images


def compose_image_env(registry: str, tag: str = DEFAULT_TAG, root: Path | None = None) -> dict[str, str]:
    reg = registry or DEFAULT_REGISTRY
    selected_tag = resolve_image_tag(_repository_root(root), tag)
    env = {"HOMELAB_REGISTRY": reg, "HOMELAB_IMAGE_TAG": selected_tag}
    env.update({image.env_var: image_ref(reg, image.repository, selected_tag) for image in custom_images(root)})
    return env


def resolve_image_names(names: tuple[str, ...] | None, root: Path | None = None) -> list[CustomImage]:
    images = custom_images(root)
    if not names:
        return list(images)
    wanted = {name.lower() for name in names}
    selected = [image for image in images if image.name in wanted]
    missing = wanted - {image.name for image in selected}
    if missing:
        raise ValueError(f"Unknown image(s): {', '.join(sorted(missing))}")
    return selected
