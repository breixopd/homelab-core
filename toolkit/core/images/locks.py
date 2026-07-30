"""Discover and resolve service-owned immutable runtime image locks."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from toolkit.core.images.references import parse_image_version_reference, parse_immutable_image_reference

_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_TRANSIENT_REGISTRY_ERROR = re.compile(
    r"(?:\b(?:429|500|502|503|504)\b|too many requests|temporarily unavailable|timeout)",
    re.IGNORECASE,
)
_PLATFORM = re.compile(r"linux/[a-z0-9_]+")
_CACHE_MAX_AGE_SECONDS = 3_600


class CommandRunner(Protocol):
    def __call__(self, command: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]: ...


@dataclass(frozen=True, slots=True)
class ImageLock:
    plugin: str
    runtime: str
    compose_path: Path
    current_ref: str
    version_ref: str
    digest: str | None


@dataclass(frozen=True, slots=True)
class ResolvedImage:
    version_ref: str
    digest: str
    platforms: tuple[str, ...]

    @property
    def immutable_ref(self) -> str:
        return f"{self.version_ref}@{self.digest}"


class ImageResolutionError(RuntimeError):
    """One or more registry lookups failed after other results were retained."""

    def __init__(self, errors: dict[str, str], resolved: dict[str, ResolvedImage]) -> None:
        self.errors = errors
        self.resolved = resolved
        details = "; ".join(f"{reference}: {message}" for reference, message in sorted(errors.items()))
        super().__init__(f"could not resolve {len(errors)} image(s): {details}")


def _service_root(root: Path) -> Path:
    for candidate in (root / "toolkit" / "services", root / "services", root):
        if candidate.is_dir() and any(candidate.glob("*/compose.yaml")):
            return candidate
    raise FileNotFoundError(f"no service Compose applications found below {root}")


def discover_image_locks(root: Path, *, include_pinned: bool = False) -> tuple[ImageLock, ...]:
    """Return literal registry images that need resolution or verification."""
    locks: list[ImageLock] = []
    for compose_path in sorted(_service_root(root.resolve()).glob("*/compose.yaml")):
        try:
            document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, yaml.YAMLError) as exc:
            raise ValueError(f"cannot read {compose_path}: {exc}") from exc
        services = document.get("services") if isinstance(document, dict) else None
        if not isinstance(services, dict):
            raise ValueError(f"{compose_path} must contain a Compose services mapping")
        for runtime, service in services.items():
            if not isinstance(runtime, str) or not isinstance(service, dict):
                raise ValueError(f"{compose_path} contains an invalid runtime definition")
            if "build" in service:
                continue
            current = service.get("image")
            if not isinstance(current, str):
                raise ValueError(f"{compose_path}: runtime {runtime!r} must declare an image or build")
            if "@" in current:
                immutable = parse_immutable_image_reference(current)
                if not include_pinned:
                    continue
                version_ref = immutable.version_ref
                digest: str | None = immutable.digest
            else:
                version = parse_image_version_reference(current)
                version_ref = version.version_ref
                digest = None
            locks.append(
                ImageLock(
                    plugin=compose_path.parent.name,
                    runtime=runtime,
                    compose_path=compose_path,
                    current_ref=current,
                    version_ref=version_ref,
                    digest=digest,
                )
            )
    return tuple(locks)


def _docker_hub_path(version_ref: str) -> tuple[str, str] | None:
    parsed = parse_image_version_reference(version_ref)
    parts = parsed.repository.split("/")
    first = parts[0]
    if first in {"docker.io", "index.docker.io", "registry-1.docker.io"}:
        parts = parts[1:]
    elif "." in first or ":" in first or first == "localhost":
        return None
    if len(parts) == 1:
        parts.insert(0, "library")
    return "/".join(parts), parsed.tag


def _read_docker_hub_tag(path: str, tag: str, timeout: int) -> dict[str, object]:
    encoded_path = "/".join(urllib.parse.quote(part, safe="._-") for part in path.split("/"))
    encoded_tag = urllib.parse.quote(tag, safe="._-")
    request = urllib.request.Request(
        f"https://hub.docker.com/v2/repositories/{encoded_path}/tags/{encoded_tag}",
        headers={"Accept": "application/json", "User-Agent": "homelab-toolkit-image-lock/1"},
    )
    for attempt in range(4):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                body = response.read(4_000_001)
            if len(body) > 4_000_000:
                raise RuntimeError("Docker Hub tag metadata exceeded 4 MB")
            payload = json.loads(body.decode("utf-8"))
            if not isinstance(payload, dict):
                raise RuntimeError("Docker Hub returned non-object tag metadata")
            return payload
        except urllib.error.HTTPError as exc:
            if attempt == 3 or exc.code not in {429, 500, 502, 503, 504}:
                raise RuntimeError(f"Docker Hub metadata request failed with HTTP {exc.code}") from exc
            time.sleep(float(2**attempt))
        except (urllib.error.URLError, TimeoutError, OSError, UnicodeError, json.JSONDecodeError) as exc:
            if attempt == 3:
                raise RuntimeError(f"Docker Hub metadata request failed: {exc}") from exc
            time.sleep(float(2**attempt))
    raise RuntimeError("Docker Hub metadata retries exhausted")


def _resolved_image(version_ref: str, digest: object, platform_entries: object) -> ResolvedImage:
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise RuntimeError(f"registry returned no SHA-256 digest for {version_ref}")
    if not isinstance(platform_entries, list):
        raise RuntimeError(f"registry returned invalid platform metadata for {version_ref}")
    platforms: list[str] = []
    for entry in platform_entries:
        platform = entry.get("platform", entry) if isinstance(entry, dict) else None
        operating_system = platform.get("os") if isinstance(platform, dict) else None
        architecture = platform.get("architecture") if isinstance(platform, dict) else None
        if operating_system == "linux" and isinstance(architecture, str) and architecture != "unknown":
            name = f"linux/{architecture}"
            if name not in platforms:
                platforms.append(name)
    return ResolvedImage(version_ref=version_ref, digest=digest, platforms=tuple(platforms))


def resolve_image_reference(
    version_ref: str,
    *,
    runner: CommandRunner | None = None,
    timeout: int = 120,
) -> ResolvedImage:
    """Resolve a version tag to the registry's current OCI index digest."""
    parse_image_version_reference(version_ref)
    docker_hub = _docker_hub_path(version_ref)
    if docker_hub is not None:
        path, tag = docker_hub
        payload = _read_docker_hub_tag(path, tag, timeout)
        if payload.get("name") != tag:
            raise RuntimeError(f"Docker Hub returned metadata for the wrong tag: {version_ref}")
        return _resolved_image(version_ref, payload.get("digest"), payload.get("images"))
    command = [
        "docker",
        "buildx",
        "imagetools",
        "inspect",
        version_ref,
        "--format",
        "{{json .Manifest}}",
    ]
    execute = runner or subprocess.run
    result: subprocess.CompletedProcess[str] | None = None
    for attempt in range(4):
        try:
            result = execute(command, capture_output=True, text=True, timeout=timeout, check=False)
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(f"cannot resolve {version_ref}: {exc}") from exc
        if result.returncode == 0:
            break
        detail = (result.stderr or result.stdout).strip()[:500]
        if attempt == 3 or not _TRANSIENT_REGISTRY_ERROR.search(detail):
            raise RuntimeError(f"cannot resolve {version_ref}: {detail or f'exit {result.returncode}'}")
        time.sleep(float(2**attempt))
    if result is None or result.returncode != 0:
        raise RuntimeError(f"cannot resolve {version_ref}: registry retries exhausted")
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"registry returned invalid manifest metadata for {version_ref}") from exc
    digest = payload.get("digest") if isinstance(payload, dict) else None
    manifests = payload.get("manifests", []) if isinstance(payload, dict) else None
    return _resolved_image(version_ref, digest, manifests)


