from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest
import yaml
from toolkit.controller import bootstrap_api
from toolkit.controller.bootstrap_api import (
    BootstrapInitializationError,
    bootstrap_phase,
    initialize_bootstrap,
    read_bootstrap_status,
    read_bootstrap_view,
)
from toolkit.controller.read_models import BootstrapDesiredState, BootstrapInitializeRequest
from toolkit.controller.store import BootstrapCapabilityError, ControllerStore


def _desired_state() -> BootstrapDesiredState:
    return BootstrapDesiredState(
        domain="home.example.com",
        email="operator@example.com",
        timezone="Europe/Madrid",
        proxmox_api_url="https://192.0.2.10:8006",
        proxmox_node="pve",
        proxmox_storage="local-zfs",
        service_settings={
            "media-library": {"server": "jellyfin"},
            "gluetun": {"enabled": True},
            "tdarr": {"enabled": True},
            "media-cache": {"enabled": True},
            "music-sync": {"enabled": True},
        },
    )


def _credentials() -> dict[str, str]:
    return {
        "CLOUDFLARE_API_TOKEN": "cloudflare-token-value-0123456789",
        "CLOUDFLARE_ZONE_ID": "0123456789abcdef0123456789abcdef",
        "PROXMOX_API_TOKEN_ID": "root@pam!homelab",
        "PROXMOX_API_TOKEN_SECRET": "proxmox-token-value-0123456789",
        "SSO_USER_PASSWORD": "correct horse battery staple",
        "NORDVPN_TOKEN": "nordvpn-token",
        "SPOTIFY_CLIENT_ID": "spotify-client-id",
        "SPOTIFY_CLIENT_SECRET": "spotify-client-secret",
    }


def test_management_mode_does_not_require_proxmox_configuration_or_credentials() -> None:
    desired = _desired_state().model_copy(
        update={
            "deployment_mode": "management",
            "proxmox_api_url": "",
            "proxmox_node": "",
            "proxmox_storage": "",
        }
    )
    request = BootstrapInitializeRequest(
        session_token="00000000-0000-4000-8000-000000000000.session-secret-value",
        desired_state=desired,
        credential_values={name: value for name, value in _credentials().items() if not name.startswith("PROXMOX_")},
    )

    config = bootstrap_api._validate_desired_state(request)
    credentials = bootstrap_api._validated_credentials(request, config)

    assert config.proxmox.provision_machines is False
    assert "PROXMOX_API_TOKEN_ID" not in credentials
    assert "PROXMOX_API_TOKEN_SECRET" not in credentials


def test_management_mode_rejects_proxmox_credentials() -> None:
    desired = _desired_state().model_copy(update={"deployment_mode": "management"})
    request = BootstrapInitializeRequest(
        session_token="00000000-0000-4000-8000-000000000000.session-secret-value",
        desired_state=desired,
        credential_values=_credentials(),
    )
    config = bootstrap_api._validate_desired_state(request)

    with pytest.raises(BootstrapInitializationError, match="not accepted"):
        bootstrap_api._validated_credentials(request, config)


def _grant(store: ControllerStore) -> str:
    capability = store.issue_bootstrap_capability(
        principal="local:operator",
        ttl=timedelta(minutes=10),
    )
    return store.exchange_bootstrap_capability(
        capability.token,
        ttl=timedelta(minutes=5),
    ).session_token


def _fake_sops(monkeypatch: pytest.MonkeyPatch) -> None:
    def ensure_ready(root: Path) -> str:
        (root / "keys").mkdir(parents=True, exist_ok=True)
        (root / "keys" / "age.key").write_text("# public key: age1test\nAGE-SECRET-KEY-TEST\n")
        (root / ".sops.yaml").write_text("creation_rules:\n  - age: age1test\n")
        return "age1test"

    def encrypt(path: Path, *, root: Path | None = None, age_recipient: str = "") -> bool:
        assert root is not None
        assert age_recipient == "age1test"
        path.write_text("payload: ENC[AES256_GCM,data:test]\nsops:\n  age: []\n")
        return True

    def decrypt(path: Path) -> dict[str, str]:
        raw = yaml.safe_load(path.read_text()) or {}
        if "sops" not in raw:
            raise RuntimeError("not encrypted")
        return {"CLOUDFLARE_API_TOKEN": "present"}

    monkeypatch.setattr("toolkit.controller.bootstrap_api.ensure_sops_ready", ensure_ready)
    monkeypatch.setattr("toolkit.controller.bootstrap_api.sops_encrypt", encrypt)
    monkeypatch.setattr("toolkit.controller.bootstrap_api.load_secrets_plaintext", decrypt)


