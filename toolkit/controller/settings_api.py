"""Controller-owned configuration and secret mutations."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast

from toolkit.controller.read_models import (
    SecretInventory,
    SecretMutationResult,
    SecretStatus,
    SecretStorageMode,
    SecretUpdateRequest,
)
from toolkit.core.config.config import Config, load_config
from toolkit.core.config.storage import config_path, secrets_path
from toolkit.core.deploy.operation_lease import LeaseBusyError, OperationLease
from toolkit.core.ops.vpn import filter_vpn_specs
from toolkit.core.secrets.secrets import (
    RotationPolicy,
    SecretSpec,
    SecretTier,
    ensure_sops_ready,
    generate_all_secrets,
    get_required_secrets,
    load_secrets_plaintext,
    rotate_secrets,
    save_secrets_plaintext,
    secret_storage_mode,
    secrets_encryption_available,
)


class SecretMutationError(RuntimeError):
    pass


@contextmanager
def _secret_mutation_lease(root: Path, operation: str) -> Iterator[None]:
    try:
        lease = OperationLease.acquire(root, operation)
    except LeaseBusyError as exc:
        raise SecretMutationError("Another deployment or mutation is already running") from exc
    try:
        yield
    finally:
        lease.release()


@contextmanager
def _secret_lock(root: Path) -> Iterator[None]:
    state_dir = root.resolve() / ".homelab-state"
    state_dir.mkdir(parents=True, exist_ok=True)
    state_dir.chmod(0o700)
    path = state_dir / "secrets.lock"
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC | os.O_NOFOLLOW, 0o600)
    os.fchmod(descriptor, 0o600)
    with os.fdopen(descriptor, "a+", encoding="utf-8") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _required_specs(cfg: Config) -> list[SecretSpec]:
    return get_required_secrets(cfg)


def _inventory(root: Path, cfg: Config, values: dict[str, str]) -> SecretInventory:
    path = secrets_path(root)
    storage_mode = secret_storage_mode(path) if path.exists() else "missing"
    if storage_mode not in {"encrypted", "plaintext", "missing"}:
        raise RuntimeError("secret storage returned an invalid mode")
    visible_specs = filter_vpn_specs(_required_specs(cfg), values)
    entries = [
        SecretStatus(
            name=spec.name,
            isConfigured=bool(values.get(spec.name))
            or (spec.tier is SecretTier.DERIVED and bool(values.get("SSO_USER_PASSWORD"))),
            tier=spec.tier.value,
            rotationPolicy=spec.rotation.value,
            description=spec.description,
        )
        for spec in visible_specs
    ]
    return SecretInventory(
        owner_email=cfg.email,
        storage_mode=cast(SecretStorageMode, storage_mode),
        encryption_available=secrets_encryption_available(),
        entries=entries,
    )


def read_secret_inventory(root: Path) -> SecretInventory:
    root = root.resolve()
    with _secret_lock(root):
        cfg = load_config(config_path(root))
        path = secrets_path(root)
        values = load_secrets_plaintext(path) if path.exists() else {}
        return _inventory(root, cfg, values)


def update_secret_values(root: Path, request: SecretUpdateRequest) -> SecretMutationResult:
    root = root.resolve()
    with _secret_mutation_lease(root, "secret-update"), _secret_lock(root):
        cfg = load_config(config_path(root))
        path = secrets_path(root)
        current = load_secrets_plaintext(path) if path.exists() else {}
        allowed = {spec.name for spec in _required_specs(cfg) if spec.tier is SecretTier.USER}
        allowed.add("SSO_USER_PASSWORD")
        rejected = sorted(set(request.values) - allowed)
        if rejected:
            raise SecretMutationError(f"Secret is not user-configurable: {', '.join(rejected)}")
        if any(not value.strip() for value in request.values.values()):
            raise SecretMutationError("Secret values must not be blank")
        changed = sorted(name for name, value in request.values.items() if current.get(name) != value)
        if changed:
            current.update(request.values)
            save_secrets_plaintext(current, path)
        return SecretMutationResult(changed_names=changed, inventory=_inventory(root, cfg, current))


def generate_secret_values(root: Path) -> SecretMutationResult:
    root = root.resolve()
    with _secret_mutation_lease(root, "secret-generate"), _secret_lock(root):
        cfg = load_config(config_path(root))
        path = secrets_path(root)
        ensure_sops_ready(root)
        current = load_secrets_plaintext(path) if path.exists() else {}
        merged = generate_all_secrets(_required_specs(cfg), current, root=root)
        changed = sorted(name for name, value in merged.items() if value and current.get(name) != value)
        if changed:
            save_secrets_plaintext(merged, path)
        return SecretMutationResult(changed_names=changed, inventory=_inventory(root, cfg, merged))


def rotatable_generated_secret_names(root: Path) -> list[str]:
    root = root.resolve()
    with _secret_lock(root):
        cfg = load_config(config_path(root))
        return sorted(
            spec.name
            for spec in _required_specs(cfg)
            if spec.tier is SecretTier.GENERATED and spec.rotation is not RotationPolicy.PERSISTENT
        )


def rotate_secret_values(root: Path, names: list[str]) -> tuple[dict[str, str | None], dict[str, str]]:
    """Rotate selected generated secrets and retain an in-memory rollback snapshot."""
    root = root.resolve()
    with _secret_lock(root):
        cfg = load_config(config_path(root))
        path = secrets_path(root)
        current = load_secrets_plaintext(path) if path.exists() else {}
        requested = list(dict.fromkeys(names))
        specs = {spec.name: spec for spec in _required_specs(cfg)}
        if set(requested) - set(specs):
            raise SecretMutationError("Unknown secret rotation target")
        if any(
            specs[name].tier is not SecretTier.GENERATED or specs[name].rotation is RotationPolicy.PERSISTENT
            for name in requested
        ):
            raise SecretMutationError("One or more selected secrets are not automatically rotatable")
        before = {name: current.get(name) for name in requested}
        rotated = rotate_secrets(root, requested)
        return before, rotated


def restore_secret_values(root: Path, before: dict[str, str | None], expected: dict[str, str]) -> None:
    root = root.resolve()
    with _secret_lock(root):
        path = secrets_path(root)
        current = load_secrets_plaintext(path) if path.exists() else {}
        if any(current.get(name) != value for name, value in expected.items()):
            raise SecretMutationError("Secret values changed concurrently; refusing rollback")
        restored = dict(current)
        for name, value in before.items():
            (restored.__setitem__(name, value) if value is not None else restored.pop(name, None))
        save_secrets_plaintext(restored, path)
