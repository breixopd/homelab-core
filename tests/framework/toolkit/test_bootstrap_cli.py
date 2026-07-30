from __future__ import annotations

import tomllib
from datetime import UTC, datetime
from pathlib import Path

from click.testing import CliRunner
from toolkit.cli import main
from toolkit.controller.read_models import BootstrapCapabilityIssue


class FakeController:
    def __init__(self, token: str) -> None:
        self.token = token
        self.closed = False

    def issue_bootstrap_capability(self) -> BootstrapCapabilityIssue:
        return BootstrapCapabilityIssue(
            token=self.token,
            expires_at=datetime(2026, 7, 10, 12, 15, tzinfo=UTC),
        )

    def close(self) -> None:
        self.closed = True


def test_bootstrap_token_prints_once_and_never_places_secret_in_url(monkeypatch) -> None:
    token = "00000000-0000-4000-8000-000000000000.bootstrap-secret-value"
    controller = FakeController(token)
    monkeypatch.setattr("toolkit.cli.controller_client_from_environment", lambda: controller)

    result = CliRunner().invoke(main, ["bootstrap", "token"])

    assert result.exit_code == 0, result.exception
    assert result.output.count(token) == 1
    assert "?" not in result.output
    assert "expires" in result.output.lower()
    assert "/setup" in result.output
    assert controller.closed is True


def test_controller_independent_cli_command_does_not_load_transport_token(monkeypatch) -> None:
    def fail_if_called():
        raise AssertionError("controller transport must be lazy")

    monkeypatch.setattr("toolkit.cli.controller_client_from_environment", fail_if_called)

    result = CliRunner().invoke(main, ["version"])

    assert result.exit_code == 0, result.exception


def test_version_command_reads_project_metadata_in_source_snapshot(monkeypatch) -> None:
    from importlib.metadata import PackageNotFoundError

    def missing_distribution(_name: str) -> str:
        raise PackageNotFoundError

    monkeypatch.setattr("toolkit.cli.distribution_version", missing_distribution)
    expected = tomllib.loads((Path(__file__).parents[3] / "pyproject.toml").read_text())["project"]["version"]

    result = CliRunner().invoke(main, ["version"])

    assert result.exit_code == 0, result.exception
    assert result.output == f"homelab-toolkit {expected}\n"