def resolve_image_locks(
    locks: tuple[ImageLock, ...],
    *,
    max_workers: int = 6,
    resolver: Callable[[str], ResolvedImage] | None = None,
    on_progress: Callable[[int, int, ResolvedImage], None] | None = None,
) -> dict[str, ResolvedImage]:
    """Resolve unique image tags concurrently and report bounded progress."""
    if not 1 <= max_workers <= 16:
        raise ValueError("image resolver workers must be between 1 and 16")
    references = sorted({lock.version_ref for lock in locks})
    resolve = resolver or resolve_image_reference
    resolved: dict[str, ResolvedImage] = {}
    errors: dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=min(max_workers, len(references) or 1)) as executor:
        pending = {executor.submit(resolve, reference): reference for reference in references}
        for completed, future in enumerate(as_completed(pending), start=1):
            reference = pending[future]
            try:
                image = future.result()
            except Exception as exc:  # noqa: BLE001 - preserve all independent registry results
                errors[reference] = str(exc)
                continue
            if image.version_ref != reference:
                errors[reference] = f"resolver returned metadata for {image.version_ref}"
                continue
            resolved[reference] = image
            if on_progress is not None:
                on_progress(completed, len(references), image)
    ordered = {reference: resolved[reference] for reference in references if reference in resolved}
    if errors:
        raise ImageResolutionError(errors, ordered)
    return ordered


