from __future__ import annotations

from unittest.mock import Mock

import click
from click.testing import CliRunner
from toolkit.cli.secrets_cmd import _invoke_rotation_deployment, secrets


def test_rotation_deployment_uses_canonical_full_deploy_command() -> None:
    context = Mock(spec=click.Context)

    assert _invoke_rotation_deployment(context) is True

    command = context.invoke.call_args.args[0]
    assert command.name == "all"
    assert context.invoke.call_args.kwargs == {
        "as_json": False,
        "skip_infra": False,
        "skip_dns": False,
        "destroy_first": False,
        "yes": True,
        "log_file": None,
        "vm": None,
        "dry_run": False,
    }


def test_rotation_deployment_reports_nonzero_click_exit() -> None:
    context = Mock(spec=click.Context)
    context.invoke.side_effect = click.exceptions.Exit(1)

    assert _invoke_rotation_deployment(context) is False


def test_rotation_deployment_treats_pre_workflow_click_errors_as_failure() -> None:
    context = Mock(spec=click.Context)
    context.invoke.side_effect = click.ClickException("preflight failed")

    assert _invoke_rotation_deployment(context) is False


def test_failed_applied_rotation_restores_and_redeploys_previous_state(monkeypatch, tmp_path) -> None:
    events: list[str] = []
    previous = {"KOMODO_DATABASE_PASSWORD": "previous-secret"}

    monkeypatch.setattr("toolkit.cli.secrets_cmd.load_config", lambda _path: object())
    monkeypatch.setattr("toolkit.cli.secrets_cmd.load_secrets_plaintext", lambda _path: dict(previous))
    monkeypatch.setattr(
        "toolkit.core.ops.db_safety.pre_deploy_dump",
        lambda _cfg, _root: events.append("dump") or "/safe/pre-rotation.sql.gz",
    )
    monkeypatch.setattr(
        "toolkit.core.secrets.secrets.rotate_secrets",
        lambda _root, _specific: events.append("rotate") or {"KOMODO_DATABASE_PASSWORD": "new-secret"},
    )
    deployments = iter((False, True))
    monkeypatch.setattr(
        "toolkit.cli.secrets_cmd._invoke_rotation_deployment",
        lambda _ctx, **_kwargs: events.append("deploy") or next(deployments),
    )
    monkeypatch.setattr(
        "toolkit.controller.settings_api.restore_secret_values",
        lambda _root, values, expected: (
            events.append("restore")
            if values == previous and expected == {"KOMODO_DATABASE_PASSWORD": "new-secret"}
            else None
        ),
    )
    monkeypatch.setattr("toolkit.core.state.audit_log.audit", lambda *_args, **_kwargs: None)

    result = CliRunner().invoke(
        secrets,
        ["rotate", "--name", "KOMODO_DATABASE_PASSWORD", "--yes", "--apply"],
        obj={"root": str(tmp_path)},
    )

    assert result.exit_code == 1
    assert events == ["dump", "rotate", "deploy", "restore", "deploy"]
    assert "Previous credentials restored and redeployed" in result.output
    assert "previous-secret" not in result.output
    assert "new-secret" not in result.output


def test_interrupted_applied_rotation_restores_before_propagating(monkeypatch, tmp_path) -> None:
    events: list[str] = []
    previous = {"KOMODO_DATABASE_PASSWORD": "previous-secret"}
    monkeypatch.setattr("toolkit.cli.secrets_cmd.load_config", lambda _path: object())
    monkeypatch.setattr("toolkit.cli.secrets_cmd.load_secrets_plaintext", lambda _path: dict(previous))
    monkeypatch.setattr("toolkit.core.ops.db_safety.pre_deploy_dump", lambda *_args: None)
    monkeypatch.setattr(
        "toolkit.core.secrets.secrets.rotate_secrets",
        lambda *_args: {"KOMODO_DATABASE_PASSWORD": "new-secret"},
    )
    deployments = iter((KeyboardInterrupt(), True))

    def deploy(*_args, **_kwargs):
        events.append("deploy")
        outcome = next(deployments)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    monkeypatch.setattr("toolkit.cli.secrets_cmd._invoke_rotation_deployment", deploy)
    monkeypatch.setattr(
        "toolkit.controller.settings_api.restore_secret_values",
        lambda *_args: events.append("restore"),
    )
    monkeypatch.setattr("toolkit.core.state.audit_log.audit", lambda *_args, **_kwargs: None)

    result = CliRunner().invoke(
        secrets,
        ["rotate", "--name", "KOMODO_DATABASE_PASSWORD", "--yes", "--apply"],
        obj={"root": str(tmp_path)},
    )

    assert result.exit_code != 0
    assert events == ["deploy", "restore", "deploy"]
