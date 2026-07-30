from __future__ import annotations

import json
from pathlib import Path

import pytest
from toolkit.controller.read_models import SecretUpdateRequest
from toolkit.controller.settings_api import (
    SecretMutationError,
    generate_secret_values,
    read_secret_inventory,
    update_secret_values,
)
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.deploy.operation_lease import OperationLease


def _configured_root(tmp_path: Path) -> Path:
    save_config(Config(domain="example.com", email="owner@example.com"), config_path(tmp_path))
    (tmp_path / "secrets.enc.yaml").write_text("placeholder\n")
    return tmp_path


def test_secret_inventory_contains_presence_only(monkeypatch, tmp_path: Path) -> None:
    root = _configured_root(tmp_path)
    canaries = {
        "PROXMOX_API_TOKEN_ID": "owner@pam!terraform",
        "PROXMOX_API_TOKEN_SECRET": "canary-secret-value-1234",
        "SSO_USER_PASSWORD": "canary-owner-password-5678",
    }
    monkeypatch.setattr("toolkit.controller.settings_api.load_secrets_plaintext", lambda _path: canaries)
    monkeypatch.setattr("toolkit.controller.settings_api.secret_storage_mode", lambda _path: "encrypted")
    monkeypatch.setattr("toolkit.controller.settings_api.secrets_encryption_available", lambda: True)

    inventory = read_secret_inventory(root)
    serialized = json.dumps(inventory.model_dump(mode="json", by_alias=True), sort_keys=True)

    assert inventory.owner_email == "owner@example.com"
    assert set(inventory.model_dump(mode="json", by_alias=True)["entries"][0]) == {
        "name",
        "isConfigured",
        "tier",
        "rotationPolicy",
        "description",
    }
    assert "canary-secret-value" not in serialized
    assert "canary-owner-password" not in serialized


def test_secret_update_allows_only_required_user_values_and_never_returns_them(monkeypatch, tmp_path: Path) -> None:
    root = _configured_root(tmp_path)
    saved: dict[str, str] = {}
    monkeypatch.setattr("toolkit.controller.settings_api.load_secrets_plaintext", lambda _path: {})
    monkeypatch.setattr(
        "toolkit.controller.settings_api.save_secrets_plaintext",
        lambda data, _path: saved.update(data),
    )
    monkeypatch.setattr("toolkit.controller.settings_api.secret_storage_mode", lambda _path: "encrypted")
    monkeypatch.setattr("toolkit.controller.settings_api.secrets_encryption_available", lambda: True)

    result = update_secret_values(
        root,
        SecretUpdateRequest(
            values={
                "PROXMOX_API_TOKEN_ID": "owner@pam!terraform",
                "PROXMOX_API_TOKEN_SECRET": "new-secret-token",
                "SSO_USER_PASSWORD": "new-owner-password",
            }
        ),
    )
    serialized = json.dumps(result.model_dump(mode="json", by_alias=True), sort_keys=True)

    assert saved["PROXMOX_API_TOKEN_SECRET"] == "new-secret-token"
    assert result.changed_names == ["PROXMOX_API_TOKEN_ID", "PROXMOX_API_TOKEN_SECRET", "SSO_USER_PASSWORD"]
    assert "new-secret-token" not in serialized
    assert "new-owner-password" not in serialized


def test_secret_update_refuses_to_race_an_active_deployment(tmp_path: Path) -> None:
    root = _configured_root(tmp_path)
    lease = OperationLease.acquire(root, "deploy")
    try:
        with pytest.raises(SecretMutationError, match="already running"):
            update_secret_values(
                root,
                SecretUpdateRequest(values={"PROXMOX_API_TOKEN_SECRET": "replacement"}),
            )
    finally:
        lease.release()


def test_secret_generate_refuses_to_race_an_active_deployment(tmp_path: Path) -> None:
    root = _configured_root(tmp_path)
    lease = OperationLease.acquire(root, "deploy")
    try:
        with pytest.raises(SecretMutationError, match="already running"):
            generate_secret_values(root)
    finally:
        lease.release()


def test_secret_update_rejects_generated_or_unknown_names_without_writing(monkeypatch, tmp_path: Path) -> None:
    from unittest.mock import MagicMock

    root = _configured_root(tmp_path)
    save = MagicMock()
    monkeypatch.setattr("toolkit.controller.settings_api.load_secrets_plaintext", lambda _path: {})
    monkeypatch.setattr("toolkit.controller.settings_api.save_secrets_plaintext", save)

    with pytest.raises(SecretMutationError, match="not user-configurable"):
        update_secret_values(root, SecretUpdateRequest(values={"LLDAP_ADMIN_PASSWORD": "operator-supplied"}))

    save.assert_not_called()
