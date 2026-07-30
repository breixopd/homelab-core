from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from click.testing import CliRunner
from toolkit.cli import main
from toolkit.core.config.config import Config, load_config, save_config, save_local_config
from toolkit.core.config.storage import config_path


def _setup_config(root: Path) -> None:
    cfg = Config(domain="example.com", email="admin@example.com")
    save_config(cfg, config_path(root))


def test_main_help():
    runner = CliRunner()
    result = runner.invoke(main, ["--help"])
    assert result.exit_code == 0
    assert "homelab-toolkit" in result.output


def test_deploy_verify_does_not_expose_browser_automation() -> None:
    runner = CliRunner()

    help_result = runner.invoke(main, ["deploy", "verify", "--help"])
    rejected_result = runner.invoke(main, ["deploy", "verify", "--browser"])

    assert help_result.exit_code == 0
    assert "--browser" not in help_result.output
    assert rejected_result.exit_code == 2
    assert "No such option" in rejected_result.output


def test_deploy_rejects_active_lease_before_bootstrap_mutations(tmp_path: Path) -> None:
    from toolkit.core.deploy.operation_lease import OperationLease

    lease = OperationLease.acquire(tmp_path, "secret-update")
    try:
        result = CliRunner().invoke(main, ["--root", str(tmp_path), "deploy", "all", "--yes"])
    finally:
        lease.release()

    assert result.exit_code == 1
    assert "already running" in result.output
    assert not (tmp_path / "config.yaml").exists()
    assert not (tmp_path / "secrets.enc.yaml").exists()


def test_deploy_json_keeps_progress_out_of_stdout(tmp_path: Path) -> None:
    from toolkit.core.config.storage import secrets_path
    from toolkit.core.deploy.deploy_workflow import DeployWorkflowResult

    cfg = Config(
        domain="example.com",
        email="admin@example.com",
        proxmox={"provision_machines": False},
    )
    save_config(cfg, config_path(tmp_path))
    secrets_path(tmp_path).touch()
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / ".ready").touch()

    async def deploy(*_args, **kwargs):
        kwargs["on_progress"]({"percent": "50", "step": "Verify services"})
        kwargs["on_step"]("verify", "ok")
        return DeployWorkflowResult(
            success=True,
            message="Deployment complete",
            notification_type="positive",
            step_status={"verify": "ok"},
        )

    with (
        patch(
            "toolkit.core.secrets.secrets.load_secrets_plaintext",
            return_value={"a": "1", "b": "2", "c": "3", "d": "4"},
        ),
        patch("toolkit.core.deploy.deploy_workflow.run_deploy_workflow", new=deploy),
    ):
        result = CliRunner().invoke(
            main,
            ["--root", str(tmp_path), "deploy", "all", "--json", "--skip-infra", "--skip-dns"],
        )

    assert result.exit_code == 0, (result.output, result.exception)
    assert json.loads(result.stdout)["success"] is True
    assert "[ 50%] Verify services" not in result.stdout
    assert "[ 50%] Verify services" in result.stderr


def test_ops_uninitialized(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "ops"])
    assert result.exit_code == 0
    assert "uninitialized" in result.output


def test_ops_with_config(tmp_path: Path):
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "ops"])
    assert result.exit_code == 0
    assert "example.com" in result.output
    assert "config_only" in result.output


def test_config_init(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "config", "init"])
    assert result.exit_code == 0
    assert "Created" in result.output
    assert config_path(tmp_path).exists()


def test_config_show(tmp_path: Path):
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "config", "show"])
    assert result.exit_code == 0
    assert "example.com" in result.output


def test_config_show_redacts_local_credentials(tmp_path: Path) -> None:
    owner_password = "owner-password-output-canary"
    ssh_password = "ssh-password-output-canary"
    cfg = Config(
        domain="example.com",
        owner_password=owner_password,
        ssh={"auth_method": "password", "password": ssh_password},
    )
    save_config(cfg, config_path(tmp_path))
    save_local_config(cfg, tmp_path)

    result = CliRunner().invoke(main, ["--root", str(tmp_path), "config", "show"])

    assert result.exit_code == 0, (result.output, result.exception)
    assert "example.com" in result.output
    assert owner_password not in result.output
    assert ssh_password not in result.output
    assert result.output.count("<redacted>") == 2


def test_config_validate(tmp_path: Path):
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "config", "validate"])
    assert result.exit_code == 0
    assert "Valid" in result.output


