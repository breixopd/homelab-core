from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
from toolkit.cli import main


def test_ops_default_shows_status(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("domain: example.com\n")
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "ops"])
    assert result.exit_code == 0
    assert "Domain: example.com" in result.output


def test_ldap_sync_hidden_group(tmp_path: Path):
    (tmp_path / "config.yaml").write_text("domain: example.com\n")
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "ldap", "sync", "--help"])
    assert result.exit_code == 0
