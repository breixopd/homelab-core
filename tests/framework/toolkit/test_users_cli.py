from __future__ import annotations

from unittest.mock import MagicMock, patch

from click.testing import CliRunner
from toolkit.cli import main


def test_users_list_cli():
    runner = CliRunner()
    with patch("toolkit.cli.context.load_controller_client") as load_client:
        load_client.return_value.directory_users.return_value.users = [
            MagicMock(id="brei", email="a@b.c", display_name="Brei")
        ]
        result = runner.invoke(main, ["--root", "/tmp", "users", "list"])
    assert result.exit_code == 0
    assert "brei" in result.output
    assert "Email" in result.output


def test_users_list_cli_reports_unreachable_private_directory_without_traceback():
    runner = CliRunner()
    with patch("toolkit.cli.context.load_controller_client") as load_client:
        from toolkit.controller.client import ControllerUnavailableError

        load_client.side_effect = ControllerUnavailableError()
        result = runner.invoke(main, ["--root", "/tmp", "users", "list"])

    assert result.exit_code == 1
    assert "homelab controller is unavailable" in result.output
    assert "Traceback" not in result.output


def test_users_create_prompts_for_password_on_tty(tmp_path):
    runner = CliRunner()
    cfg = MagicMock()
    cfg.services.enabled.side_effect = lambda category: category == "media"
    fake_stdin = MagicMock()
    fake_stdin.isatty.return_value = True

    with (
        patch("toolkit.cli.users_cmd._client", return_value=MagicMock()),
        patch("toolkit.cli.load_root_config", return_value=(tmp_path, cfg)),
        patch("toolkit.cli.users_cmd.load_secrets_plaintext", return_value={}),
        patch("toolkit.cli.users_cmd.click.get_text_stream", return_value=fake_stdin),
        patch("toolkit.cli.users_cmd.getpass.getpass", return_value="typed-password") as getpass_mock,
        patch("toolkit.core.identity.user_provision.invite_and_provision_user", return_value=["ok"]) as provision,
    ):
        result = runner.invoke(main, ["--root", str(tmp_path), "users", "create", "a@b.c"])

    assert result.exit_code == 0
    assert "ok" in result.output
    getpass_mock.assert_called_once()
    assert provision.call_args.kwargs["password"] == "typed-password"


def test_users_delete_resolves_email_and_requires_yes():
    runner = CliRunner()
    user = MagicMock(id="brei", email="brei@example.com", display_name="Brei")
    with patch("toolkit.cli.users_cmd._client") as mock_client:
        inst = MagicMock()
        inst.list_users.return_value = [user]
        mock_client.return_value = inst
        result = runner.invoke(main, ["--root", "/tmp", "users", "delete", "brei@example.com", "--yes"])

    assert result.exit_code == 0
    assert "brei <brei@example.com>" in result.output
    inst.delete_user.assert_called_once_with("brei")


def test_users_delete_abort_does_not_delete():
    runner = CliRunner()
    user = MagicMock(id="brei", email="brei@example.com", display_name="Brei")
    with patch("toolkit.cli.users_cmd._client") as mock_client:
        inst = MagicMock()
        inst.list_users.return_value = [user]
        mock_client.return_value = inst
        result = runner.invoke(main, ["--root", "/tmp", "users", "delete", "brei"], input="n\n")

    assert result.exit_code == 0
    assert "Aborted" in result.output
    inst.delete_user.assert_not_called()
