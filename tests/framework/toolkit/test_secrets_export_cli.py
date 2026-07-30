from __future__ import annotations

import json
from unittest.mock import patch

from click.testing import CliRunner
from toolkit.cli import main


def test_secrets_export_json_uses_stdout_for_safe_pipelines() -> None:
    with patch(
        "toolkit.cli.secrets_cmd.load_secrets_plaintext",
        return_value={"SSO_USER_PASSWORD": "owner-password"},
    ):
        result = CliRunner().invoke(main, ["--root", "/tmp", "secrets", "export", "--format", "json"])

    assert result.exit_code == 0
    assert json.loads(result.stdout) == {"SSO_USER_PASSWORD": "owner-password"}
    assert result.stderr == ""


def test_secrets_export_env_uses_stdout_for_safe_pipelines() -> None:
    with patch(
        "toolkit.cli.secrets_cmd.load_secrets_plaintext",
        return_value={"ONE": "value", "MULTILINE": "first\nsecond"},
    ):
        result = CliRunner().invoke(main, ["--root", "/tmp", "secrets", "export", "--format", "env"])

    assert result.exit_code == 0
    assert "ONE=value" in result.stdout
    assert "# SKIPPED MULTILINE" in result.stdout
    assert result.stderr == ""