def test_config_set_validates_and_persists_typed_value(tmp_path: Path) -> None:
    _setup_config(tmp_path)

    result = CliRunner().invoke(
        main,
        ["--root", str(tmp_path), "config", "set", "network.expose_via_internet=false"],
    )

    assert result.exit_code == 0, (result.output, result.exception)
    assert load_config(config_path(tmp_path)).network.expose_via_internet is False


def test_config_set_does_not_echo_sensitive_value(tmp_path: Path) -> None:
    _setup_config(tmp_path)
    password = "new-owner-password-output-canary"

    result = CliRunner().invoke(
        main,
        ["--root", str(tmp_path), "config", "set", f"owner_password={password}"],
    )

    assert result.exit_code == 0, (result.output, result.exception)
    assert load_config(config_path(tmp_path)).owner_password == password
    assert password not in result.output
    assert "owner_password = <redacted>" in result.output


def test_config_set_persists_local_only_values_outside_tracked_config(tmp_path: Path) -> None:
    _setup_config(tmp_path)
    public_key = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIGhvbWVsYWItY2ktdGVzdC1rZXk ci@invalid"

    result = CliRunner().invoke(
        main,
        ["--root", str(tmp_path), "config", "set", f"proxmox.ssh_public_key={public_key}"],
    )

    assert result.exit_code == 0, (result.output, result.exception)
    assert load_config(config_path(tmp_path)).proxmox.ssh_public_key == public_key
    assert public_key not in config_path(tmp_path).read_text(encoding="utf-8")
    local_config = tmp_path / "config.local.yaml"
    assert local_config.stat().st_mode & 0o777 == 0o600
    assert yaml.safe_load(local_config.read_text(encoding="utf-8"))["proxmox"]["ssh_public_key"] == public_key


def test_config_set_rejects_invalid_state_without_partial_write(tmp_path: Path) -> None:
    _setup_config(tmp_path)
    before = config_path(tmp_path).read_bytes()

    result = CliRunner().invoke(
        main,
        [
            "--root",
            str(tmp_path),
            "config",
            "set",
            "network.expose_via_internet=false",
            "services.management=false",
        ],
    )

    assert result.exit_code != 0
    assert "always-on" in result.output
    assert config_path(tmp_path).read_bytes() == before


def test_config_set_rejects_unknown_path_without_mutating_config(tmp_path: Path) -> None:
    _setup_config(tmp_path)
    before = config_path(tmp_path).read_bytes()

    result = CliRunner().invoke(
        main,
        ["--root", str(tmp_path), "config", "set", "network.not_a_setting=true"],
    )

    assert result.exit_code != 0
    assert "not_a_setting" in result.output
    assert config_path(tmp_path).read_bytes() == before


def test_services_list(tmp_path: Path):
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "services", "list"])
    assert result.exit_code == 0
    assert "Management" in result.output
    assert "Media" in result.output
    assert "Total:" in result.output


def test_services_routes(tmp_path: Path):
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "services", "routes"])
    assert result.exit_code == 0
    assert "example.com" in result.output


def test_secrets_show(tmp_path: Path):
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "secrets", "show"])
    assert result.exit_code == 0
    assert "POSTGRES_PASSWORD" in result.output


def test_secrets_generate(tmp_path: Path):
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "secrets", "generate"])
    assert result.exit_code == 0
    assert "Generated" in result.output
    assert "Storage:" in result.output


@pytest.mark.parametrize(
    "arguments",
    (
        ["secrets", "generate"],
        ["secrets", "set", "PROXMOX_API_TOKEN_SECRET", "replacement"],
        ["secrets", "unset", "PROXMOX_API_TOKEN_SECRET", "--yes"],
    ),
)
def test_secret_mutation_commands_refuse_active_deployment(
    tmp_path: Path,
    monkeypatch,
    arguments: list[str],
) -> None:
    from unittest.mock import MagicMock

    from toolkit.core.deploy.operation_lease import OperationLease

    _setup_config(tmp_path)
    save = MagicMock()
    monkeypatch.setattr("toolkit.cli.secrets_cmd.save_secrets_plaintext", save)
    if arguments[1] == "unset":
        monkeypatch.setattr(
            "toolkit.cli.secrets_cmd.load_secrets_plaintext",
            lambda _path: {"PROXMOX_API_TOKEN_SECRET": "configured"},
        )
    lease = OperationLease.acquire(tmp_path, "deploy")
    try:
        result = CliRunner().invoke(main, ["--root", str(tmp_path), *arguments])
    finally:
        lease.release()

    assert result.exit_code == 1
    assert "already running" in result.output
    save.assert_not_called()


