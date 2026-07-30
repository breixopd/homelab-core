"""Revisioned update plans derived from conservative registry scan results."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

import yaml

from toolkit.core.images.references import parse_image_version_reference, parse_immutable_image_reference
from toolkit.core.ops.version_policy import select_latest_compatible

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

_SERVICE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_TAG = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}$")
_MAX_CACHE_BYTES = 2 * 1024 * 1024


class UpdatePlanError(ValueError):
    """Update discovery data is stale, malformed, or unsafe to apply."""


@dataclass(frozen=True)
class UpdateCandidate:
    service: str
    current: str
    target: str
    current_image: str
    target_image: str
    changelog_url: str


@dataclass(frozen=True)
class UpdatePlan:
    revision: str
    checked_at: str
    candidates: tuple[UpdateCandidate, ...]


def _target_image(image: str, current: str, target: str) -> str:
    if not image or len(image) > 512 or "${" in image or not _TAG.fullmatch(target):
        raise UpdatePlanError("update image reference is not a mutable version tag")
    try:
        reference = parse_immutable_image_reference(image) if "@" in image else parse_image_version_reference(image)
    except ValueError as exc:
        raise UpdatePlanError("update image reference is not a reviewed version tag") from exc
    if reference.tag != current:
        raise UpdatePlanError("update image does not match its reported current tag")
    return f"{reference.repository}:{target}"


def _changelog(value: object) -> str:
    if value in (None, ""):
        return ""
    if not isinstance(value, str) or len(value) > 2_048 or any(ord(char) < 32 for char in value):
        raise UpdatePlanError("update changelog URL is invalid")
    parsed = urlsplit(value)
    if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
        raise UpdatePlanError("update changelog URL must be public HTTPS")
    return value


def build_update_plan(report: list[dict], current_images: dict[str, str], *, checked_at: str) -> UpdatePlan:
    try:
        timestamp = datetime.fromisoformat(checked_at)
    except ValueError as exc:
        raise UpdatePlanError("update plan timestamp is invalid") from exc
    if timestamp.tzinfo is None:
        raise UpdatePlanError("update plan timestamp must include a timezone")
    if not isinstance(report, list) or len(report) > 512:
        raise UpdatePlanError("update report is not a bounded list")
    candidates: list[UpdateCandidate] = []
    seen: set[str] = set()
    for item in report:
        if not isinstance(item, dict):
            raise UpdatePlanError("update report entry is invalid")
        if not item.get("needs_update"):
            continue
        service = item.get("service")
        current = item.get("current")
        target = item.get("latest")
        image = item.get("image")
        if (
            not isinstance(service, str)
            or not _SERVICE.fullmatch(service)
            or service in seen
            or not isinstance(current, str)
            or not isinstance(target, str)
            or not isinstance(image, str)
        ):
            raise UpdatePlanError("update report contains an invalid candidate")
        if current_images.get(service) != image:
            raise UpdatePlanError("update report no longer matches the active Compose model")
        if select_latest_compatible([target], current) != target:
            raise UpdatePlanError("update candidate changes version channel, flavor, or major version")
        candidates.append(
            UpdateCandidate(
                service=service,
                current=current,
                target=target,
                current_image=image,
                target_image=_target_image(image, current, target),
                changelog_url=_changelog(item.get("changelog_url")),
            )
        )
        seen.add(service)
    candidates.sort(key=lambda candidate: candidate.service)
    content = {
        "checked_at": checked_at,
        "candidates": [candidate.__dict__ for candidate in candidates],
    }
    revision = sha256(json.dumps(content, separators=(",", ":"), sort_keys=True).encode("utf-8")).hexdigest()
    return UpdatePlan(revision=revision, checked_at=checked_at, candidates=tuple(candidates))


def load_update_cache(root: Path) -> tuple[str, list[dict]] | None:
    """Load the bounded scanner cache without following links."""
    path = root.resolve() / "generated" / "updates-cache.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise UpdatePlanError("update cache cannot be opened safely") from exc
    try:
        content = os.read(descriptor, _MAX_CACHE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(content) > _MAX_CACHE_BYTES:
        raise UpdatePlanError("update cache exceeds its size limit")
    try:
        raw = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise UpdatePlanError("update cache is not valid JSON") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("cached_at"), int | float):
        raise UpdatePlanError("update cache envelope is invalid")
    updates = raw.get("updates")
    if not isinstance(updates, list):
        raise UpdatePlanError("update cache entries are invalid")
    checked_at = datetime.fromtimestamp(float(raw["cached_at"]), UTC).isoformat()
    return checked_at, updates


def load_current_update_plan(root: Path, cfg: Config) -> UpdatePlan | None:
    """Build a plan only when cached discovery still matches active desired state."""
    cached = load_update_cache(root)
    if cached is None:
        return None
    from toolkit.core.generate.compose_assemble import apply_manifest_release_versions, assemble_compose_text
    from toolkit.core.ops.release_state import load_active_release

    checked_at, report = cached
    document = yaml.safe_load(assemble_compose_text(root, cfg, include_release=False))
    apply_manifest_release_versions(root, cfg, document)
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        raise UpdatePlanError("active Compose services are unavailable")
    current_images = {
        name: service["image"]
        for name, service in services.items()
        if isinstance(service, dict) and isinstance(service.get("image"), str)
    }
    active = load_active_release(root)
    if active is not None:
        current_images.update(
            {service: image for service, image in active.versions.items() if service in current_images}
        )
    return build_update_plan(report, current_images, checked_at=checked_at)


def write_update_scan_compose(root: Path, cfg: Config) -> Path:
    """Write a scanner model containing reviewed version tags, never digest-only refs."""
    from toolkit.core.generate.compose_assemble import (
        apply_manifest_release_versions,
        apply_release_images,
        assemble_compose_text,
    )
    from toolkit.core.generate.generate import _atomic_write
    from toolkit.core.ops.release_state import load_active_release

    document = yaml.safe_load(assemble_compose_text(root, cfg, include_release=False))
    apply_manifest_release_versions(root, cfg, document)
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        raise UpdatePlanError("active Compose services are unavailable")
    active = load_active_release(root)
    if active is not None:
        apply_release_images(
            document,
            {service: image for service, image in active.versions.items() if service in services},
        )
    path = root.resolve() / "generated" / "update-scan-compose.yaml"
    _atomic_write(path, yaml.safe_dump(document, sort_keys=False, width=120))
    return path
