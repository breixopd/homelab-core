from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from toolkit.cli import main
from toolkit.core.config.config import (
    Config,
    ImagesConfig,
    NotificationsConfig,
    ProjectEntry,
    ProjectsConfig,
    ServicesConfig,
    SMTPNotificationConfig,
)
from toolkit.core.manifest.schema import RequiredSecretManifest
from toolkit.core.secrets.secrets import (
    generate_all_secrets,
    generate_password,
    generate_secret,
    generated_password_is_valid,
    get_required_secrets,
    load_secrets_plaintext,
    manifest_secret_spec,
    save_secrets_plaintext,
    secret_storage_mode,
    secrets_file_is_encrypted,
)

PINNED_IMAGE = "ghcr.io/example/project:1@sha256:" + "a" * 64


def _config(
    *categories: str,
    music_sync: bool = True,
    media_server: str = "jellyfin",
    media_cache: bool = False,
    projects: ProjectsConfig | None = None,
) -> Config:
    enabled = set(categories)
    return Config(
        services=ServicesConfig(
            media="media" in enabled,
            cloud="cloud" in enabled,
            notifications="notifications" in enabled,
            email="email" in enabled,
            security="security" in enabled,
        ),
        service_settings={
            "media-library": {"server": media_server},
            "media-cache": {"enabled": media_cache},
            "music-sync": {"enabled": music_sync},
        },
        projects=projects or ProjectsConfig(),
    )


def test_generate_secret_length():
    s = generate_secret(32)
    assert len(s) == 32
    assert s.isalnum()


def test_generate_secret_unique():
    a = generate_secret(32)
    b = generate_secret(32)
    assert a != b


def test_generate_password_has_required_complexity_without_compose_interpolation() -> None:
    value = generate_password(32)

    assert len(value) == 32
    assert any(character.islower() for character in value)
    assert any(character.isupper() for character in value)
    assert any(character.isdigit() for character in value)
    assert any(not character.isalnum() for character in value)
    assert set(value) <= set("abcdefghjkmnpqrstuvwxyzABCDEFGHJKMNPQRSTUVWXYZ23456789.*+?-")
    assert "$" not in value


@pytest.mark.parametrize(
    "value",
    [
        "missing-symbol-A2",
        "missinguppercase.2",
        "MISSINGLOWERCASE.2",
        "MissingDigit.",
        "Invalid$Character2A",
    ],
)
def test_generated_password_validation_rejects_values_outside_contract(value: str) -> None:
    assert not generated_password_is_valid(value)


def test_generate_all_secrets_repairs_invalid_reconcilable_password() -> None:
    spec = RequiredSecretManifest(
        name="SERVICE_PASSWORD",
        tier="generated",
        description="Service password",
        generator="password",
        rotation="reconcile",
    )

    result = generate_all_secrets(
        [manifest_secret_spec(spec)],
        {"SERVICE_PASSWORD": "legacy-alphanumeric-value"},
    )

    assert result["SERVICE_PASSWORD"] != "legacy-alphanumeric-value"
    assert generated_password_is_valid(result["SERVICE_PASSWORD"])


def test_generate_all_secrets_refuses_invalid_persistent_password() -> None:
    spec = RequiredSecretManifest(
        name="SERVICE_PASSWORD",
        tier="generated",
        description="Service password",
        generator="password",
        rotation="persistent",
    )

    with pytest.raises(ValueError, match="persistent generated password SERVICE_PASSWORD"):
        generate_all_secrets(
            [manifest_secret_spec(spec)],
            {"SERVICE_PASSWORD": "legacy-alphanumeric-value"},
        )


def test_get_required_secrets_core():
    specs = get_required_secrets(_config("management"))
    names = [s.name for s in specs]
    assert "POSTGRES_PASSWORD" in names
    assert "REDIS_PASSWORD" in names
    assert "AUTHELIA_JWT_SECRET" in names
    assert "GRAFANA_WEBHOOK_HMAC_SECRET" in names


