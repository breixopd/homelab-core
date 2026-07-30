from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from click.testing import CliRunner
from toolkit.cli import main


def test_services_status_uses_controller_inventory() -> None:
    inventory = MagicMock(is_available=True, unavailable_nodes=[])
    inventory.containers = [
        SimpleNamespace(
            name="lldap",
            node="infra",
            state="running",
            health="healthy",
            image="lldap:stable",
        )
    ]
    with patch("toolkit.cli.services.load_controller_client") as load_client:
        load_client.return_value.container_inventory.return_value = inventory
        result = CliRunner().invoke(main, ["--root", "/tmp", "services", "status"])

    assert result.exit_code == 0
    assert "[infra]" in result.output
    assert "lldap" in result.output
    assert "healthy" in result.output


def test_services_status_fails_when_a_node_inventory_is_unavailable() -> None:
    inventory = MagicMock(is_available=False, containers=[], unavailable_nodes=["media"])
    with patch("toolkit.cli.services.load_controller_client") as load_client:
        load_client.return_value.container_inventory.return_value = inventory
        result = CliRunner().invoke(main, ["--root", "/tmp", "services", "status"])

    assert result.exit_code == 1
    assert "[media] Container inventory unavailable" in result.output
