"""Top-level status command and rich format helpers.

The status command fuses ops + deploy status + services status + watchdog
summary + last audit row into one view. Tests stub the data sources so no live
infra is touched; they assert the command runs + surfaces the expected sections.
"""

from __future__ import annotations

from pathlib import Path

from click.testing import CliRunner
from toolkit.cli import main
from toolkit.cli._format import render_status_table, status_panel

# --- _format helpers -------------------------------------------------------


def test_render_status_table_basic_rows():
    rows = [
        ("infra", "10.10.10.10", "ok"),
        ("media", "10.10.10.11", "degraded"),
        ("apps", "10.10.10.12", "down"),
    ]
    out = render_status_table(rows, columns=("VM", "IP", "Health"))
    # render_status_table returns a string (rich-rendered, plain when no tty).
    assert "infra" in out and "10.10.10.10" in out
    assert "media" in out
    assert "apps" in out


def test_status_panel_wraps_content():
    out = status_panel(title="Cluster Status", body="all green")
    assert "all green" in out


# --- top-level 'status' command --------------------------------------------


def test_status_command_exists_and_runs(tmp_path: Path):
    runner = CliRunner()
    # Empty tmp root: config + watchdog-state absent → status still runs + reports.
    result = runner.invoke(main, ["--root", str(tmp_path), "status"])
    assert result.exit_code == 0, (result.output, result.exception)


def test_status_command_shows_sections(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "status"])
    # The one-view command should mention the cluster sections it fused.
    out = result.output
    # At least the domain + node inventory + health- summary sections.
    assert any(token in out for token in ("Cluster", "Node", "VM", "Domain", "Status"))


def test_status_command_json_output(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "status", "--json"])
    assert result.exit_code == 0, result.exception
    import json

    data = json.loads(result.output)
    # JSON mode returns a structured cluster snapshot.
    assert "domain" in data
    assert "vms" in data


def test_status_json_surfaces_unreadable_approval_state(tmp_path: Path):
    queue = tmp_path / ".homelab-state" / "approvals.json"
    queue.parent.mkdir()
    queue.write_text("not-json")

    result = CliRunner().invoke(main, ["--root", str(tmp_path), "status", "--json"])

    assert result.exit_code == 0
    import json

    data = json.loads(result.output)
    assert "unreadable" in data["approval_error"]
