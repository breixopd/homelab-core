"""Immutable digest-pinned runtime release state."""

from __future__ import annotations

import json
import os
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from hashlib import sha256
from pathlib import Path

_MAX_RELEASE_BYTES = 256 * 1024
_SERVICE = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_DIGEST_IMAGE = re.compile(r"^[^\s@]{1,440}@sha256:[0-9a-f]{64}$")


class ReleaseStateError(ValueError):
    """Release state is malformed, unsafe, or has been modified."""


@dataclass(frozen=True)
class ReleaseState:
    revision: str
    created_at: str
    images: dict[str, str]
    versions: dict[str, str]
    schema_version: int = 1


@dataclass(frozen=True)
class RollbackRelease:
    expected_active_revision: str
    previous: ReleaseState | None
    revision: str


@dataclass(frozen=True)
class RecoveryRelease:
    """A failed release that must be reconciled back to its previous state."""

    previous: ReleaseState | None
    failed: ReleaseState
    revision: str


def _payload(created_at: str, images: dict[str, str], versions: dict[str, str]) -> dict[str, object]:
    return {
        "schema_version": 1,
        "created_at": created_at,
        "images": dict(sorted(images.items())),
        "versions": dict(sorted(versions.items())),
    }


def _revision(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return sha256(canonical).hexdigest()


def _validate_images(images: object) -> dict[str, str]:
    if not isinstance(images, dict) or not images or len(images) > 512:
        raise ReleaseStateError("release images must be a non-empty bounded mapping")
    validated: dict[str, str] = {}
    for service, image in images.items():
        if not isinstance(service, str) or not _SERVICE.fullmatch(service):
            raise ReleaseStateError("release contains an invalid service name")
        if not isinstance(image, str) or not _DIGEST_IMAGE.fullmatch(image):
            raise ReleaseStateError("release images must use lowercase sha256 digest references")
        validated[service] = image
    return dict(sorted(validated.items()))


def _validate_versions(versions: object, services: set[str]) -> dict[str, str]:
    if not isinstance(versions, dict) or set(versions) != services:
        raise ReleaseStateError("release versions must exactly match digest-pinned services")
    validated: dict[str, str] = {}
    for service, image in versions.items():
        if (
            not isinstance(image, str)
            or len(image) > 512
            or "@" in image
            or "${" in image
            or any(char.isspace() for char in image)
        ):
            raise ReleaseStateError("release versions must use explicit image tags")
        separator = image.rfind(":")
        if separator <= image.rfind("/") or not re.fullmatch(
            r"[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}", image[separator + 1 :]
        ):
            raise ReleaseStateError("release versions must use explicit image tags")
        validated[service] = image
    return dict(sorted(validated.items()))


def build_release(images: dict[str, str], versions: dict[str, str], *, created_at: str) -> ReleaseState:
    validated = _validate_images(images)
    validated_versions = _validate_versions(versions, set(validated))
    try:
        parsed = datetime.fromisoformat(created_at)
    except ValueError as exc:
        raise ReleaseStateError("release timestamp is invalid") from exc
    if parsed.tzinfo is None:
        raise ReleaseStateError("release timestamp must include a timezone")
    payload = _payload(created_at, validated, validated_versions)
    return ReleaseState(
        revision=_revision(payload),
        created_at=created_at,
        images=validated,
        versions=validated_versions,
    )


def active_release_path(root: Path) -> Path:
    return root.resolve() / ".homelab-state" / "releases" / "active.json"


def rollback_release_path(root: Path) -> Path:
    return root.resolve() / ".homelab-state" / "releases" / "rollback.json"


def recovery_release_path(root: Path) -> Path:
    return root.resolve() / ".homelab-state" / "releases" / "recovery.json"


def clear_rollback_release(root: Path) -> None:
    rollback_release_path(root).unlink(missing_ok=True)


def clear_recovery_release(root: Path) -> None:
    recovery_release_path(root).unlink(missing_ok=True)


def load_active_release(root: Path) -> ReleaseState | None:
    path = active_release_path(root)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReleaseStateError("active release cannot be opened safely") from exc
    try:
        content = os.read(descriptor, _MAX_RELEASE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(content) > _MAX_RELEASE_BYTES:
        raise ReleaseStateError("active release exceeds its size limit")
    try:
        raw = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseStateError("active release is not valid JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "revision", "created_at", "images", "versions"}:
        raise ReleaseStateError("active release has an invalid schema")
    if raw["schema_version"] != 1 or not isinstance(raw["revision"], str):
        raise ReleaseStateError("active release has an unsupported schema")
    release = build_release(raw["images"], raw["versions"], created_at=raw["created_at"])
    if release.revision != raw["revision"]:
        raise ReleaseStateError("active release revision does not match its content")
    return release


def _release_dict(release: ReleaseState) -> dict[str, object]:
    rebuilt = build_release(release.images, release.versions, created_at=release.created_at)
    if rebuilt != release:
        raise ReleaseStateError("release object is not canonical")
    return {
        "schema_version": release.schema_version,
        "revision": release.revision,
        "created_at": release.created_at,
        "images": release.images,
        "versions": release.versions,
    }


def _write_content(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)
    return path


def write_active_release(root: Path, release: ReleaseState | None) -> Path:
    path = active_release_path(root)
    if release is None:
        path.unlink(missing_ok=True)
        return path
    content = json.dumps(_release_dict(release), indent=2, sort_keys=True) + "\n"
    return _write_content(path, content)


def write_rollback_release(
    root: Path,
    *,
    expected_active_revision: str,
    previous: ReleaseState | None,
) -> Path:
    if not re.fullmatch(r"[0-9a-f]{64}", expected_active_revision):
        raise ReleaseStateError("rollback active revision is invalid")
    payload: dict[str, object] = {
        "schema_version": 1,
        "expected_active_revision": expected_active_revision,
        "previous": _release_dict(previous) if previous is not None else None,
    }
    payload["revision"] = _revision(payload)
    return _write_content(
        rollback_release_path(root),
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def load_rollback_release(root: Path) -> RollbackRelease | None:
    path = rollback_release_path(root)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReleaseStateError("rollback release cannot be opened safely") from exc
    try:
        content = os.read(descriptor, _MAX_RELEASE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(content) > _MAX_RELEASE_BYTES:
        raise ReleaseStateError("rollback release exceeds its size limit")
    try:
        raw = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseStateError("rollback release is not valid JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "revision",
        "expected_active_revision",
        "previous",
    }:
        raise ReleaseStateError("rollback release has an invalid schema")
    revision = raw.pop("revision", None)
    if raw.get("schema_version") != 1 or revision != _revision(raw):
        raise ReleaseStateError("rollback release revision does not match its content")
    expected = raw.get("expected_active_revision")
    if not isinstance(expected, str) or not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ReleaseStateError("rollback release active revision is invalid")
    previous_raw = raw.get("previous")
    previous: ReleaseState | None
    if previous_raw is None:
        previous = None
    elif isinstance(previous_raw, dict) and set(previous_raw) == {
        "schema_version",
        "revision",
        "created_at",
        "images",
        "versions",
    }:
        previous = build_release(
            previous_raw["images"],
            previous_raw["versions"],
            created_at=previous_raw["created_at"],
        )
        if previous.schema_version != previous_raw["schema_version"] or previous.revision != previous_raw["revision"]:
            raise ReleaseStateError("rollback previous release revision is invalid")
    else:
        raise ReleaseStateError("rollback previous release is invalid")
    return RollbackRelease(expected_active_revision=expected, previous=previous, revision=revision)


def write_recovery_release(root: Path, *, previous: ReleaseState | None, failed: ReleaseState) -> Path:
    """Durably record an automatic rollback before attempting the repair deploy."""

    payload: dict[str, object] = {
        "schema_version": 1,
        "previous": _release_dict(previous) if previous is not None else None,
        "failed": _release_dict(failed),
    }
    payload["revision"] = _revision(payload)
    return _write_content(
        recovery_release_path(root),
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
    )


def _embedded_release(raw: object, *, field: str, allow_none: bool) -> ReleaseState | None:
    if raw is None and allow_none:
        return None
    if not isinstance(raw, dict) or set(raw) != {
        "schema_version",
        "revision",
        "created_at",
        "images",
        "versions",
    }:
        raise ReleaseStateError(f"recovery {field} release is invalid")
    release = build_release(raw["images"], raw["versions"], created_at=raw["created_at"])
    if raw["schema_version"] != release.schema_version or raw["revision"] != release.revision:
        raise ReleaseStateError(f"recovery {field} release revision is invalid")
    return release


def load_recovery_release(root: Path) -> RecoveryRelease | None:
    path = recovery_release_path(root)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise ReleaseStateError("release recovery cannot be opened safely") from exc
    try:
        content = os.read(descriptor, _MAX_RELEASE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(content) > _MAX_RELEASE_BYTES:
        raise ReleaseStateError("release recovery exceeds its size limit")
    try:
        raw = json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise ReleaseStateError("release recovery is not valid JSON") from exc
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "revision", "previous", "failed"}:
        raise ReleaseStateError("release recovery has an invalid schema")
    revision = raw.pop("revision", None)
    if raw.get("schema_version") != 1 or revision != _revision(raw):
        raise ReleaseStateError("release recovery revision does not match its content")
    previous = _embedded_release(raw.get("previous"), field="previous", allow_none=True)
    failed = _embedded_release(raw.get("failed"), field="failed", allow_none=False)
    if failed is None:
        raise ReleaseStateError("release recovery is missing its failed release")
    return RecoveryRelease(previous=previous, failed=failed, revision=revision)
