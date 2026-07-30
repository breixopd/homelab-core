from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
from tests.helpers.machines import renamed_default_machines
from toolkit.cli import main
from toolkit.core.config.config import Config, ServicesConfig


def test_install_help():
    runner = CliRunner()
    result = runner.invoke(main, ["install", "--help"])
    assert result.exit_code == 0
    assert "Interactive first-run setup wizard" in result.output
    assert "--preset" in result.output
    assert "--smoke-test" in result.output


def test_install_invalid_preset():
    runner = CliRunner()
    result = runner.invoke(main, ["--root", "/tmp/nonexistent", "install", "--preset", "invalid"])
    assert result.exit_code != 0
    assert "invalid" in result.output.lower() or "not" in result.output.lower()


def test_install_preset_minimal(tmp_path: Path):
    """Install with --preset minimal should succeed."""
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "install", "--preset", "minimal"])
    assert result.exit_code == 0
    assert "Setup Complete" in result.output


def test_install_rejects_removed_full_preset(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "install", "--preset", "full"])
    assert result.exit_code != 0
    assert "Unknown preset" in result.output


def test_install_preset_all(tmp_path: Path):
    """Install with --preset all should succeed (same as full)."""
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "install", "--preset", "all"])
    assert result.exit_code == 0
    assert "Setup Complete" in result.output


def test_install_preset_media(tmp_path: Path):
    """Install with --preset media should succeed."""
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "install", "--preset", "media"])
    assert result.exit_code == 0
    assert "Setup Complete" in result.output


def test_category_presets_are_discovered_at_runtime() -> None:
    from toolkit.cli.install_cmd import _preset_config

    config = _preset_config("notifications")

    assert config.category_enabled("notifications") is True
    assert config.category_enabled("media") is False


def test_media_preset_machine_scope_follows_service_placements_after_rename() -> None:
    from toolkit.cli.install_cmd import _scope_machines_to_enabled_services
    from toolkit.core.compose.registry import all_categories, load_all

    load_all()
    services = ServicesConfig.model_validate(
        {category.name: category.always_on or category.name == "media" for category in all_categories()}
    )
    cfg = Config(services=services, machines=renamed_default_machines())

    scoped = _scope_machines_to_enabled_services(cfg)

    assert set(scoped.enabled_nodes) == {"core", "stream"}
    assert scoped.machines["data"].enabled is False


def test_interactive_service_settings_are_discovered_from_manifests(monkeypatch) -> None:
    from toolkit.cli.install_cmd import _collect_service_settings
    from toolkit.core.compose.registry import all_categories, load_all

    load_all()
    services = ServicesConfig.model_validate({category.name: True for category in all_categories()})

    def prompt(label, *, type=None, default=None, **_kwargs):
        if "Media servers" in label:
            return "plex"
        if "VPN provider" in label:
            return "protonvpn"
        return default

    monkeypatch.setattr("toolkit.cli.install_cmd.click.prompt", prompt)
    monkeypatch.setattr("toolkit.cli.install_cmd.click.confirm", lambda label, default=True: default)

    settings = _collect_service_settings(services)

    assert settings["media-library"]["server"] == "plex"
    assert settings["gluetun"]["provider"] == "protonvpn"
    assert settings["media-cache"]["enabled"] is False


def test_install_smoke_test(tmp_path: Path):
    """Install with --smoke-test should create merged .env."""
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--root", str(tmp_path), "install", "--preset", "minimal", "--smoke-test"],
    )
    assert result.exit_code == 0
    env_file = tmp_path / ".env"
    assert env_file.exists(), f"Smoke-test .env not created at {env_file}"
