from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
from toolkit.cli import main
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path


def _root(tmp_path: Path) -> Path:
    save_config(Config(domain="example.com"), config_path(tmp_path))
    return tmp_path


def test_services_routes_reports_manifest_exposure_and_auth(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main,
        ["--root", str(_root(tmp_path)), "services", "routes", "fmd-server"],
    )

    assert result.exit_code == 0
    assert "https://fmd.example.com" in result.output
    assert "public" in result.output
    assert "split" in result.output
    assert "internal" not in result.output
    assert "open" not in result.output


def test_config_exposure_is_a_compiled_route_report(tmp_path: Path) -> None:
    result = CliRunner().invoke(
        main,
        ["--root", str(_root(tmp_path)), "config", "exposure"],
    )

    assert result.exit_code == 0
    assert "Auth" in result.output
    assert "forward_auth" in result.output
    assert "oidc" in result.output
    assert "private" in result.output
