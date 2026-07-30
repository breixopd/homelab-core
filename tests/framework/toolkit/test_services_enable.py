from __future__ import annotations

import yaml
from click.testing import CliRunner
from toolkit.categories import Category
from toolkit.cli import main
from toolkit.core.config.config import Config, load_config, save_config
from toolkit.core.config.storage import config_path


def _write_config(root) -> None:
    save_config(Config(domain="example.com", email="admin@example.com"), config_path(root))


def test_enable_category_is_discovered_from_plugin_registry(tmp_path, monkeypatch) -> None:
    _write_config(tmp_path)
    from toolkit.core.compose import registry

    registry.load_all()
    monkeypatch.setitem(
        registry._REGISTRY,
        "custom-addon",
        Category(name="custom-addon", label="Custom", compose_file="", placement="control"),
    )

    result = CliRunner().invoke(
        main,
        ["--root", str(tmp_path), "services", "enable", "custom-addon"],
    )

    assert result.exit_code == 0, (result.output, result.exception)
    assert "Enabled 'custom-addon'" in result.output
    assert yaml.safe_load(config_path(tmp_path).read_text())["services"]["custom-addon"] is True
    assert load_config(config_path(tmp_path)).services.enabled("custom-addon") is True


def test_enable_category_rejects_unknown_plugin(tmp_path) -> None:
    _write_config(tmp_path)

    result = CliRunner().invoke(
        main,
        ["--root", str(tmp_path), "services", "enable", "not-installed"],
    )

    assert result.exit_code != 0
    assert "Unknown service category" in result.output


def test_enable_category_refuses_to_disable_always_on_category(tmp_path) -> None:
    _write_config(tmp_path)

    result = CliRunner().invoke(
        main,
        ["--root", str(tmp_path), "services", "enable", "management", "--disable"],
    )

    assert result.exit_code != 0
    assert "always-on" in result.output
    assert load_config(config_path(tmp_path)).category_enabled("management") is True


def test_enable_category_refuses_to_break_enabled_dependency(tmp_path, monkeypatch) -> None:
    _write_config(tmp_path)
    from toolkit.core.compose import registry

    registry.load_all()
    monkeypatch.setitem(
        registry._REGISTRY,
        "custom-addon",
        Category(
            name="custom-addon",
            label="Custom",
            compose_file="",
            placement="control",
            _depends_on=["media"],
        ),
    )

    result = CliRunner().invoke(
        main,
        ["--root", str(tmp_path), "services", "enable", "media", "--disable"],
    )

    assert result.exit_code != 0
    assert "required by enabled categories: custom-addon" in result.output
    assert load_config(config_path(tmp_path)).category_enabled("media") is True
