"""Persist framework-owned paths and secrets after verified full deployments."""

from __future__ import annotations

import json
import os
import re
import shutil
import stat
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from toolkit.core.state.files import atomic_write_json

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.manifest.catalog import ServiceCatalog

_VERSION = 1
_MAX_LEDGER_BYTES = 1024 * 1024
_OWNER_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}$")
_SECRET_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


@dataclass(frozen=True, slots=True)
class OwnedPath:
    path: str
    service: str
    node: str


@dataclass(frozen=True, slots=True)
class OwnedSecret:
    name: str
    owner: str


@dataclass(frozen=True, slots=True)
class OwnershipLedger:
    generated: tuple[OwnedPath, ...] = ()
    config: tuple[OwnedPath, ...] = ()
    machines: tuple[str, ...] = ()
    secrets: tuple[OwnedSecret, ...] = ()


def ownership_ledger_path(root: Path) -> Path:
    return root.resolve() / ".homelab-state" / "service-ownership.json"


def _secure_state_directory(root: Path) -> Path:
    state_dir = root.resolve() / ".homelab-state"
    try:
        metadata = state_dir.lstat()
    except FileNotFoundError:
        state_dir.mkdir(mode=0o700)
        metadata = state_dir.lstat()
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise ValueError("homelab state path must be a real directory")
    descriptor = os.open(state_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        os.fchmod(descriptor, 0o700)
    finally:
        os.close(descriptor)
    return state_dir


def _owned_path(value: object, *, prefix: str) -> OwnedPath:
    if not isinstance(value, dict):
        raise ValueError("ownership path entry must be a mapping")
    record = OwnedPath(
        path=str(value.get("path", "")),
        service=str(value.get("service", "")),
        node=str(value.get("node", "")),
    )
    path = PurePosixPath(record.path)
    from toolkit.core.machines.models import validate_machine_id

    if (
        not record.service
        or not record.node
        or _OWNER_ID.fullmatch(record.service) is None
        or path.is_absolute()
        or len(path.parts) < 2
        or ".." in path.parts
        or not record.path.startswith(prefix)
    ):
        raise ValueError("ownership path entry is invalid")
    validate_machine_id(record.node)
    return record


def load_ownership_ledger(root: Path) -> OwnershipLedger:
    path = _secure_state_directory(root) / "service-ownership.json"
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return OwnershipLedger()
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > _MAX_LEDGER_BYTES:
            raise ValueError("service ownership ledger is not a bounded regular file")
        with os.fdopen(descriptor, encoding="utf-8") as stream:
            descriptor = -1
            payload = json.load(stream)
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("service ownership ledger is unreadable") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    if not isinstance(payload, dict) or payload.get("version") != _VERSION:
        raise ValueError("service ownership ledger version is unsupported")
    generated = tuple(_owned_path(value, prefix="generated/") for value in payload.get("generated", []))
    config = tuple(_owned_path(value, prefix="config/") for value in payload.get("config", []))
    machines_raw = payload.get("machines", [])
    secrets_raw = payload.get("secrets", [])
    if not isinstance(machines_raw, list) or not isinstance(secrets_raw, list):
        raise ValueError("service ownership ledger collections are invalid")
    from toolkit.core.machines.models import validate_machine_id

    machines = tuple(sorted({validate_machine_id(str(value)) for value in machines_raw}))
    secrets: list[OwnedSecret] = []
    for value in secrets_raw:
        if not isinstance(value, dict):
            raise ValueError("ownership secret entry must be a mapping")
        record = OwnedSecret(name=str(value.get("name", "")), owner=str(value.get("owner", "")))
        if _SECRET_NAME.fullmatch(record.name) is None or not record.owner:
            raise ValueError("ownership secret entry is invalid")
        secrets.append(record)
    return OwnershipLedger(generated=generated, config=config, machines=machines, secrets=tuple(secrets))


def current_ownership(config: Config, catalog: ServiceCatalog) -> OwnershipLedger:
    from toolkit.core.manifest.artifacts import compile_config_sources, compile_generated_artifacts
    from toolkit.core.secrets.secrets import get_required_secrets

    generated = tuple(
        OwnedPath(path=artifact.source_path, service=artifact.service, node=artifact.node)
        for artifact in compile_generated_artifacts(config, catalog)
    )
    config_paths = tuple(
        OwnedPath(path=source.path, service=source.service, node=source.node)
        for source in compile_config_sources(config, catalog)
    )
    service_secret_owners: dict[str, set[str]] = {}
    for manifest in catalog.manifests:
        for secret in manifest.required_secrets:
            service_secret_owners.setdefault(secret.name, set()).add(manifest.name)
    secret_owners = {name: "service:" + ",".join(sorted(owners)) for name, owners in service_secret_owners.items()}
    from toolkit.core.projects.secrets import project_database_secret_name

    for project in config.projects.entries:
        if project.database_service:
            secret_owners.setdefault(project_database_secret_name(project.subdomain), f"project:{project.subdomain}")
    for spec in get_required_secrets(config, catalog):
        secret_owners.setdefault(spec.name, "active")
    return OwnershipLedger(
        generated=generated,
        config=config_paths,
        machines=tuple(sorted(config.machines)),
        secrets=tuple(OwnedSecret(name=name, owner=owner) for name, owner in sorted(secret_owners.items())),
    )


def ownership_tombstones(root: Path, current: OwnershipLedger) -> OwnershipLedger:
    previous = load_ownership_ledger(root)
    generated_keys = {(item.path, item.node) for item in current.generated}
    config_keys = {(item.path, item.node) for item in current.config}
    secret_names = {item.name for item in current.secrets}
    return OwnershipLedger(
        generated=tuple(item for item in previous.generated if (item.path, item.node) not in generated_keys),
        config=tuple(item for item in previous.config if (item.path, item.node) not in config_keys),
        machines=tuple(sorted(set(previous.machines) - set(current.machines))),
        secrets=tuple(
            item
            for item in previous.secrets
            if item.name not in secret_names and item.owner.startswith(("service:", "project:"))
        ),
    )


def prune_local_ownership_tombstones(root: Path, current: OwnershipLedger) -> None:
    tombstones = ownership_tombstones(root, current)
    generated_root = root.resolve() / "generated"
    if generated_root.exists() and (generated_root.is_symlink() or not generated_root.is_dir()):
        raise ValueError("generated path must be a real directory")

    def remove_owned(target: Path) -> None:
        relative = target.relative_to(generated_root)
        cursor = generated_root
        for part in relative.parts[:-1]:
            cursor /= part
            if cursor.exists() and cursor.is_symlink():
                raise ValueError(f"refusing generated cleanup through symbolic link: {cursor}")
        if target.is_symlink() or not target.is_dir():
            target.unlink(missing_ok=True)
        else:
            shutil.rmtree(target)

    for item in tombstones.generated:
        remove_owned(root.resolve() / item.path)
    for machine in tombstones.machines:
        remove_owned(generated_root / machine)
        remove_owned(generated_root / "bundles" / machine)


def commit_ownership_ledger(root: Path, current: OwnershipLedger) -> tuple[str, ...]:
    """Prune previously tracked removed secrets, then atomically commit ownership."""
    tombstones = ownership_tombstones(root, current)
    removed_names = tuple(sorted(item.name for item in tombstones.secrets))
    if removed_names:
        from toolkit.core.config.storage import secrets_path
        from toolkit.core.secrets.secrets import load_secrets_plaintext, save_secrets_plaintext

        path = secrets_path(root)
        values = load_secrets_plaintext(path)
        for name in removed_names:
            values.pop(name, None)
        save_secrets_plaintext(values, path)
    payload: dict[str, Any] = {
        "version": _VERSION,
        "generated": [asdict(item) for item in current.generated],
        "config": [asdict(item) for item in current.config],
        "machines": list(current.machines),
        "secrets": [asdict(item) for item in current.secrets],
    }
    atomic_write_json(_secure_state_directory(root) / "service-ownership.json", payload, mode=0o600)
    return removed_names
