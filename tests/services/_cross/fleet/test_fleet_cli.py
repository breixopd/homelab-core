from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
from toolkit.cli import main
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path


def _setup_config(root: Path) -> None:
    cfg = Config(domain="test.local", email="admin@test.local")
    save_config(cfg, config_path(root))


def test_fleet_list_empty(tmp_path):
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "fleet", "list"])
    assert result.exit_code == 0
    assert "No managed hosts" in result.output


def test_fleet_add_plain_host_and_list(tmp_path):
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--root",
            str(tmp_path),
            "fleet",
            "add",
            "nas",
            "10.0.0.5",
            "--plain",
            "--service",
            "media-cache",
            "--integration-setting",
            "media-cache.path=/mnt/pool/media",
            "--skip-onboard",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Added host" in result.output

    result = runner.invoke(main, ["--root", str(tmp_path), "fleet", "list"])
    assert result.exit_code == 0
    assert "nas" in result.output
    assert "10.0.0.5" in result.output


def test_fleet_remove_plain_host(tmp_path):
    _setup_config(tmp_path)
    runner = CliRunner()
    runner.invoke(
        main,
        [
            "--root",
            str(tmp_path),
            "fleet",
            "add",
            "nas",
            "10.0.0.5",
            "--plain",
            "--service",
            "media-cache",
            "--integration-setting",
            "media-cache.path=/mnt/pool/media",
            "--skip-onboard",
        ],
    )
    # Mock cleanup_host_resources to avoid real DNS/HTTP calls during removal.
    with patch("toolkit.core.infra.hosts.cleanup_host_resources"):
        result = runner.invoke(main, ["--root", str(tmp_path), "fleet", "remove", "nas", "-y"])
    assert result.exit_code == 0
    assert "Removed" in result.output


def test_fleet_remove_confirmation_declined(tmp_path):
    _setup_config(tmp_path)
    runner = CliRunner()
    runner.invoke(
        main,
        [
            "--root",
            str(tmp_path),
            "fleet",
            "add",
            "nas",
            "10.0.0.5",
            "--plain",
            "--service",
            "media-cache",
            "--integration-setting",
            "media-cache.path=/mnt/pool/media",
            "--skip-onboard",
        ],
    )
    result = runner.invoke(main, ["--root", str(tmp_path), "fleet", "remove", "nas"], input="n\n")
    assert result.exit_code == 1
    assert "Removed" not in result.output


def test_fleet_remove_nonexistent(tmp_path):
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "fleet", "remove", "nope", "-y"])
    assert result.exit_code == 0
    assert "not found" in result.output


def test_fleet_add_with_explicit_service_settings(tmp_path):
    """Fleet integrations and their fields are selected without core presets."""
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--root",
            str(tmp_path),
            "fleet",
            "add",
            "cache-01",
            "203.0.113.10",
            "--service",
            "media-cache",
            "--integration-setting",
            "media-cache.path=/srv/cache",
            "--skip-onboard",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Added fleet node" in result.output
    assert "media-cache" in result.output
    assert "ldap-client" not in result.output


def test_fleet_add_default_has_ldap_client(tmp_path):
    """Default fleet add includes ldap-client (SSH via LLDAP is core to fleet)."""
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--root", str(tmp_path), "fleet", "add", "edge-01", "203.0.113.20", "--skip-onboard"],
    )
    assert result.exit_code == 0, result.output
    assert "ldap-client" in result.output
    assert "wazuh-agent" in result.output


def test_fleet_add_custom_services(tmp_path):
    """Repeated --service options define an explicit integration set."""
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--root",
            str(tmp_path),
            "fleet",
            "add",
            "minimal-01",
            "203.0.113.30",
            "--service",
            "wazuh-agent",
            "--service",
            "monitoring-agent",
            "--skip-onboard",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "wazuh-agent" in result.output
    assert "monitoring-agent" in result.output
    assert "ldap-client" not in result.output


def test_deploy_status(tmp_path):
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "deploy", "status"])
    assert result.exit_code == 0
    assert "infra" in result.output


def test_services_status_no_compose(tmp_path):
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "services", "status"])
    assert result.exit_code == 1


def test_services_start_no_compose(tmp_path):
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "services", "start"])
    assert result.exit_code == 0
    assert "Done" in result.output


def test_services_stop_no_compose(tmp_path):
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "services", "stop"])
    assert result.exit_code == 0
    assert "Done" in result.output