def test_required_secret_deduplication_rejects_conflicting_generators(monkeypatch) -> None:
    cfg = _config()
    shared = {
        "name": "SHARED_GENERATED_SECRET",
        "tier": "generated",
        "description": "Shared generated secret",
        "rotation": "restart",
    }
    catalog = SimpleNamespace(
        manifests=(
            SimpleNamespace(required_secrets=(RequiredSecretManifest(**shared, generator="token"),)),
            SimpleNamespace(required_secrets=(RequiredSecretManifest(**shared, generator="password"),)),
        )
    )
    monkeypatch.setattr("toolkit.core.manifest.catalog.load_service_catalog", lambda: catalog)
    monkeypatch.setattr("toolkit.core.manifest.routes.service_is_enabled", lambda *_args: True)

    with pytest.raises(ValueError, match="conflicting secret ownership for SHARED_GENERATED_SECRET"):
        get_required_secrets(cfg)


def test_registry_auth_secret_is_declared_from_image_config() -> None:
    cfg = _config("management")
    cfg.images = ImagesConfig(
        registry="ghcr.io/private-owner",
        auth={"username": "automation", "token_secret": "GHCR_READ_TOKEN"},
    )

    specs = {spec.name: spec for spec in get_required_secrets(cfg)}

    assert specs["GHCR_READ_TOKEN"].tier.value == "user"


def test_external_smtp_secret_is_declared_from_notification_config() -> None:
    cfg = _config("management")
    cfg.notifications = NotificationsConfig(
        smtp=SMTPNotificationConfig(
            mode="external",
            host="smtp.example.com",
            username="operator",
            password_secret="SMTP_OPERATOR_PASSWORD",
        )
    )

    specs = {spec.name: spec for spec in get_required_secrets(cfg)}

    assert specs["SMTP_OPERATOR_PASSWORD"].tier.value == "user"


def test_project_database_secrets_are_generated_from_desired_state():
    projects = ProjectsConfig(
        entries=[
            ProjectEntry(
                subdomain=name,
                auth_mode="forward_auth",
                exposure="private",
                docker_image=PINNED_IMAGE,
                placement="apps",
                database_service="dev-postgres",
            )
            for name in ("status-page", "blog")
        ]
    )
    specs = get_required_secrets(_config("management", "cloud", projects=projects))
    names = {spec.name for spec in specs}

    assert "STATUS_PAGE_POSTGRES_PASSWORD" in names
    assert "BLOG_POSTGRES_PASSWORD" in names


def test_get_required_secrets_media():
    specs = get_required_secrets(_config("management", "media", music_sync=True, media_server="both"))
    names = [s.name for s in specs]
    assert "SONARR_API_KEY" in names
    assert "TAUTULLI_API_KEY" in names
    assert "SPOTIFY_CLIENT_ID" in names


def test_get_required_secrets_media_jellyfin_only():
    specs = get_required_secrets(_config("management", "media", music_sync=True, media_server="jellyfin"))
    names = [s.name for s in specs]
    assert "TAUTULLI_API_KEY" not in names
    assert "PLEX_CLAIM" not in names


def test_get_required_secrets_no_music_sync():
    specs = get_required_secrets(_config("management", "media", music_sync=False))
    names = [s.name for s in specs]
    assert "SONARR_API_KEY" in names
    assert "SPOTIFY_CLIENT_ID" not in names


def test_get_required_secrets_music_sync_web_credentials():
    specs = get_required_secrets(_config("management", "media", music_sync=True))
    names = [s.name for s in specs]
    assert "MUSIC_SYNC_WEB_USERNAME" in names
    assert "MUSIC_SYNC_WEB_PASSWORD" in names


def test_get_required_secrets_media_cache_gate():
    with_cache = get_required_secrets(_config("management", "media", media_cache=True))
    without_cache = get_required_secrets(_config("management", "media", media_cache=False))
    assert "MEDIA_CACHE_TOKEN" in [s.name for s in with_cache]
    assert "MEDIA_CACHE_TOKEN" not in [s.name for s in without_cache]


def test_generate_all_secrets_music_sync_web_username_default():
    specs = get_required_secrets(_config("management", "media", music_sync=True))
    result = generate_all_secrets(specs)
    assert result["MUSIC_SYNC_WEB_USERNAME"] == "admin"
    assert len(result["MUSIC_SYNC_WEB_PASSWORD"]) == 24


def test_bootstrapped_service_token_is_not_randomly_generated():
    specs = get_required_secrets(_config("management", "cloud"))
    result = generate_all_secrets(specs)

    assert "GITEA_ADMIN_TOKEN" not in result


