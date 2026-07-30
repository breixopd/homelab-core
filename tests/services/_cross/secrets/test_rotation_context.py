from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from toolkit.core.ansible.secret_vars import deployment_secret_variables
from toolkit.core.secrets.rotation_context import load_previous_secret_values, previous_secret_context


def test_previous_secret_context_is_private_ephemeral_and_restores_environment(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HOMELAB_ROTATION_PREVIOUS_FILE", "/existing/context")

    with previous_secret_context(tmp_path, {"SERVICE_PASSWORD": "previous-value", "EMPTY": None}):
        path = Path(os.environ["HOMELAB_ROTATION_PREVIOUS_FILE"])
        assert path.is_file()
        assert path.stat().st_mode & 0o777 == 0o600
        assert load_previous_secret_values(tmp_path) == {"SERVICE_PASSWORD": "previous-value"}

    assert not path.exists()
    assert os.environ["HOMELAB_ROTATION_PREVIOUS_FILE"] == "/existing/context"


def test_previous_secret_context_rejects_external_files(tmp_path: Path, monkeypatch) -> None:
    external = tmp_path / "external.yaml"
    external.write_text(yaml.safe_dump({"SERVICE_PASSWORD": "previous-value"}), encoding="utf-8")
    external.chmod(0o600)
    monkeypatch.setenv("HOMELAB_ROTATION_PREVIOUS_FILE", str(external))

    with pytest.raises(ValueError, match="outside the protected state directory"):
        load_previous_secret_values(tmp_path)


def test_ansible_secret_projection_includes_previous_value_only_inside_context(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "config.yaml").touch()
    (tmp_path / "secrets.enc.yaml").touch()

    class Plugin:
        service = "example"

        @staticmethod
        def is_enabled(_cfg) -> bool:
            return True

        @staticmethod
        def ansible_secret_variables(_cfg, secrets: dict[str, str]) -> dict[str, str]:
            return {"service_password": secrets.get("SERVICE_PASSWORD", "")}

    monkeypatch.setattr("toolkit.core.config.config.load_config", lambda _path: SimpleNamespace())
    monkeypatch.setattr(
        "toolkit.core.secrets.secrets.load_secrets_plaintext",
        lambda _path: {"SERVICE_PASSWORD": "current-value"},
    )
    monkeypatch.setattr("toolkit.services.discover_service_plugins", lambda: (Plugin(),))

    assert deployment_secret_variables(tmp_path) == {"service_password": "current-value"}
    with previous_secret_context(tmp_path, {"SERVICE_PASSWORD": "previous-value"}):
        assert deployment_secret_variables(tmp_path) == {
            "service_password": "current-value",
            "service_password_previous": "previous-value",
        }
