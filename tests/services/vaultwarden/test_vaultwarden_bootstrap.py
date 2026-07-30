from __future__ import annotations

import shutil
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from tests.helpers.machines import single_control_machines
from toolkit.core.config.config import Config
from toolkit.core.config.credential_catalog import credential_entries
from toolkit.core.secrets.bitwarden_crypto import (
    DEFAULT_KDF_ITERATIONS,
    DEFAULT_KDF_MEMORY,
    KDF_ARGON2ID,
    build_register_keys,
    encrypt_cipher_string,
)
from toolkit.services.vaultwarden import bootstrap as vw


@pytest.fixture
def cfg() -> Config:
    c = Config(
        domain="example.com",
        email="owner@example.com",
        machines=single_control_machines(),
    )
    # Single-VM layout so sync runs locally (httpx mocks) instead of guest SSH.
    c.services.media = False
    c.services.cloud = False
    c.services.email = False
    return c


@pytest.fixture
def secrets() -> dict[str, str]:
    return {
        "VAULTWARDEN_MASTER_PASSWORD": "vault-master-secret",
        "SSO_USER_PASSWORD": "sso-password",
        "GRAFANA_ADMIN_PASSWORD": "grafana-pass",
    }


def test_catalog_has_no_vaultwarden_self_entry(cfg: Config):
    names = {entry.name for entry in credential_entries(cfg)}
    assert "Vaultwarden" not in names
    assert "Code Server" not in names
    assert {"Authelia / LLDAP", "Grafana"}.issubset(names)


def test_catalog_excludes_private_ssh_keys(cfg: Config):
    for entry in credential_entries(cfg):
        assert "PRIVATE_KEY" not in entry.secret_key
        assert "PROXMOX_HOST_SSH" not in entry.secret_key
    names = {e.name for e in credential_entries(cfg)}
    assert "Homelab SSH Access" in names
    assert "Homelab SSH Public Key" in names


def test_catalog_excludes_gitea_local_password_credentials():
    config = Config(domain="example.com", email="owner@example.com")

    assert "Gitea" not in {entry.name for entry in credential_entries(config)}


def test_existing_account_password_mismatch_preserves_vault(cfg: Config, secrets: dict[str, str]) -> None:
    healthy = MagicMock(status_code=200)
    invited = MagicMock(status_code=200)
    existing = MagicMock(status_code=400, text="Account already exists")

    with (
        patch.object(vw.httpx, "get", return_value=healthy),
        patch.object(vw.httpx, "post", side_effect=[invited, existing]) as post,
        patch.object(vw, "vaultwarden_fetch_kdf", return_value=object()),
        patch.object(vw, "vaultwarden_login_access_token", return_value="") as login,
        patch.object(vw, "vaultwarden_admin_session", return_value=MagicMock()),
    ):
        logs = vw.ensure_vaultwarden_account(cfg, secrets, base_url="http://vaultwarden")

    assert login.call_count == 2
    assert "Vaultwarden: account already exists" in logs
    assert any("vault preserved" in line for line in logs)
    assert not any("/delete" in call.args[0] for call in post.call_args_list)


def test_password_login_identifies_as_current_web_vault_client() -> None:
    from toolkit.core.secrets.bitwarden_crypto import KdfParams
    from toolkit.services.sdk.vaultwarden import BITWARDEN_CLIENT_VERSION, vaultwarden_login_access_token

    response = MagicMock(status_code=400)
    with patch("toolkit.services.sdk.vaultwarden.httpx.post", return_value=response) as post:
        assert not vaultwarden_login_access_token(
            "http://vaultwarden",
            "owner@example.com",
            "master-password",
            kdf=KdfParams(KDF_ARGON2ID, 3, 65536, 4),
        )

    assert post.call_args.kwargs["headers"] == {"Bitwarden-Client-Version": BITWARDEN_CLIENT_VERSION}


@pytest.mark.skipif(not shutil.which("openssl"), reason="openssl required")
def test_sync_catalog_encrypted_personal_ciphers(cfg: Config, secrets: dict[str, str], tmp_path: Path):
    keys = build_register_keys(
        secrets["VAULTWARDEN_MASTER_PASSWORD"],
        cfg.email,
    )
    enc_key, mac_key = keys.enc_key, keys.mac_key
    existing_name = encrypt_cipher_string("Grafana", enc_key, mac_key)
    sync_payload = {
        "profile": {"key": keys.protected_symmetric_key},
        "ciphers": [{"id": "existing-grafana-id", "type": 1, "name": existing_name}],
    }

    put_urls: list[str] = []
    post_urls: list[str] = []

    def fake_get(url: str, *args, **kwargs):
        resp = MagicMock(status_code=200)
        if "/api/sync" in url:
            resp.json.return_value = sync_payload
            resp.raise_for_status = MagicMock()
        return resp

    def fake_post(url: str, *args, **kwargs):
        resp = MagicMock(status_code=200)
        if url.endswith("/accounts/prelogin"):
            resp.json.return_value = {
                "kdf": KDF_ARGON2ID,
                "kdfIterations": DEFAULT_KDF_ITERATIONS,
                "kdfMemory": DEFAULT_KDF_MEMORY // 1024,
                "kdfParallelism": 4,
            }
        elif url.endswith("/connect/token"):
            resp.json.return_value = {"access_token": "test-token"}
        elif url.endswith("/api/ciphers"):
            post_urls.append(url)
            resp.status_code = 201
        return resp

    def fake_put(url: str, *args, **kwargs):
        put_urls.append(url)
        return MagicMock(status_code=200)

    with (
        patch("toolkit.services.vaultwarden.bootstrap.vaultwarden_url", return_value="http://vaultwarden"),
        patch.object(vw.httpx, "get", side_effect=fake_get),
        patch.object(vw.httpx, "post", side_effect=fake_post),
        patch.object(vw.httpx, "put", side_effect=fake_put),
        patch.object(
            vw,
            "ensure_vaultwarden_account",
            return_value=["Vaultwarden: vault account ready (password login)"],
        ),
    ):
        logs = vw.sync_catalog_to_vaultwarden(tmp_path, cfg, secrets)

    assert any("encrypted, personal" in line for line in logs)
    assert put_urls == ["http://vaultwarden/api/ciphers/existing-grafana-id"]
    assert post_urls.count("http://vaultwarden/api/ciphers") >= 1
    assert all("/api/ciphers/admin" not in url for url in post_urls)