def test_age_key_backup_attest_in_core_secrets():
    specs = get_required_secrets(_config("management"))
    names = [s.name for s in specs]
    assert "AGE_KEY_BACKUP_ATTEST" in names


def test_get_required_secrets_cloud_seaweedfs():
    specs = get_required_secrets(_config("management", "cloud"))
    names = [s.name for s in specs]
    assert "SEAWEEDFS_S3_ACCESS_KEY" in names
    assert "SEAWEEDFS_S3_SECRET_KEY" in names
    assert "FMD_REGISTRATION_TOKEN" in names


def test_generate_all_secrets_fills_generated():
    specs = get_required_secrets(_config("management"))
    result = generate_all_secrets(specs)
    assert len(result["POSTGRES_PASSWORD"]) == 32
    assert len(result["AUTHELIA_JWT_SECRET"]) == 64


def test_generate_all_secrets_preserves_existing():
    specs = get_required_secrets(_config("management"))
    existing = {"POSTGRES_PASSWORD": "keep-me"}
    result = generate_all_secrets(specs, existing)
    assert result["POSTGRES_PASSWORD"] == "keep-me"


def test_generate_all_secrets_user_tier_empty():
    specs = get_required_secrets(_config("management", "media", media_server="both"))
    result = generate_all_secrets(specs)
    assert result["PLEX_CLAIM"] == ""  # User must fill this in
    # Jellyfin API key is pre-generated; the media hook seeds it into Jellyfin.
    assert len(result["JELLYFIN_API_KEY"]) == 32


def test_save_load_roundtrip(tmp_path: Path):
    path = tmp_path / "secrets.yaml"
    original = {"POSTGRES_PASSWORD": "test123", "REDIS_PASSWORD": "abc456"}
    save_secrets_plaintext(original, path)
    loaded = load_secrets_plaintext(path)
    assert loaded == original


def test_load_nonexistent():
    result = load_secrets_plaintext(Path("/nonexistent/path.yaml"))
    assert result == {}


def test_load_runtime_secrets_uses_role_scoped_guest_bundle(tmp_path: Path):
    from toolkit.core.secrets.secrets import load_runtime_secrets

    env = tmp_path / "generated" / "infra" / ".hooks.env"
    env.parent.mkdir(parents=True)
    env.write_text('LLDAP_BIND_PASSWORD="bind # value"\nCONTROLLER_ONLY=\n')

    assert load_runtime_secrets(tmp_path, role="infra") == {
        "LLDAP_BIND_PASSWORD": "bind # value",
        "CONTROLLER_ONLY": "",
    }


def test_load_runtime_secrets_prefers_role_bundle_over_controller_store(tmp_path: Path):
    from toolkit.core.secrets.secrets import load_runtime_secrets

    (tmp_path / "secrets.enc.yaml").write_text("LLDAP_BIND_PASSWORD: controller-value\n")
    env = tmp_path / "generated" / "infra" / ".hooks.env"
    env.parent.mkdir(parents=True)
    env.write_text("LLDAP_BIND_PASSWORD=guest-value\n")

    assert load_runtime_secrets(tmp_path, role="infra")["LLDAP_BIND_PASSWORD"] == "guest-value"


def test_load_runtime_secrets_uses_controller_store_without_role_bundle(tmp_path: Path):
    from toolkit.core.secrets.secrets import load_runtime_secrets

    (tmp_path / "secrets.enc.yaml").write_text("LLDAP_BIND_PASSWORD: controller-value\n")

    assert load_runtime_secrets(tmp_path)["LLDAP_BIND_PASSWORD"] == "controller-value"


def test_load_runtime_secrets_fails_closed_when_role_bundle_is_missing(tmp_path: Path):
    from toolkit.core.secrets.secrets import load_runtime_secrets

    (tmp_path / "secrets.enc.yaml").write_text("CONTROLLER_ONLY: must-not-leak\n")

    assert load_runtime_secrets(tmp_path, role="apps") == {}


def test_load_runtime_secrets_uses_controller_rendered_role_bundle(tmp_path: Path):
    from toolkit.core.secrets.secrets import load_runtime_secrets

    bundle = tmp_path / "generated" / "bundles" / "apps" / ".hooks.env"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("APP_TOKEN=scoped\n")

    assert load_runtime_secrets(tmp_path, role="apps") == {"APP_TOKEN": "scoped"}


