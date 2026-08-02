from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from toolkit.core.compose.volume_permissions import fix_volume_permissions


def test_fix_volume_permissions_from_env_file(tmp_path: Path):
    root = tmp_path / "homelab"
    env_dir = root / "generated" / "infra"
    env_dir.mkdir(parents=True)
    data_dir = root / "data" / "caddy"
    env_dir.joinpath(".env").write_text(
        f"CADDY_DATA_SOURCE={data_dir}\n"
        f"PROMETHEUS_DATA_SOURCE={root / 'data' / 'prometheus'}\n"
        f"WAZUH_INDEXER_DATA_SOURCE={root / 'data' / 'wazuh-indexer'}\n"
    )

    chowned: list[tuple[Path, int, int]] = []

    def fake_chown(path, uid, gid):
        chowned.append((Path(path), uid, gid))

    with patch("os.chown", side_effect=fake_chown):
        logs = fix_volume_permissions(root, node="infra")

    assert data_dir.is_dir()
    assert (root / "data" / "prometheus" / "data").is_dir()
    assert any("caddy" in line.lower() for line in logs)
    assert any(p == data_dir and uid == 1000 for p, uid, _gid in chowned)


def test_fix_volume_permissions_builds_env_when_missing(tmp_path: Path, monkeypatch):
    from toolkit.core.config.config import Config, save_config
    from toolkit.core.config.storage import config_path

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = Config(domain="example.com", email="admin@example.com")
    save_config(cfg, config_path(root))

    grafana_dir = root / "data" / "grafana"
    monkeypatch.setattr(
        "toolkit.core.generate.generate._build_env_vars",
        lambda _cfg, _vm, _secrets, _root: {"GRAFANA_DATA_SOURCE": str(grafana_dir)},
    )
    monkeypatch.setattr("toolkit.core.secrets.secrets.load_secrets_plaintext", lambda _p: {})

    with patch("os.chown"):
        logs = fix_volume_permissions(root, node="infra")

    assert grafana_dir.is_dir()
    assert any("grafana" in line.lower() for line in logs)