def test_sync_skips_without_master_password(cfg: Config, tmp_path: Path):
    logs = vw.sync_catalog_to_vaultwarden(tmp_path, cfg, {})
    assert logs == ["Vaultwarden: no VAULTWARDEN_MASTER_PASSWORD — skip sync"]


def test_multinode_sync_without_master_password_does_not_open_tunnel(tmp_path: Path) -> None:
    cfg = Config(domain="example.com", email="owner@example.com")

    with patch.object(vw, "_sync_catalog_via_tunnel") as tunnel:
        logs = vw.sync_catalog_to_vaultwarden(tmp_path, cfg, {})

    assert logs == ["Vaultwarden: no VAULTWARDEN_MASTER_PASSWORD — skip sync"]
    tunnel.assert_not_called()


def test_sync_runs_locally_when_hook_is_already_on_apps_guest(tmp_path: Path, monkeypatch) -> None:
    cfg = Config(domain="example.com", email="owner@example.com")
    monkeypatch.setenv("HOMELAB_NODE", "apps")
    monkeypatch.delenv("HOMELAB_CONTROLLER_ROLE", raising=False)

    with (
        patch.object(vw, "_sync_catalog_local", return_value=["local sync"]) as local,
        patch.object(vw, "_sync_catalog_via_tunnel", create=True) as remote,
    ):
        logs = vw.sync_catalog_to_vaultwarden(tmp_path, cfg, {"VAULTWARDEN_MASTER_PASSWORD": "secret"})

    assert logs == ["local sync"]
    local.assert_called_once()
    remote.assert_not_called()


def test_sync_fails_closed_when_hook_runs_on_another_guest(tmp_path: Path, monkeypatch) -> None:
    cfg = Config(domain="example.com", email="owner@example.com")
    monkeypatch.setenv("HOMELAB_NODE", "infra")
    monkeypatch.delenv("HOMELAB_CONTROLLER_ROLE", raising=False)

    with pytest.raises(RuntimeError, match="Vaultwarden service host"):
        vw.sync_catalog_to_vaultwarden(tmp_path, cfg, {"VAULTWARDEN_MASTER_PASSWORD": "secret"})


def test_controller_role_uses_tunnel_even_when_placed_on_infra(tmp_path: Path, monkeypatch) -> None:
    cfg = Config(domain="example.com", email="owner@example.com")
    monkeypatch.setenv("HOMELAB_NODE", "infra")
    monkeypatch.setenv("HOMELAB_CONTROLLER_ROLE", "local")

    with (
        patch.object(vw, "_sync_catalog_via_tunnel", return_value=["remote sync"]) as remote,
        patch.object(vw, "_sync_catalog_local") as local,
    ):
        logs = vw.sync_catalog_to_vaultwarden(
            tmp_path,
            cfg,
            {"VAULTWARDEN_MASTER_PASSWORD": "secret"},
        )

    assert logs == ["remote sync"]
    remote.assert_called_once_with(
        tmp_path,
        cfg,
        {"VAULTWARDEN_MASTER_PASSWORD": "secret"},
    )
    local.assert_not_called()


def test_controller_syncs_full_catalog_through_apps_tunnel(tmp_path: Path) -> None:
    cfg = Config(domain="example.com", email="owner@example.com")
    secrets = {
        "VAULTWARDEN_MASTER_PASSWORD": "secret",
        "PROXMOX_API_TOKEN_VALUE": "controller-only",
    }

    @contextmanager
    def tunnel(*args, **kwargs):
        yield 43123

    with (
        patch("toolkit.core.manifest.placement.service_address", return_value="100.64.0.20"),
        patch("toolkit.core.manifest.placement.service_route_port", return_value=8080),
        patch("toolkit.core.ansible.ansible_ssh.ssh_local_forward", side_effect=tunnel) as forward,
        patch.object(vw, "_sync_catalog_local", return_value=["synced"]) as local,
    ):
        logs = vw._sync_catalog_via_tunnel(tmp_path, cfg, secrets)

    assert logs == ["synced"]
    forward.assert_called_once_with(
        cfg,
        tmp_path,
        "100.64.0.20",
        8080,
        remote_host="100.64.0.20",
    )
    local.assert_called_once_with(
        tmp_path,
        cfg,
        secrets,
        base_url="http://127.0.0.1:43123",
    )


def test_ensure_account_skips_without_master_password(cfg: Config):
    logs = vw.ensure_vaultwarden_account(cfg, {})
    assert logs == ["Vaultwarden: no VAULTWARDEN_MASTER_PASSWORD — skip account bootstrap"]