def test_bootstrap_phase_distinguishes_clean_partial_and_tampered_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_sops(monkeypatch)
    store = ControllerStore(tmp_path / ".homelab-state" / "controller.db")

    assert bootstrap_phase(tmp_path) == "uninitialized"
    (tmp_path / "keys").mkdir()
    (tmp_path / "keys" / "age.key").write_text("prepared key")
    assert bootstrap_phase(tmp_path) == "uninitialized"

    (tmp_path / "config.yaml").write_text("domain: partial.example.com\n")
    assert bootstrap_phase(tmp_path) == "recovery_required"
    (tmp_path / "config.yaml").unlink()

    session_token = _grant(store)
    result = initialize_bootstrap(
        tmp_path,
        store,
        BootstrapInitializeRequest(
            session_token=session_token,
            desired_state=_desired_state(),
            credential_values=_credentials(),
        ),
        principal="mtls:homelab-ui",
    )
    assert result.phase == "ready"
    assert len(result.config_revision) == 64
    assert read_bootstrap_status(tmp_path, store).phase == "ready"
    assert bootstrap_phase(tmp_path) == "ready"

    (tmp_path / "config.yaml").write_text("domain: tampered.example.com\n")
    assert bootstrap_phase(tmp_path) == "recovery_required"


def test_initialize_bootstrap_commits_matched_generated_ssh_identity_and_consumes_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_sops(monkeypatch)
    store = ControllerStore(tmp_path / ".homelab-state" / "controller.db")
    session_token = _grant(store)

    view = read_bootstrap_view(tmp_path, store, session_token)
    assert view.status.phase == "uninitialized"
    assert any(category.name == "management" for category in view.categories)
    assert {(setting.service, setting.key) for setting in view.service_settings} == {
        ("gluetun", "enabled"),
        ("gluetun", "provider"),
        ("media-cache", "enabled"),
        ("media-library", "server"),
        ("music-sync", "enabled"),
        ("tdarr", "enabled"),
    }
    assert {(secret.service, secret.name) for secret in view.service_secrets} == {
        ("gluetun", "NORDVPN_TOKEN"),
        ("gluetun", "VPN_PASSWORD"),
        ("gluetun", "VPN_USER"),
        ("music-sync", "SPOTIFY_CLIENT_ID"),
        ("music-sync", "SPOTIFY_CLIENT_SECRET"),
        ("plex", "PLEX_CLAIM"),
    }

    result = initialize_bootstrap(
        tmp_path,
        store,
        BootstrapInitializeRequest(
            session_token=session_token,
            desired_state=_desired_state(),
            credential_values=_credentials(),
        ),
        principal="mtls:homelab-ui",
    )

    local_config = yaml.safe_load((tmp_path / "config.local.yaml").read_text())
    public_config = yaml.safe_load((tmp_path / "config.yaml").read_text())
    assert public_config["service_settings"] == _desired_state().service_settings
    public_key = (tmp_path / "ssh" / "homelab_admin_ed25519.pub").read_text().strip()
    assert local_config["proxmox"]["ssh_public_key"] == public_key
    assert (tmp_path / "ssh" / "homelab_admin_ed25519").stat().st_mode & 0o077 == 0
    assert set(_credentials()).issubset(result.configured_secret_names)
    assert "cloudflare-token" not in repr(result)
    with pytest.raises(BootstrapCapabilityError, match="invalid"):
        store.validate_bootstrap_grant(session_token)


@pytest.mark.parametrize(
    ("credentials", "message"),
    [
        ({}, "Required"),
        ({**_credentials(), "UNRECOGNIZED_SECRET": "value"}, "unsupported"),
        ({name: value for name, value in _credentials().items() if name != "NORDVPN_TOKEN"}, "NORDVPN_TOKEN"),
        (
            {name: value for name, value in _credentials().items() if name != "SPOTIFY_CLIENT_SECRET"},
            "SPOTIFY_CLIENT_SECRET",
        ),
    ],
)
def test_initialize_bootstrap_rejects_invalid_credentials_without_consuming_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    credentials: dict[str, str],
    message: str,
) -> None:
    _fake_sops(monkeypatch)
    store = ControllerStore(tmp_path / ".homelab-state" / "controller.db")
    session_token = _grant(store)

    with pytest.raises(BootstrapInitializationError, match=message):
        initialize_bootstrap(
            tmp_path,
            store,
            BootstrapInitializeRequest(
                session_token=session_token,
                desired_state=_desired_state(),
                credential_values=credentials,
            ),
            principal="mtls:homelab-ui",
        )

    assert store.validate_bootstrap_grant(session_token).session_token == session_token
    assert bootstrap_phase(tmp_path) == "uninitialized"


