from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# Default repo root on hosts (matches automation/ansible group_vars repo_dest).
DEFAULT_HOMELAB_ROOT = "/opt/homelab"


@dataclass(frozen=True, slots=True)
class HomelabPaths:
    root: Path

    @property
    def config(self) -> Path:
        return self.root / "config.yaml"

    @property
    def secrets(self) -> Path:
        return self.root / "secrets.enc.yaml"

    @property
    def sops_config(self) -> Path:
        return self.root / ".sops.yaml"

    @property
    def generated_dir(self) -> Path:
        return self.root / "generated"


def resolve_homelab_root(root: str | Path | None = None, *, prefer_cwd: bool = False) -> Path:
    """Resolve homelab repo root (config.yaml directory).

    When ``prefer_cwd`` is true (CLI), walk up from the working directory if the
    explicit/default path has no ``config.yaml``. Module-level ``INSTALL_ROOT`` uses
    ``HOMELAB_ROOT`` or ``/opt/homelab`` only so imports stay stable in tests.
    """
    default_path = Path(DEFAULT_HOMELAB_ROOT).expanduser().resolve()
    explicit = Path(root).expanduser().resolve() if root is not None else None

    if explicit and (explicit / "config.yaml").is_file():
        return explicit

    # Honor explicit --root (install/tests) even when uninitialized; only walk cwd for default path.
    if explicit is not None and (not prefer_cwd or explicit != default_path):
        return explicit

    env_root = os.environ.get("HOMELAB_ROOT")
    if env_root:
        candidate = Path(env_root).expanduser().resolve()
        if (candidate / "config.yaml").is_file():
            return candidate

    if prefer_cwd or (explicit is not None and explicit == default_path):
        cwd = Path.cwd().resolve()
        for candidate in (cwd, *cwd.parents):
            if (candidate / "config.yaml").is_file():
                return candidate

    if explicit is not None:
        return explicit
    return default_path


INSTALL_ROOT = resolve_homelab_root()


def homelab_paths(root: str | Path | None = None) -> HomelabPaths:
    return HomelabPaths(resolve_homelab_root(root))


def homelab_state_path() -> Path:
    configured = os.environ.get("HOMELAB_STATE_PATH")
    if configured:
        return Path(configured).expanduser().resolve()

    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state")).expanduser()
    return (state_home / "homelab-toolkit").resolve()


def ensure_homelab_state_path() -> Path:
    path = homelab_state_path()
    path.mkdir(parents=True, exist_ok=True)
    return path


def ui_ssh_key_dir() -> Path:
    return ensure_homelab_state_path() / "ssh"


def ensure_ui_ssh_key_dir() -> Path:
    path = ui_ssh_key_dir()
    path.mkdir(parents=True, exist_ok=True)
    return path


def config_path(root: Path | None = None) -> Path:
    return homelab_paths(root).config


def secrets_path(root: Path | None = None) -> Path:
    return homelab_paths(root).secrets


def sops_config_path(root: Path | None = None) -> Path:
    return homelab_paths(root).sops_config


def env_path(vm: str, root: Path | None = None) -> Path:
    from toolkit.core.machines.models import validate_machine_id

    return homelab_paths(root).generated_dir / validate_machine_id(vm) / ".env"


def hook_env_path(vm: str, root: Path | None = None) -> Path:
    from toolkit.core.machines.models import validate_machine_id

    return homelab_paths(root).generated_dir / validate_machine_id(vm) / ".hooks.env"


def hook_bundle_path(vm: str, root: Path | None = None) -> Path:
    from toolkit.core.machines.models import validate_machine_id

    return homelab_paths(root).generated_dir / "bundles" / validate_machine_id(vm) / ".hooks.env"


def caddyfile_path(vm: str, root: Path | None = None) -> Path:
    return homelab_paths(root).generated_dir / vm / "Caddyfile"


def backup_path(original: Path) -> Path:
    return original.with_suffix(original.suffix + ".backup")