def test_fix_volume_permissions_media_config_owners(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    env_dir = root / "generated" / "media"
    env_dir.mkdir(parents=True)
    env_dir.joinpath(".env").write_text(f"SEERR_CONFIG_SOURCE={root / 'data' / 'seerr' / 'config'}\n")

    with patch("os.chown"):
        logs = fix_volume_permissions(root, node="media")

    seerr_cfg = root / "data" / "seerr" / "config"
    assert seerr_cfg.is_dir()
    assert (seerr_cfg / "logs").is_dir()
    assert any("data/seerr/config" in line for line in logs)
    assert not any("Missing generated config path" in line for line in logs)


def test_fix_volume_permissions_creates_declared_media_library_layout(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()

    with patch("os.chown"):
        logs = fix_volume_permissions(root, node="media")

    assert all((root / "media" / name).is_dir() for name in ("tv", "movies", "downloads"))
    assert any("/media" in line for line in logs)


def test_fix_volume_permissions_infra_generated_secret_owners(tmp_path: Path):
    root = tmp_path / "homelab"
    authelia = root / "generated" / "authelia"
    headscale = root / "generated" / "headscale"
    wazuh = root / "generated" / "wazuh"
    authelia.mkdir(parents=True)
    headscale.mkdir(parents=True)
    wazuh.mkdir(parents=True)
    authelia.joinpath("configuration.yml").write_text("secret")
    wazuh.joinpath("internal_users.yml").write_text("secret")
    chowned: list[tuple[Path, int, int]] = []

    with patch("os.chown", side_effect=lambda path, uid, gid: chowned.append((Path(path), uid, gid))):
        logs = fix_volume_permissions(root, node="infra")

    assert (authelia, 1000, 1000) in chowned
    assert (headscale, 0, 0) in chowned
    assert (wazuh, 1000, 1000) in chowned
    assert any("generated/authelia" in line for line in logs)
    assert any("generated/wazuh" in line for line in logs)


def test_fix_volume_permissions_chown_oserror_logged(tmp_path: Path):
    root = tmp_path / "homelab"
    env_dir = root / "generated" / "infra"
    env_dir.mkdir(parents=True)
    data_dir = root / "data" / "ntfy"
    env_dir.joinpath(".env").write_text(f"NTFY_CACHE_SOURCE={data_dir}\n")

    with patch("os.chown", side_effect=OSError(1, "Operation not permitted")):
        logs = fix_volume_permissions(root, node="infra")

    assert data_dir.is_dir()
    assert any("Could not chown" in line for line in logs)


def test_fix_volume_permissions_skips_unknown_keys(tmp_path: Path):
    root = tmp_path / "homelab"
    env_dir = root / "generated" / "infra"
    env_dir.mkdir(parents=True)
    env_dir.joinpath(".env").write_text("UNKNOWN_DATA_SOURCE=/tmp/nowhere\n")

    with patch("os.chown") as mock_chown:
        logs = fix_volume_permissions(root, node="infra")

    assert all(Path(call.args[0]) != Path("/tmp/nowhere") for call in mock_chown.call_args_list)
    assert all("/tmp/nowhere" not in line for line in logs)


def test_fix_volume_permissions_uses_source_subpath_and_creates_shared_layout_without_recursive_chown(
    tmp_path: Path, monkeypatch
):
    root = tmp_path / "homelab"
    env_dir = root / "generated" / "apps"
    env_dir.mkdir(parents=True)
    install_root = root / "runtime"
    env_dir.joinpath(".env").write_text(f"INSTALL_ROOT={install_root}\n", encoding="utf-8")

    from toolkit.core.manifest.catalog import ServiceCatalog
    from toolkit.core.manifest.schema import ServiceManifest

    owned = ServiceManifest.model_validate(
        {
            "name": "owned",
            "label": "Owned",
            "description": "Owned data",
            "icon": "box",
            "category": "cloud",
            "placement": "apps",
            "priority": 50,
            "stateful": True,
            "data_specs": [
                {
                    "name": "nested",
                    "source_env": "INSTALL_ROOT",
                    "source_subpath": "data/nested",
                    "target": "/data",
                    "size_estimate_gb": 1,
                },
                {
                    "name": "shared",
                    "source_env": "INSTALL_ROOT",
                    "source_subpath": "media",
                    "target": "/media",
                    "size_estimate_gb": 0,
                    "snapshot": False,
                    "manage_permissions": False,
                    "shared": True,
                    "host_uid": 1000,
                    "host_gid": 1000,
                    "host_subdirs": ["roms"],
                },
            ],
        }
    )
    monkeypatch.setattr(
        "toolkit.core.manifest.catalog.load_service_catalog",
        lambda _root: ServiceCatalog((owned,)),
    )

    with patch("os.chown") as mock_chown:
        fix_volume_permissions(root, node="apps")

    assert (install_root / "data" / "nested").is_dir()
    assert (install_root / "media" / "roms").is_dir()
    assert any(Path(call.args[0]) == install_root / "data" / "nested" for call in mock_chown.call_args_list)
    assert any(Path(call.args[0]) == install_root / "media" / "roms" for call in mock_chown.call_args_list)


def test_fix_volume_permissions_applies_non_recursive_host_path_mode(tmp_path: Path, monkeypatch):
    root = tmp_path / "homelab"
    root.mkdir()
    env_dir = root / "generated" / "media"
    env_dir.mkdir(parents=True)
    env_dir.joinpath(".env").write_text("TEST_ONLY=1\n", encoding="utf-8")

    from toolkit.core.manifest.catalog import ServiceCatalog
    from toolkit.core.manifest.schema import ServiceManifest

    manifest = ServiceManifest.model_validate(
        {
            "name": "shared-layout",
            "label": "Shared layout",
            "description": "Shared writable directories",
            "icon": "box",
            "category": "media",
            "placement": "media",
            "priority": 1,
            "runtime": "embedded",
            "host_paths": [
                {
                    "path": "media",
                    "uid": 1000,
                    "gid": 1001,
                    "mode": "2775",
                    "subdirs": ["tv", "movies", "downloads"],
                    "create": True,
                    "recursive": False,
                }
            ],
        }
    )
    monkeypatch.setattr(
        "toolkit.core.manifest.catalog.load_service_catalog",
        lambda _root: ServiceCatalog((manifest,)),
    )

    child = root / "media" / "movies" / "existing.mkv"
    child.parent.mkdir(parents=True)
    child.write_text("video", encoding="utf-8")
    with patch("os.chown") as mock_chown:
        fix_volume_permissions(root, node="media")

    assert (root / "media").stat().st_mode & 0o7777 == 0o2775
    assert (root / "media" / "tv").stat().st_mode & 0o7777 == 0o2775
    assert all(Path(call.args[0]) != child for call in mock_chown.call_args_list)
