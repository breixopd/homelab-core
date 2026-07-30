from __future__ import annotations

from pathlib import Path

import pytest
from toolkit.core.config.storage import (
    HomelabPaths,
    caddyfile_path,
    config_path,
    env_path,
    homelab_paths,
    hook_bundle_path,
    hook_env_path,
    secrets_path,
    sops_config_path,
)


def test_config_path(tmp_path: Path):
    result = config_path(tmp_path)
    assert result == tmp_path / "config.yaml"


def test_secrets_path(tmp_path: Path):
    result = secrets_path(tmp_path)
    assert result == tmp_path / "secrets.enc.yaml"


def test_sops_config_path(tmp_path: Path):
    result = sops_config_path(tmp_path)
    assert result == tmp_path / ".sops.yaml"


def test_env_path_infra(tmp_path: Path):
    result = env_path("infra", tmp_path)
    assert result == tmp_path / "generated" / "infra" / ".env"


def test_env_path_media(tmp_path: Path):
    result = env_path("media", tmp_path)
    assert result == tmp_path / "generated" / "media" / ".env"


def test_hook_env_path_is_separate_from_compose_environment(tmp_path: Path):
    result = hook_env_path("apps", tmp_path)
    assert result == tmp_path / "generated" / "apps" / ".hooks.env"
    assert result != env_path("apps", tmp_path)


def test_hook_bundle_path_is_controller_scoped(tmp_path: Path):
    assert hook_bundle_path("apps", tmp_path) == tmp_path / "generated" / "bundles" / "apps" / ".hooks.env"


@pytest.mark.parametrize("path", ("../keys", "apps/../../keys", "Apps"))
def test_environment_paths_reject_unsafe_machine_ids(tmp_path: Path, path: str):
    with pytest.raises(ValueError, match="invalid machine id"):
        env_path(path, tmp_path)
    with pytest.raises(ValueError, match="invalid machine id"):
        hook_env_path(path, tmp_path)


def test_caddyfile_path(tmp_path: Path):
    result = caddyfile_path("infra", tmp_path)
    assert result == tmp_path / "generated" / "infra" / "Caddyfile"


def test_config_path_with_string_path(tmp_path: Path):
    result = config_path(str(tmp_path))
    assert result == tmp_path / "config.yaml"


def test_secrets_path_with_string_path(tmp_path: Path):
    result = secrets_path(str(tmp_path))
    assert result == tmp_path / "secrets.enc.yaml"


def test_env_path_with_string_path(tmp_path: Path):
    result = env_path("apps", str(tmp_path))
    assert result == tmp_path / "generated" / "apps" / ".env"


def test_config_path_defaults_to_homelab_root(monkeypatch):
    from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT

    monkeypatch.delenv("HOMELAB_ROOT", raising=False)
    monkeypatch.chdir("/tmp")
    result = config_path()
    assert str(result).endswith(f"{DEFAULT_HOMELAB_ROOT}/config.yaml")


def test_homelab_paths_dataclass(tmp_path: Path):
    paths = homelab_paths(tmp_path)
    assert isinstance(paths, HomelabPaths)
    assert paths.root == tmp_path
    assert paths.config == tmp_path / "config.yaml"
    assert paths.secrets == tmp_path / "secrets.enc.yaml"
    assert paths.generated_dir == tmp_path / "generated"


def test_env_paths_are_unique(tmp_path: Path):
    infra = env_path("infra", tmp_path)
    media = env_path("media", tmp_path)
    assert infra != media
    assert infra.parent == tmp_path / "generated" / "infra"
    assert media.parent == tmp_path / "generated" / "media"
