from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
from toolkit.cli import main
from toolkit.cli.update import _build_email_html, update
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path


def test_update_cli_exposes_only_controller_backed_workflows() -> None:
    result = CliRunner().invoke(update, ["--help"])

    assert result.exit_code == 0
    assert "apply" in result.output
    assert "check" in result.output
    assert "diff" in result.output
    assert "rollback" in result.output
    assert "pull" not in result.output
    assert "snapshots" not in result.output
    assert "self" not in result.output


def test_update_apply_requires_an_explicit_selection() -> None:
    result = CliRunner().invoke(update, ["apply"])

    assert result.exit_code == 2
    assert "Specify SERVICE or use --all" in result.output


def test_update_check_reports_unavailable_local_and_ssh_controller_without_traceback(
    tmp_path: Path, monkeypatch
) -> None:
    save_config(Config(domain="example.com", email="admin@example.com"), config_path(tmp_path))

    def unavailable_controller():
        raise FileNotFoundError("/var/lib/homelab-controller/local.token")

    monkeypatch.setattr("toolkit.cli.controller_client_from_environment", unavailable_controller)
    result = CliRunner().invoke(main, ["--root", str(tmp_path), "update", "check", "--no-notify"])

    assert result.exit_code == 1
    assert "homelab controller is unavailable" in result.output
    assert "Traceback" not in result.output
    assert "/var/lib/homelab-controller/local.token" not in result.output


def test_update_email_escapes_report_fields_and_rejects_unsafe_links() -> None:
    html = _build_email_html(
        [
            {
                "service": '<img src=x onerror="alert(1)">',
                "current": "1<2",
                "latest": "3>2",
                "changelog_url": "javascript:alert(1)",
            }
        ],
        [{"name": "tool<script>", "current": "1&2", "latest": "3", "source": "registry"}],
        "example.com<script>",
        "javascript:alert(2)",
    )

    assert "<img src=x" not in html
    assert "<script>" not in html
    assert "javascript:" not in html
    assert 'href=""' not in html
    assert "&lt;img src=x" in html
    assert "1&lt;2" in html
    assert "1&amp;2" in html