def _cache_path(root: Path) -> Path:
    return root.resolve() / ".homelab-state" / "cache" / "image-locks.json"


def load_image_lock_cache(root: Path, *, now: float | None = None) -> dict[str, ResolvedImage]:
    """Load a short-lived local cache used only to resume registry resolution."""
    path = _cache_path(root)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(payload, dict) or payload.get("version") != 1:
        return {}
    created_at = payload.get("created_at")
    current_time = time.time() if now is None else now
    if not isinstance(created_at, (int, float)) or current_time - float(created_at) > _CACHE_MAX_AGE_SECONDS:
        return {}
    entries = payload.get("images")
    if not isinstance(entries, dict):
        return {}
    resolved: dict[str, ResolvedImage] = {}
    try:
        for reference, entry in entries.items():
            parse_image_version_reference(reference)
            if not isinstance(entry, dict):
                return {}
            digest = entry.get("digest")
            platforms = entry.get("platforms")
            if (
                not isinstance(digest, str)
                or not _DIGEST.fullmatch(digest)
                or not isinstance(platforms, list)
                or any(not isinstance(platform, str) or not _PLATFORM.fullmatch(platform) for platform in platforms)
            ):
                return {}
            resolved[reference] = ResolvedImage(reference, digest, tuple(platforms))
    except ValueError:
        return {}
    return resolved


def save_image_lock_cache(
    root: Path,
    resolved: dict[str, ResolvedImage],
    *,
    now: float | None = None,
) -> Path:
    """Atomically persist successful registry lookups for bounded retry reuse."""
    path = _cache_path(root)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    payload = {
        "version": 1,
        "created_at": time.time() if now is None else now,
        "images": {
            reference: {"digest": image.digest, "platforms": list(image.platforms)}
            for reference, image in sorted(resolved.items())
        },
    }
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def apply_image_locks(locks: tuple[ImageLock, ...], digests: dict[str, str]) -> tuple[Path, ...]:
    """Atomically replace only previously parsed Compose image scalars."""
    by_path: dict[Path, list[ImageLock]] = defaultdict(list)
    for lock in locks:
        digest = digests.get(lock.version_ref)
        if digest is None or not _DIGEST.fullmatch(digest):
            raise ValueError(f"missing resolved SHA-256 digest for {lock.version_ref}")
        by_path[lock.compose_path].append(lock)

    changed: list[Path] = []
    for path, path_locks in sorted(by_path.items()):
        content = path.read_text(encoding="utf-8")
        original = content
        for lock in path_locks:
            replacement = f"{lock.version_ref}@{digests[lock.version_ref]}"
            pattern = re.compile(
                rf"^(?P<prefix>\s*image:\s*){re.escape(lock.current_ref)}(?P<suffix>\s*)$",
                re.MULTILINE,
            )
            content, count = pattern.subn(rf"\g<prefix>{replacement}\g<suffix>", content, count=1)
            if count != 1:
                raise ValueError(f"parsed image field changed before update: {path}:{lock.runtime}")
        if content == original:
            continue
        mode = path.stat().st_mode
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.chmod(temporary, mode)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)
        changed.append(path)
    return tuple(changed)