def test_load_runtime_secrets_ignores_controller_bundle_on_guest(tmp_path: Path, monkeypatch) -> None:
    from toolkit.core.secrets.secrets import load_runtime_secrets

    bundle = tmp_path / "generated" / "bundles" / "apps" / ".hooks.env"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("STALE_TOKEN=must-not-load\n")
    monkeypatch.setenv("HOMELAB_NODE", "apps")
    monkeypatch.delenv("HOMELAB_CONTROLLER_ROLE", raising=False)

    assert load_runtime_secrets(tmp_path, role="apps") == {}


def test_load_runtime_secrets_uses_controller_bundle_when_controller_is_placed_on_guest(
    tmp_path: Path, monkeypatch
) -> None:
    from toolkit.core.secrets.secrets import load_runtime_secrets

    bundle = tmp_path / "generated" / "bundles" / "media" / ".hooks.env"
    bundle.parent.mkdir(parents=True)
    bundle.write_text("MEDIA_TOKEN=scoped\n")
    monkeypatch.setenv("HOMELAB_NODE", "infra")
    monkeypatch.setenv("HOMELAB_CONTROLLER_ROLE", "local")

    assert load_runtime_secrets(tmp_path, role="media") == {"MEDIA_TOKEN": "scoped"}


def test_load_runtime_secrets_uses_role_bundle_when_guest_has_no_age_key(tmp_path: Path):
    from toolkit.core.secrets import secrets as secrets_module
    from toolkit.core.secrets.secrets import load_runtime_secrets

    (tmp_path / "secrets.enc.yaml").write_text("sops:\n  age: []\n")
    env = tmp_path / "generated" / "infra" / ".hooks.env"
    env.parent.mkdir(parents=True)
    env.write_text("LLDAP_BIND_PASSWORD=guest-value\n")

    with patch.object(secrets_module, "_sops_age_key_candidates", return_value=[]):
        assert load_runtime_secrets(tmp_path, role="infra") == {
            "LLDAP_BIND_PASSWORD": "guest-value",
        }


def test_secret_storage_mode_plaintext(tmp_path: Path):
    path = tmp_path / "secrets.enc.yaml"
    path.write_text("POSTGRES_PASSWORD: plain\n")
    assert secret_storage_mode(path) == "plaintext"
    assert secrets_file_is_encrypted(path) is False


def test_secret_types():
    """Secrets must be alphanumeric (urlsafe base64)."""
    for _ in range(50):
        s = generate_secret()
        assert len(s) > 0
        assert " " not in s
        assert "\n" not in s


def test_init_sops_creates_config(tmp_path, monkeypatch):
    """init_sops creates .sops.yaml and age key file."""
    import subprocess

    from toolkit.core.secrets.secrets import init_sops

    def mock_run(*args, **kwargs):
        key_path = tmp_path / "keys" / "age.key"
        key_path.parent.mkdir(parents=True, exist_ok=True)
        key_path.write_text("# public key: age1testkey123\nAGE-SECRET-KEY-1FAKE\n")
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="Public key: age1testkey123\n")

    monkeypatch.setattr(subprocess, "run", mock_run)

    pubkey = init_sops(tmp_path)
    assert pubkey == "age1testkey123"
    assert (tmp_path / ".sops.yaml").exists()
    assert "age1testkey123" in (tmp_path / ".sops.yaml").read_text()


