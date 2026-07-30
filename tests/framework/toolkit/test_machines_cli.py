from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from click.testing import CliRunner
from toolkit.cli import main
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path


def test_machines_list_reports_all_configured_kinds(tmp_path: Path) -> None:
    save_config(Config(), config_path(tmp_path))

    result = CliRunner().invoke(main, ["--root", str(tmp_path), "machines", "list"])

    assert result.exit_code == 0, result.output
    assert "infra" in result.output
    assert "LXC" in result.output


def test_lxc_command_is_not_registered() -> None:
    result = CliRunner().invoke(main, ["lxc", "--help"])

    assert result.exit_code != 0
    assert "No such command 'lxc'" in result.output


def test_machines_add_uses_strict_definition_and_regenerates(monkeypatch, tmp_path: Path) -> None:
    save_config(Config(), config_path(tmp_path))
    definition = tmp_path / "worker.yaml"
    definition.write_text(
        "\n".join(
            (
                "kind: lxc",
                "provider: proxmox",
                "enabled: true",
                "managed: false",
                "hostname: worker-01",
                "address: 10.10.10.20",
                "gateway: 10.10.10.1",
                "vmid: 820",
                "labels: [compute]",
            )
        )
        + "\n",
        encoding="utf-8",
    )
    generated: list[tuple[Path, bool]] = []

    def run_full_generate(root: Path, *, validate: bool):
        generated.append((root, validate))
        return {"compose": [root / "generated" / "compose.yaml"]}

    monkeypatch.setattr("toolkit.core.generate.generate.run_full_generate", run_full_generate)

    result = CliRunner().invoke(
        main,
        ["--root", str(tmp_path), "machines", "add", "worker-east", "--file", str(definition)],
    )

    assert result.exit_code == 0, result.output
    assert "Added machine worker-east" in result.output
    assert generated == [(tmp_path, True)]


def test_machines_retire_uses_revision_bound_controller_approval(tmp_path: Path) -> None:
    client = MagicMock()
    plan = SimpleNamespace(
        plan_id="plan-identifier-1234",
        plan_hash="a" * 64,
        spec=SimpleNamespace(
            action="retire_machine",
            scopes=["worker-east"],
            config_revision="c" * 64,
            checkpoint_id="b" * 32,
            checkpoint_verified_at=datetime(2026, 7, 15, tzinfo=UTC),
        ),
    )
    client.create_destruction_plan.return_value = plan
    client.approve_plan.return_value = SimpleNamespace(token="approval-token-123456")
    client.submit.return_value = SimpleNamespace(job_id="job-retire-machine")
    finished = SimpleNamespace(state=SimpleNamespace(value="SUCCEEDED"), error=None)
    with (
        patch("toolkit.cli.load_controller_client", return_value=client),
        patch("toolkit.cli.controller_jobs.wait_for_controller_job", return_value=finished),
    ):
        result = CliRunner().invoke(
            main,
            ["--root", str(tmp_path), "machines", "retire", "worker-east", "--yes"],
        )

    assert result.exit_code == 0, result.output
    assert "Checkpoint" in result.output
    assert "retired and independently verified" in result.output
    client.approve_plan.assert_called_once_with(
        plan.plan_id,
        plan_hash=plan.plan_hash,
        confirmation="RETIRE MACHINE worker-east",
    )
    operation = client.submit.call_args.args[0].operation
    assert operation.action == "retire_machine"
    assert operation.config_revision == "c" * 64
