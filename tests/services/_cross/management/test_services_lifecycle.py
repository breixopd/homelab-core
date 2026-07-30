"""services stop/start must filter by name (the restart pattern), not
stop the entire stack. Today `services stop <name>` calls dc.down() with no
filter — a data-loss footgun (stops every service on the VM).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from click.testing import CliRunner
from toolkit.cli import main


def _config_yaml(tmp_path: Path) -> Path:
    (tmp_path / "config.yaml").write_text("domain: example.com\nemail: a@b.com\n")
    return tmp_path


def test_services_stop_by_name_only_stops_that_service(tmp_path: Path):
    """`services stop grafana` must call dc.down(services=['grafana']) — NOT
    dc.down() which stops the entire stack."""
    _config_yaml(tmp_path)
    runner = CliRunner()

    fake_dc = MagicMock()
    fake_dc.down = MagicMock()
    with patch("toolkit.cli.services.compose_for_root", return_value=fake_dc):
        result = runner.invoke(main, ["--root", str(tmp_path), "services", "stop", "grafana"])

    assert result.exit_code == 0, (result.output, result.exception)
    # CRITICAL: down() must have been called with services=['grafana'] when a name
    # is given — NOT with no args (which would stop everything).
    assert fake_dc.down.called, "dc.down() was never called"
    call_kwargs = fake_dc.down.call_args.kwargs
    assert call_kwargs.get("services") == ["grafana"], (
        f"dc.down() called with {call_kwargs} — stopping the whole stack instead of "
        "the named service. Data-loss footgun."
    )


def test_services_stop_no_name_stops_stack(tmp_path: Path):
    """`services stop` (no name) stops the whole stack — that's the intended batch path."""
    _config_yaml(tmp_path)
    runner = CliRunner()

    fake_dc = MagicMock()
    with patch("toolkit.cli.services.compose_for_root", return_value=fake_dc):
        result = runner.invoke(main, ["--root", str(tmp_path), "services", "stop"])

    assert result.exit_code == 0, (result.output, result.exception)
    assert fake_dc.down.called


def test_services_start_by_name_only_starts_that_service(tmp_path: Path):
    """`services start grafana` must call dc.up(services=['grafana']) on the
    owning VM only — not fan out to every VM."""
    _config_yaml(tmp_path)
    runner = CliRunner()

    fake_dc = MagicMock()
    with patch("toolkit.cli.services.compose_for_root", return_value=fake_dc):
        result = runner.invoke(main, ["--root", str(tmp_path), "services", "start", "grafana"])

    assert result.exit_code == 0, (result.output, result.exception)
    assert fake_dc.up.called
    call_kwargs = fake_dc.up.call_args.kwargs
    assert "services" in call_kwargs, "dc.up() called without services= — fans out to every service"
    assert call_kwargs["services"] == ["grafana"], (
        f"dc.up() called with {call_kwargs} — starting the whole stack instead of the named service."
    )


def test_services_stop_unknown_name_reports_clearly(tmp_path: Path):
    """`services stop no-such-service` reports the service isn't found (not silently
    stops everything)."""
    _config_yaml(tmp_path)
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "services", "stop", "no-such-service"])
    assert "not found" in result.output.lower()