def test_sops_discovery_ignores_inaccessible_system_key_candidates(tmp_path: Path, monkeypatch) -> None:
    from toolkit.core.secrets.secrets import _sops_env, ensure_sops_ready

    class InaccessibleKey:
        def is_file(self) -> bool:
            raise PermissionError("system key directory is private")

    (tmp_path / ".sops.yaml").write_text(
        "creation_rules:\n  - age: age1testkey123\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("SOPS_AGE_KEY_FILE", raising=False)
    monkeypatch.setattr(
        "toolkit.core.secrets.secrets._sops_age_key_candidates",
        lambda _root: [InaccessibleKey()],
    )

    assert ensure_sops_ready(tmp_path) == "age1testkey123"
    assert "SOPS_AGE_KEY_FILE" not in _sops_env(tmp_path)


def test_sops_env_prefers_the_install_root_key_over_an_inherited_key(tmp_path: Path, monkeypatch) -> None:
    from toolkit.core.secrets.secrets import _sops_env

    root_key = tmp_path / "keys" / "age.key"
    root_key.parent.mkdir()
    root_key.write_text("root key", encoding="utf-8")
    inherited_key = tmp_path / "inherited-age.key"
    inherited_key.write_text("inherited key", encoding="utf-8")
    monkeypatch.setenv("SOPS_AGE_KEY_FILE", str(inherited_key))

    assert _sops_env(tmp_path)["SOPS_AGE_KEY_FILE"] == str(root_key)


def test_save_encrypted_path_uses_sops_when_available(tmp_path, monkeypatch):
    calls: list[tuple] = []

    monkeypatch.setattr("toolkit.core.secrets.secrets.secrets_encryption_available", lambda: True)
    monkeypatch.setattr("toolkit.core.secrets.secrets.ensure_sops_ready", lambda root: "age1testkey123")

    def fake_encrypt(path, root=None, age_recipient=""):
        calls.append((path, root))
        path.write_text("data: ENC[AES256_GCM,data:test]\nsops:\n  version: 3.9.0\n")
        return True

    monkeypatch.setattr("toolkit.core.secrets.secrets.sops_encrypt", fake_encrypt)

    path = tmp_path / "secrets.enc.yaml"
    save_secrets_plaintext({"POSTGRES_PASSWORD": "test123"}, path)

    assert len(calls) == 1
    assert calls[0][0].name.startswith("secrets.enc.tmp.")
    assert calls[0][1] == tmp_path
    assert secret_storage_mode(path) == "encrypted"


def test_vpn_secrets_included_with_media():
    from toolkit.core.secrets.secrets import get_required_secrets

    specs = get_required_secrets(_config("management", "media", music_sync=False))
    names = [s.name for s in specs]
    assert "VPN_PROVIDER" in names
    assert "VPN_USER" in names
    assert "VPN_PASSWORD" in names
    assert "WIREGUARD_PRIVATE_KEY" in names
    assert "WIREGUARD_ADDRESSES" in names


def test_vpn_secrets_not_included_without_media():
    from toolkit.core.secrets.secrets import get_required_secrets

    specs = get_required_secrets(_config("management", "cloud", music_sync=False))
    names = [s.name for s in specs]
    assert "VPN_PROVIDER" not in names
    assert "VPN_USER" not in names


def test_secrets_set_and_unset_cli(tmp_path):
    """`secrets set` writes a value; `secrets unset` removes it."""
    runner = CliRunner()
    with (
        patch.dict("os.environ", {"HOMELAB_TEST_PLAINTEXT_SECRETS": "1"}),
        patch("toolkit.core.secrets.secrets.ensure_sops_ready", lambda root: ""),
        patch("toolkit.core.secrets.secrets.secrets_encryption_available", lambda: False),
    ):
        result = runner.invoke(main, ["--root", str(tmp_path), "secrets", "set", "SPOTIFY_CLIENT_ID", "abc123"])
        assert result.exit_code == 0, result.output
        assert load_secrets_plaintext(tmp_path / "secrets.enc.yaml")["SPOTIFY_CLIENT_ID"] == "abc123"

        result = runner.invoke(main, ["--root", str(tmp_path), "secrets", "unset", "SPOTIFY_CLIENT_ID", "--yes"])
        assert result.exit_code == 0, result.output
        assert "SPOTIFY_CLIENT_ID" not in load_secrets_plaintext(tmp_path / "secrets.enc.yaml")


def test_secrets_set_prompts_when_value_omitted(tmp_path):
    runner = CliRunner()
    with (
        patch.dict("os.environ", {"HOMELAB_TEST_PLAINTEXT_SECRETS": "1"}),
        patch("toolkit.core.secrets.secrets.ensure_sops_ready", lambda root: ""),
        patch("toolkit.core.secrets.secrets.secrets_encryption_available", lambda: False),
    ):
        result = runner.invoke(
            main,
            ["--root", str(tmp_path), "secrets", "set", "PROXMOX_API_TOKEN_SECRET"],
            input="hunter2\n",
        )
    assert result.exit_code == 0, result.output
    assert load_secrets_plaintext(tmp_path / "secrets.enc.yaml")["PROXMOX_API_TOKEN_SECRET"] == "hunter2"