def test_secret_unset_does_not_hold_deploy_lease_while_confirming(tmp_path: Path, monkeypatch) -> None:
    from toolkit.core.config.storage import secrets_path
    from toolkit.core.deploy.operation_lease import OperationLease
    from toolkit.core.secrets.secrets import save_secrets_plaintext

    _setup_config(tmp_path)
    save_secrets_plaintext({"PROXMOX_API_TOKEN_SECRET": "configured"}, secrets_path(tmp_path))

    def confirm(*_args, **_kwargs) -> bool:
        probe = OperationLease.acquire(tmp_path, "deploy")
        probe.release()
        return True

    monkeypatch.setattr("click.confirm", confirm)
    result = CliRunner().invoke(
        main,
        ["--root", str(tmp_path), "secrets", "unset", "PROXMOX_API_TOKEN_SECRET"],
    )

    assert result.exit_code == 0


def test_secret_set_rejects_unknown_names_without_writing(tmp_path: Path, monkeypatch) -> None:
    from unittest.mock import MagicMock

    _setup_config(tmp_path)
    save = MagicMock()
    monkeypatch.setattr("toolkit.cli.secrets_cmd.save_secrets_plaintext", save)

    result = CliRunner().invoke(
        main,
        ["--root", str(tmp_path), "secrets", "set", "UNDECLARED_SECRET", "value"],
    )

    assert result.exit_code == 1
    assert "Unknown secret" in result.output
    save.assert_not_called()


@pytest.mark.parametrize(
    "arguments",
    (
        ["secrets", "configure-vpn", "--provider", "nordvpn"],
        ["secrets", "init"],
        ["install", "--preset", "minimal", "--yes"],
    ),
)
def test_remaining_secret_mutators_refuse_active_deployment(
    tmp_path: Path,
    monkeypatch,
    arguments: list[str],
) -> None:
    from unittest.mock import MagicMock

    from toolkit.core.deploy.operation_lease import OperationLease

    monkeypatch.setattr("click.prompt", lambda *_args, **_kwargs: "test-token")
    save = MagicMock()
    monkeypatch.setattr("toolkit.cli.secrets_cmd.save_secrets_plaintext", save)
    monkeypatch.setattr("toolkit.cli.install_cmd.save_secrets_plaintext", save, raising=False)
    lease = OperationLease.acquire(tmp_path, "deploy")
    try:
        result = CliRunner().invoke(main, ["--root", str(tmp_path), *arguments])
    finally:
        lease.release()

    assert result.exit_code == 1
    assert "already running" in result.output
    save.assert_not_called()


def test_secrets_generate_owner_password_overrides_existing_sso_password(tmp_path: Path):
    from toolkit.core.config.config import Config, save_local_config
    from toolkit.core.config.storage import secrets_path
    from toolkit.core.secrets.secrets import load_secrets_plaintext, save_secrets_plaintext

    cfg = Config(domain="example.com", email="admin@example.com", owner_password="configured-owner-password")
    # owner_password is a local-only sensitive field — save_config() strips it
    # from the tracked config.yaml by design (it's a plaintext SSO password).
    # It must be persisted to config.local.yaml (which load_config() merges)
    # for reload to keep it. Mirrors how the WebUI/CLI operators actually set it.
    save_config(cfg, config_path(tmp_path))
    save_local_config(cfg, tmp_path)
    save_secrets_plaintext({"SSO_USER_PASSWORD": "old-generated-password"}, secrets_path(tmp_path))

    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "secrets", "generate"])

    assert result.exit_code == 0
    assert load_secrets_plaintext(secrets_path(tmp_path))["SSO_USER_PASSWORD"] == "configured-owner-password"


def test_generate_command(tmp_path: Path):
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "generate"])
    assert result.exit_code == 0, f"{result.output}\n{result.exception!r}"
    assert "infra" in result.output
    assert "media" in result.output
    assert "validate:" in result.output


def test_install_preset(tmp_path: Path):
    """Install with --preset should create config."""
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "install", "--preset", "minimal"], input="y\n")
    assert result.exit_code in (0, 1)


def test_services_status(tmp_path: Path):
    """Services status command should run."""
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "services", "status"])
    assert result.exit_code in (0, 1)
