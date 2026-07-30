from types import SimpleNamespace

import click
from toolkit.cli import deploy_cmd


def test_recap_deduplicates_failed_warning_and_marks_partial_summary_red(capsys, monkeypatch) -> None:
    failed = "✗ music-sync.api_status: warnings: Spotify OAuth is not completed yet"
    colored: list[tuple[str, str | None]] = []

    def record_secho(message: str, **kwargs) -> None:
        colored.append((message, kwargs.get("fg")))
        click.echo(message)

    monkeypatch.setattr(deploy_cmd.click, "secho", record_secho)
    deploy_cmd._print_deploy_recap(
        SimpleNamespace(success=False, message="Recovery finished with issues"),
        {"hook_verify": "fail"},
        {"hook_verify": "Verify hooks"},
        ["Summary: 273/278 checks passed", failed, failed],
    )

    output = capsys.readouterr().out
    assert output.count(failed) == 1
    assert ("  Summary: 273/278 checks passed", "red") in colored
