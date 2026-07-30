from __future__ import annotations

import os
import stat
from datetime import timedelta
from pathlib import Path

import pytest
from toolkit.core.secrets import secrets

ROOT = Path(__file__).resolve().parents[4]


def test_encrypted_secret_save_fails_closed_without_sops(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("HOMELAB_TEST_PLAINTEXT_SECRETS", raising=False)
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(secrets, "secrets_encryption_available", lambda: False)

    with pytest.raises(secrets.SecretStoreUnavailableError):
        secrets.save_secrets_plaintext({"TOKEN": "x"}, tmp_path / "secrets.enc.yaml")


def test_scoped_guest_cannot_create_controller_secret_store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("HOMELAB_NODE", "media")

    logs = secrets.merge_secret_values(tmp_path, {"SEERR_API_KEY": "runtime-only"})

    assert not (tmp_path / "secrets.enc.yaml").exists()
    assert logs == [
        "Secrets: runtime values discovered on scoped media guest (SEERR_API_KEY); controller store unchanged"
    ]


def test_sensitive_writer_has_mode_0600(tmp_path: Path):
    from toolkit.core.deploy.destructive_guard import write_sensitive_file

    path = tmp_path / "generated" / "headscale" / "config.yaml"
    write_sensitive_file(path, "db_password: x\n")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert os.access(path, os.R_OK)


def test_wipe_requires_verified_checkpoint(tmp_path: Path):
    from toolkit.core.deploy.destructive_guard import RecoveryCheckpointRequiredError, require_verified_checkpoint

    with pytest.raises(RecoveryCheckpointRequiredError):
        require_verified_checkpoint(tmp_path, ["infra", "apps", "media"], timedelta(days=7))


def test_webui_secret_router_has_no_plaintext_secret_or_sops_boundary() -> None:
    source = (ROOT / "toolkit" / "webui" / "routers" / "secrets.py").read_text()

    for forbidden in (
        "load_secrets_plaintext",
        "save_secrets_plaintext",
        "generate_all_secrets",
        "ensure_sops_ready",
        "secrets_path",
    ):
        assert forbidden not in source
    assert "toolkit.core.secrets" not in source


def test_secret_template_never_renders_masked_secret_fragments() -> None:
    source = (ROOT / "toolkit" / "webui" / "templates" / "secrets.html").read_text()

    assert "masked" not in source
    assert "secrets.get" not in source
