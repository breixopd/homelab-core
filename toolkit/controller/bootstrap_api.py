"""Controller-owned first-run initialization and recovery detection."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import shutil
import stat
import tempfile
import uuid
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import yaml

from toolkit.controller.read_models import (
    BootstrapCategory,
    BootstrapInitializeRequest,
    BootstrapInitializeResult,
    BootstrapPhase,
    BootstrapService,
    BootstrapServiceSecretConditionView,
    BootstrapServiceSecretView,
    BootstrapServiceSettingView,
    BootstrapStatus,
    BootstrapView,
)
from toolkit.controller.store import ControllerStore
from toolkit.core.compose.registry import enabled_categories, load_all
from toolkit.core.config.config import Config, load_config, save_config, save_local_config
from toolkit.core.config.storage import config_path, secrets_path
from toolkit.core.manifest.catalog import load_service_catalog
from toolkit.core.manifest.setup import active_setup_secrets, prepare_bootstrap_credentials
from toolkit.core.secrets.secrets import (
    ensure_sops_ready,
    generate_all_secrets,
    get_required_secrets,
    load_secrets_plaintext,
    secrets_file_is_encrypted,
    sops_encrypt,
)


class BootstrapInitializationError(RuntimeError):
    """Bootstrap input or durable state prevents safe initialization."""


_STATE_DIR = ".homelab-state"
_MANIFEST = "bootstrap.json"
_MANIFEST_VERSION = 1
_RESERVED_DIRECTORIES = (_STATE_DIR, "keys", "ssh")
_MANAGED_FILES = (
    ".sops.yaml",
    "keys/age.key",
    "ssh/homelab_admin_ed25519",
    "ssh/homelab_admin_ed25519.pub",
    "config.local.yaml",
    "config.yaml",
    "secrets.enc.yaml",
)
_PARTIAL_STATE_FILES = ("config.yaml", "config.local.yaml", "secrets.enc.yaml")
_ORPHANED_MATERIAL_FILES = (
    ".sops.yaml",
    "keys/age.key",
    "ssh/homelab_admin_ed25519",
    "ssh/homelab_admin_ed25519.pub",
)
_BASE_REQUIRED_CREDENTIALS = frozenset(
    {
        "CLOUDFLARE_API_TOKEN",
        "CLOUDFLARE_ZONE_ID",
        "SSO_USER_PASSWORD",
    }
)
_PROVISION_REQUIRED_CREDENTIALS = frozenset({"PROXMOX_API_TOKEN_ID", "PROXMOX_API_TOKEN_SECRET"})


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _exists_without_following(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    return True


def _is_regular_file(path: Path) -> bool:
    try:
        return stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        return False


def _manifest_path(root: Path) -> Path:
    return root / _STATE_DIR / _MANIFEST


def _reserved_directories_are_safe(root: Path) -> bool:
    for relative in _RESERVED_DIRECTORIES:
        path = root / relative
        if not _exists_without_following(path):
            continue
        mode = path.lstat().st_mode
        if not stat.S_ISDIR(mode) or stat.S_ISLNK(mode):
            return False
    return True


def _read_valid_manifest(root: Path) -> dict[str, object] | None:
    path = _manifest_path(root)
    if not _is_regular_file(path):
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or raw.get("version") != _MANIFEST_VERSION:
        return None
    files = raw.get("files")
    if not isinstance(files, dict) or set(files) != set(_MANAGED_FILES):
        return None
    if not all(
        isinstance(name, str) and isinstance(digest, str) and len(digest) == 64 for name, digest in files.items()
    ):
        return None
    config_revision = raw.get("config_revision")
    if not isinstance(config_revision, str) or config_revision != files.get("config.yaml"):
        return None
    return raw


def bootstrap_phase(root: Path) -> BootstrapPhase:
    """Return readiness derived from a committed manifest and verified files."""
    root = root.resolve()
    if not _reserved_directories_are_safe(root):
        return "recovery_required"
    manifest_path = _manifest_path(root)
    manifest = _read_valid_manifest(root)
    if manifest is None:
        if _exists_without_following(manifest_path) or any(
            _exists_without_following(root / relative) for relative in _PARTIAL_STATE_FILES
        ):
            return "recovery_required"
        return "uninitialized"

    files = manifest["files"]
    if not isinstance(files, dict):
        return "recovery_required"
    try:
        for relative in _MANAGED_FILES:
            path = root / relative
            expected = files[relative]
            if not _is_regular_file(path) or _sha256_file(path) != expected:
                return "recovery_required"
        if not secrets_file_is_encrypted(secrets_path(root)):
            return "recovery_required"
        load_config(config_path(root))
        load_secrets_plaintext(secrets_path(root))
    except (OSError, RuntimeError, ValueError, yaml.YAMLError):
        return "recovery_required"
    return "ready"


def read_bootstrap_status(root: Path, store: ControllerStore) -> BootstrapStatus:
    capability, session = store.bootstrap_access_state()
    return BootstrapStatus(
        phase=bootstrap_phase(root),
        has_active_capability=capability,
        has_active_session=session,
    )


def _category_preview() -> list[BootstrapCategory]:
    load_all()
    config = Config.model_validate(
        {
            "domain": "example.com",
            "email": "operator@example.com",
        }
    )
    preview: list[BootstrapCategory] = []
    for category in enabled_categories(config):
        services = category.services(config)
        preview.append(
            BootstrapCategory(
                name=category.name,
                label=category.label,
                description=category.description,
                node=category.runtime_node(config),
                service_count=len(services),
                services=[BootstrapService(name=service.name, label=service.label) for service in services],
            )
        )
    return preview


def read_bootstrap_view(root: Path, store: ControllerStore, session_token: str) -> BootstrapView:
    store.validate_bootstrap_grant(session_token)
    status = read_bootstrap_status(root, store)
    if status.phase != "uninitialized":
        raise BootstrapInitializationError("Bootstrap initialization is not available in the current state")
    catalog = load_service_catalog()
    settings: list[BootstrapServiceSettingView] = []
    secrets: list[BootstrapServiceSecretView] = []
    for manifest in catalog.manifests:
        for setting in manifest.management.settings:
            if not setting.setup:
                continue
            settings.append(
                BootstrapServiceSettingView(
                    service=manifest.name,
                    service_label=manifest.label,
                    key=setting.key,
                    label=setting.label,
                    description=setting.description,
                    type=setting.type,
                    default=setting.default,
                    minimum=setting.minimum,
                    maximum=setting.maximum,
                    step=setting.step,
                    choices=list(setting.choices),
                )
            )
        for secret in manifest.required_secrets:
            setup = secret.setup
            if setup is None:
                continue
            secrets.append(
                BootstrapServiceSecretView(
                    service=manifest.name,
                    name=secret.name,
                    label=setup.label,
                    description=secret.description,
                    input=setup.input,
                    required=setup.required,
                    conditions=[
                        BootstrapServiceSecretConditionView(
                            setting=predicate.setting,
                            values=(
                                [cast(bool | int | float | str, predicate.equals)]
                                if "equals" in predicate.model_fields_set
                                else list(predicate.one_of)
                            ),
                        )
                        for predicate in setup.when
                        if predicate.setting is not None
                    ],
                )
            )
    return BootstrapView(
        status=status,
        categories=_category_preview(),
        service_settings=settings,
        service_secrets=secrets,
    )


def _validate_desired_state(request: BootstrapInitializeRequest) -> Config:
    desired = request.desired_state
    try:
        ZoneInfo(desired.timezone)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise BootstrapInitializationError("A valid IANA timezone is required") from exc

    proxmox: dict[str, object] = {"provision_machines": False}
    if desired.deployment_mode == "provision":
        proxmox_url = urlparse(desired.proxmox_api_url)
        if proxmox_url.scheme != "https" or not proxmox_url.hostname or proxmox_url.username or proxmox_url.password:
            raise BootstrapInitializationError("The Proxmox API URL must be an HTTPS origin without credentials")
        if not desired.proxmox_node or not desired.proxmox_storage:
            raise BootstrapInitializationError("A Proxmox node and storage are required for provisioning")
        proxmox = {
            "api_url": desired.proxmox_api_url.strip(),
            "node": desired.proxmox_node.strip(),
            "lxc_storage": desired.proxmox_storage.strip(),
            "provision_machines": True,
        }

    catalog = load_service_catalog()
    setup_settings = {
        (manifest.name, setting.key)
        for manifest in catalog.manifests
        for setting in manifest.management.settings
        if setting.setup
    }
    submitted_settings = {(service, key) for service, values in desired.service_settings.items() for key in values}
    unsupported_settings = sorted(submitted_settings - setup_settings)
    if unsupported_settings:
        names = ", ".join(f"{service}.{key}" for service, key in unsupported_settings)
        raise BootstrapInitializationError(f"Service settings not available during setup: {names}")

    try:
        return Config.model_validate(
            {
                "domain": desired.domain.strip().lower(),
                "email": desired.email.strip().lower(),
                "timezone": desired.timezone,
                "service_settings": desired.service_settings,
                "proxmox": proxmox,
                "dns": {"provider": "cloudflare", "proxy_enabled": True},
            }
        )
    except ValueError as exc:
        raise BootstrapInitializationError("Bootstrap configuration is invalid") from exc


def _validated_credentials(request: BootstrapInitializeRequest, config: Config) -> dict[str, str]:
    values = {name: value.strip() for name, value in request.credential_values.items()}
    provisioning = request.desired_state.deployment_mode == "provision"
    mode_required = _PROVISION_REQUIRED_CREDENTIALS if provisioning else frozenset()
    if not provisioning and set(values).intersection(_PROVISION_REQUIRED_CREDENTIALS):
        raise BootstrapInitializationError("Proxmox credentials are not accepted in management mode")
    catalog = load_service_catalog()
    active = active_setup_secrets(config, catalog)
    supported = _BASE_REQUIRED_CREDENTIALS | mode_required | set(active)
    unsupported = sorted(set(values) - supported)
    if unsupported:
        raise BootstrapInitializationError("Bootstrap credentials contain unsupported names")

    required = (
        _BASE_REQUIRED_CREDENTIALS
        | mode_required
        | frozenset(
            name for name, (_manifest, secret) in active.items() if secret.setup is not None and secret.setup.required
        )
    )
    missing = sorted(name for name in required if not values.get(name))
    if missing:
        raise BootstrapInitializationError(f"Required bootstrap credentials are missing: {', '.join(missing)}")
    if len(values["SSO_USER_PASSWORD"]) < 16:
        raise BootstrapInitializationError("The owner password must contain at least 16 characters")
    if len(values["CLOUDFLARE_API_TOKEN"]) < 20:
        raise BootstrapInitializationError("The Cloudflare API token is invalid")
    zone_id = values["CLOUDFLARE_ZONE_ID"].lower()
    if len(zone_id) != 32 or any(character not in "0123456789abcdef" for character in zone_id):
        raise BootstrapInitializationError("The Cloudflare zone ID must contain 32 hexadecimal characters")
    if provisioning:
        if "@" not in values["PROXMOX_API_TOKEN_ID"] or "!" not in values["PROXMOX_API_TOKEN_ID"]:
            raise BootstrapInitializationError("The Proxmox API token ID is invalid")
        if len(values["PROXMOX_API_TOKEN_SECRET"]) < 16:
            raise BootstrapInitializationError("The Proxmox API token secret is invalid")

    try:
        return prepare_bootstrap_credentials(config, values)
    except ValueError as exc:
        raise BootstrapInitializationError("Service bootstrap credentials are invalid") from exc


def _ensure_secure_directory(path: Path, *, mode: int = 0o700) -> None:
    if _exists_without_following(path):
        file_mode = path.lstat().st_mode
        if not stat.S_ISDIR(file_mode) or stat.S_ISLNK(file_mode):
            raise BootstrapInitializationError("Bootstrap state storage is not a secure directory")
    else:
        path.mkdir(parents=True, mode=mode)
    path.chmod(mode)


@contextmanager
def _bootstrap_lock(root: Path) -> Iterator[None]:
    state_dir = root / _STATE_DIR
    _ensure_secure_directory(state_dir)
    flags = os.O_CREAT | os.O_RDWR | os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        descriptor = os.open(state_dir / "bootstrap.lock", flags, 0o600)
    except OSError as exc:
        raise BootstrapInitializationError("Bootstrap state lock is unavailable") from exc
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_yaml_file(path: Path, values: Mapping[str, str]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        yaml.safe_dump(dict(values), handle, default_flow_style=False, sort_keys=True)
        handle.flush()
        os.fsync(handle.fileno())
    path.chmod(0o600)


def _remove_orphaned_bootstrap_material(root: Path) -> None:
    """Clear reserved artifacts that cannot represent a committed installation alone."""
    if not _reserved_directories_are_safe(root):
        raise BootstrapInitializationError("Reserved bootstrap directories contain an unsafe file type")
    for relative in _ORPHANED_MATERIAL_FILES:
        path = root / relative
        if not _exists_without_following(path):
            continue
        if not _is_regular_file(path):
            raise BootstrapInitializationError("Reserved bootstrap material contains an unsafe file type")
        path.unlink()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _install_file_exclusive(source: Path, target: Path) -> None:
    if _exists_without_following(target.parent):
        parent_mode = target.parent.lstat().st_mode
        if not stat.S_ISDIR(parent_mode) or stat.S_ISLNK(parent_mode):
            raise BootstrapInitializationError("Bootstrap target storage is not a secure directory")
    else:
        target.parent.mkdir(parents=True, mode=0o700)
    if _exists_without_following(target):
        raise FileExistsError(target)
    os.link(source, target, follow_symlinks=False)
    source.unlink()
    _fsync_directory(target.parent)


def _build_manifest(stage: Path) -> dict[str, object]:
    files = {relative: _sha256_file(stage / relative) for relative in _MANAGED_FILES}
    return {
        "version": _MANIFEST_VERSION,
        "generation": str(uuid.uuid4()),
        "committed_at": datetime.now(UTC).isoformat(),
        "config_revision": files["config.yaml"],
        "files": files,
    }


def _write_manifest(stage: Path, manifest: Mapping[str, object]) -> Path:
    descriptor, name = tempfile.mkstemp(prefix="bootstrap-manifest-", suffix=".json", dir=stage)
    path = Path(name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(manifest, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        path.chmod(0o600)
        return path
    except BaseException:
        path.unlink(missing_ok=True)
        raise


def _prepare_staged_state(stage: Path, request: BootstrapInitializeRequest) -> tuple[Config, dict[str, str]]:
    config = _validate_desired_state(request)
    credentials = _validated_credentials(request, config)
    specs = get_required_secrets(config)
    generated = generate_all_secrets(specs, credentials, root=stage)
    public_key = generated.get("HOMELAB_SSH_PUBLIC_KEY", "").strip()
    private_key = generated.get("HOMELAB_SSH_PRIVATE_KEY", "").strip()
    if not public_key or not private_key:
        raise BootstrapInitializationError("A matched automation SSH identity could not be generated")

    config = config.model_copy(update={"proxmox": config.proxmox.model_copy(update={"ssh_public_key": public_key})})
    save_config(config, stage / "config.yaml")
    save_local_config(config, stage)
    _write_yaml_file(stage / "secrets.enc.yaml", generated)

    recipient = ensure_sops_ready(stage)
    if not recipient or not sops_encrypt(stage / "secrets.enc.yaml", root=stage, age_recipient=recipient):
        raise BootstrapInitializationError("Encrypted secret storage could not be initialized")
    if not secrets_file_is_encrypted(stage / "secrets.enc.yaml"):
        raise BootstrapInitializationError("Bootstrap refused to persist plaintext secrets")
    load_secrets_plaintext(stage / "secrets.enc.yaml")
    for relative in _MANAGED_FILES:
        if not _is_regular_file(stage / relative):
            raise BootstrapInitializationError("Bootstrap staging did not produce all required files")
    return config, generated


def initialize_bootstrap(
    root: Path,
    store: ControllerStore,
    request: BootstrapInitializeRequest,
    *,
    principal: str,
) -> BootstrapInitializeResult:
    """Validate, stage, encrypt, and atomically publish first-run state."""
    root = root.resolve()
    store.validate_bootstrap_grant(request.session_token)
    with _bootstrap_lock(root):
        if bootstrap_phase(root) != "uninitialized":
            raise BootstrapInitializationError("Bootstrap initialization is not available in the current state")
        _remove_orphaned_bootstrap_material(root)

        stage = root / _STATE_DIR / f"bootstrap-stage-{uuid.uuid4()}"
        _ensure_secure_directory(stage)
        try:
            _config, generated = _prepare_staged_state(stage, request)
            manifest = _build_manifest(stage)
            staged_manifest = _write_manifest(stage, manifest)
            for relative in _MANAGED_FILES:
                _install_file_exclusive(stage / relative, root / relative)
            _install_file_exclusive(staged_manifest, _manifest_path(root))
            if bootstrap_phase(root) != "ready":
                raise BootstrapInitializationError("Bootstrap committed invalid state; recovery is required")
            store.consume_bootstrap_grant(request.session_token, principal=principal)
            return BootstrapInitializeResult(
                config_revision=str(manifest["config_revision"]),
                configured_secret_names=sorted(name for name, value in generated.items() if value),
            )
        except BootstrapInitializationError:
            raise
        except Exception as exc:
            if bootstrap_phase(root) == "recovery_required":
                raise BootstrapInitializationError(
                    "Bootstrap was interrupted while committing state; recovery is required"
                ) from exc
            raise BootstrapInitializationError("Bootstrap initialization failed before state was committed") from exc
        finally:
            shutil.rmtree(stage, ignore_errors=True)
