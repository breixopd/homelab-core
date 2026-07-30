from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner
from toolkit.cli import main
from toolkit.core.config.config import Config, ProjectEntry, load_config, save_config
from toolkit.core.config.storage import config_path

PINNED_IMAGE = "docker.io/library/nginx:1@sha256:" + "a" * 64


def _setup_config(root: Path, *, with_project: bool = False) -> None:
    cfg = Config(domain="example.com", email="admin@example.com")
    if with_project:
        cfg.projects.entries.append(
            ProjectEntry(
                name="demo",
                subdomain="demo",
                auth_mode="forward_auth",
                exposure="private",
                docker_image=PINNED_IMAGE,
                container_port=8080,
                placement="apps",
            )
        )
    save_config(cfg, config_path(root))


@pytest.mark.parametrize(
    ("arguments", "expected_error"),
    [
        (["--subdomain", "demo", "--image", "nginx:latest"], "immutable tag and sha256 digest"),
        (["--subdomain", "demo;id", "--image", PINNED_IMAGE], "lowercase DNS label"),
        (["--subdomain", "demo", "--image", PINNED_IMAGE, "--port", "70000"], "less than or equal to 65535"),
    ],
)
def test_projects_add_rejects_non_declarative_definition(
    tmp_path: Path, arguments: list[str], expected_error: str
) -> None:
    _setup_config(tmp_path)

    result = CliRunner().invoke(main, ["--root", str(tmp_path), "projects", "add", *arguments])

    assert result.exit_code != 0
    assert expected_error in result.output
    assert load_config(config_path(tmp_path)).projects.entries == []


def test_projects_add_persists_validated_immutable_definition(tmp_path: Path) -> None:
    _setup_config(tmp_path)

    with patch("toolkit.cli.projects_cmd.run_full_generate", return_value={}):
        result = CliRunner().invoke(
            main,
            [
                "--root",
                str(tmp_path),
                "projects",
                "add",
                "--subdomain",
                "demo",
                "--image",
                PINNED_IMAGE,
                "--port",
                "45678",
                "--placement",
                "apps",
            ],
        )

    assert result.exit_code == 0, (result.output, result.exception)
    entry = load_config(config_path(tmp_path)).projects.entries[0]
    assert entry.subdomain == "demo"
    assert entry.docker_image == PINNED_IMAGE
    assert entry.container_port == 45678
    assert entry.placement == "apps"
    assert entry.upstream == "demo:45678"
    assert entry.auth_mode == "forward_auth"
    assert entry.exposure == "private"


def test_projects_list_shows_placement_and_resolved_node(tmp_path: Path) -> None:
    _setup_config(tmp_path, with_project=True)

    result = CliRunner().invoke(main, ["--root", str(tmp_path), "projects", "list"])

    assert result.exit_code == 0
    assert "Placement" in result.output
    assert "Node" in result.output
    assert "apps" in result.output


@pytest.mark.parametrize("command", ["stop", "start", "restart", "logs", "status"])
def test_project_runtime_commands_use_bounded_runtime_adapter(tmp_path: Path, command: str) -> None:
    _setup_config(tmp_path, with_project=True)
    from toolkit.core.projects.runtime import ProjectCommandResult

    with patch(
        "toolkit.core.projects.runtime.run_project_command",
        return_value=ProjectCommandResult(True, "completed", "apps"),
    ) as run:
        result = CliRunner().invoke(main, ["--root", str(tmp_path), "projects", command, "demo"])

    assert result.exit_code == 0, (result.output, result.exception)
    assert "completed" in result.output
    assert run.call_args.args[2:] == ("demo", command)


def test_projects_ps_lists_runtime_state(tmp_path: Path) -> None:
    _setup_config(tmp_path, with_project=True)
    from toolkit.core.projects.runtime import ProjectCommandResult

    with patch(
        "toolkit.core.projects.runtime.run_project_command",
        return_value=ProjectCommandResult(True, '{"Status":"running"}', "apps"),
    ):
        result = CliRunner().invoke(main, ["--root", str(tmp_path), "projects", "ps"])

    assert result.exit_code == 0, (result.output, result.exception)
    assert "demo" in result.output
    assert "running" in result.output


def test_projects_deploy_runs_reconciliation(tmp_path: Path) -> None:
    _setup_config(tmp_path, with_project=True)

    with patch("toolkit.cli.projects_cmd._deploy_project_stack", return_value=True) as deploy:
        result = CliRunner().invoke(main, ["--root", str(tmp_path), "projects", "deploy", "demo"])

    assert result.exit_code == 0, (result.output, result.exception)
    deploy.assert_called_once_with(tmp_path, "demo")