def test_initialize_bootstrap_rejects_service_settings_not_exposed_for_setup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_sops(monkeypatch)
    store = ControllerStore(tmp_path / ".homelab-state" / "controller.db")
    session_token = _grant(store)
    desired = _desired_state().model_copy(
        update={"service_settings": {"tdarr": {"schedule": "0 5 * * *"}}},
    )

    with pytest.raises(BootstrapInitializationError, match="not available during setup"):
        initialize_bootstrap(
            tmp_path,
            store,
            BootstrapInitializeRequest(
                session_token=session_token,
                desired_state=desired,
                credential_values=_credentials(),
            ),
            principal="mtls:homelab-ui",
        )

    assert bootstrap_phase(tmp_path) == "uninitialized"


def test_initialize_bootstrap_rejects_credentials_for_disabled_service(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_sops(monkeypatch)
    store = ControllerStore(tmp_path / ".homelab-state" / "controller.db")
    session_token = _grant(store)
    desired = _desired_state().model_copy(
        update={"service_settings": {**_desired_state().service_settings, "gluetun": {"enabled": False}}},
    )

    with pytest.raises(BootstrapInitializationError, match="unsupported"):
        initialize_bootstrap(
            tmp_path,
            store,
            BootstrapInitializeRequest(
                session_token=session_token,
                desired_state=desired,
                credential_values=_credentials(),
            ),
            principal="mtls:homelab-ui",
        )


def test_initialize_bootstrap_requires_credentials_for_selected_provider(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_sops(monkeypatch)
    store = ControllerStore(tmp_path / ".homelab-state" / "controller.db")
    session_token = _grant(store)
    desired = _desired_state().model_copy(
        update={
            "service_settings": {
                **_desired_state().service_settings,
                "gluetun": {"enabled": True, "provider": "protonvpn"},
            }
        },
    )
    credentials = {name: value for name, value in _credentials().items() if name != "NORDVPN_TOKEN"}

    with pytest.raises(BootstrapInitializationError, match="VPN_PASSWORD.*VPN_USER"):
        initialize_bootstrap(
            tmp_path,
            store,
            BootstrapInitializeRequest(
                session_token=session_token,
                desired_state=desired,
                credential_values=credentials,
            ),
            principal="mtls:homelab-ui",
        )


def test_failed_commit_enters_recovery_and_does_not_consume_grant(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_sops(monkeypatch)
    store = ControllerStore(tmp_path / ".homelab-state" / "controller.db")
    session_token = _grant(store)
    real_install = bootstrap_api._install_file_exclusive

    def fail_after_first(source: Path, target: Path) -> None:
        real_install(source, target)
        if target.name == "config.yaml":
            raise OSError("simulated commit interruption")

    monkeypatch.setattr("toolkit.controller.bootstrap_api._install_file_exclusive", fail_after_first)

    with pytest.raises(BootstrapInitializationError, match="recover"):
        initialize_bootstrap(
            tmp_path,
            store,
            BootstrapInitializeRequest(
                session_token=session_token,
                desired_state=_desired_state(),
                credential_values=_credentials(),
            ),
            principal="mtls:homelab-ui",
        )

    assert bootstrap_phase(tmp_path) == "recovery_required"
    assert store.validate_bootstrap_grant(session_token).session_token == session_token


def test_bootstrap_rejects_reserved_symlink_parents_without_touching_target(tmp_path: Path) -> None:
    external = tmp_path.parent / f"{tmp_path.name}-external"
    external.mkdir()
    external_key = external / "age.key"
    external_key.write_text("must remain unchanged")
    (tmp_path / "keys").symlink_to(external, target_is_directory=True)
    store = ControllerStore(tmp_path / ".homelab-state" / "controller.db")
    session_token = _grant(store)

    assert bootstrap_phase(tmp_path) == "recovery_required"
    with pytest.raises(BootstrapInitializationError, match="current state"):
        initialize_bootstrap(
            tmp_path,
            store,
            BootstrapInitializeRequest(
                session_token=session_token,
                desired_state=_desired_state(),
                credential_values=_credentials(),
            ),
            principal="mtls:homelab-ui",
        )
    assert external_key.read_text() == "must remain unchanged"
