from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner
from toolkit.cli.maintenance_cmd import maintenance
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.ops.backups import BackupResult, run_node_snapshot
from toolkit.core.ops.logical_backups import LogicalDumpResult


def _env(root: Path, role: str) -> None:
    if not config_path(root).is_file():
        save_config(Config(backups={"enabled": True}), config_path(root))
    path = root / "generated" / role / ".env"
    path.parent.mkdir(parents=True)
    path.write_text(
        "KOPIA_SERVER_HOST=10.10.10.10\nKOPIA_SERVER_CERT_FINGERPRINT=abc123\n",
        encoding="utf-8",
    )


def test_agent_snapshot_connects_then_creates_tagged_snapshot(tmp_path: Path, monkeypatch) -> None:
    _env(tmp_path, "media")
    calls: list[tuple[str, list[str]]] = []

    def fake_exec(container: str, command: list[str], **_kwargs):
        calls.append((container, command))
        if command[:3] == ["kopia", "repository", "status"]:
            return 1, "not connected"
        return 0, "ok"

    monkeypatch.setattr("toolkit.core.ops.backups.docker_exec", fake_exec)

    result = run_node_snapshot(tmp_path, "media")

    assert result.ok
    assert (
        "kopia-agent",
        [
            "kopia",
            "repository",
            "connect",
            "server",
            "--url=https://10.10.10.10:51515",
            "--server-cert-fingerprint=abc123",
            "--override-username=homelab",
            "--override-hostname=homelab-media",
        ],
    ) in calls
    assert any(
        container == "kopia-agent"
        and command[:4] == ["kopia", "snapshot", "create", "/source"]
        and "--tags=node:media" in command
        for container, command in calls
    )


def test_infra_snapshot_uses_direct_repository_without_agent_connect(tmp_path: Path, monkeypatch) -> None:
    _env(tmp_path, "infra")
    monkeypatch.setattr(
        "toolkit.core.ops.logical_backups.prepare_logical_dumps",
        lambda *_args: LogicalDumpResult(True),
    )
    calls: list[tuple[str, list[str]]] = []

    def fake_exec(container: str, command: list[str], **_kwargs):
        calls.append((container, command))
        return 0, "ok"

    monkeypatch.setattr("toolkit.core.ops.backups.docker_exec", fake_exec)

    result = run_node_snapshot(tmp_path, "infra")

    assert result.ok
    assert all("connect" not in command for _container, command in calls)
    assert any(
        container == "kopia" and command[:4] == ["kopia", "snapshot", "create", "/source"]
        for container, command in calls
    )


def test_snapshot_failure_is_returned_with_bounded_detail(tmp_path: Path, monkeypatch) -> None:
    _env(tmp_path, "apps")
    monkeypatch.setattr(
        "toolkit.core.ops.logical_backups.prepare_logical_dumps",
        lambda *_args: LogicalDumpResult(True),
    )

    def fake_exec(_container: str, command: list[str], **_kwargs):
        if command[:3] == ["kopia", "repository", "status"]:
            return 0, "connected"
        if command[:3] == ["kopia", "policy", "set"]:
            return 0, "ok"
        return 1, "failed upload " * 100

    monkeypatch.setattr("toolkit.core.ops.backups.docker_exec", fake_exec)

    result = run_node_snapshot(tmp_path, "apps")

    assert not result.ok
    assert "failed upload" in result.message
    assert len(result.message) < 300


def test_snapshot_stops_when_consistent_database_export_fails(tmp_path: Path, monkeypatch) -> None:
    _env(tmp_path, "infra")
    save_config(Config(backups={"enabled": True}), config_path(tmp_path))
    calls: list[list[str]] = []
    monkeypatch.setattr(
        "toolkit.core.ops.logical_backups.prepare_logical_dumps",
        lambda *_args: LogicalDumpResult(False, errors=("postgres unavailable",)),
    )
    monkeypatch.setattr(
        "toolkit.core.ops.backups.docker_exec",
        lambda _container, command, **_kwargs: calls.append(command) or (0, "ok"),
    )

    result = run_node_snapshot(tmp_path, "infra")

    assert not result.ok
    assert "postgres unavailable" in result.message
    assert calls == []


def test_cli_snapshot_streams_progress_and_result(tmp_path: Path, monkeypatch) -> None:
    cfg = Config(backups={"enabled": True})
    monkeypatch.setenv("HOMELAB_NODE", "media")
    with (
        patch("toolkit.cli.maintenance_cmd.load_root_config", return_value=(tmp_path, cfg)),
        patch(
            "toolkit.core.ops.backups.run_node_snapshot",
            return_value=BackupResult(True, "media", "media snapshot completed", ("Repository verified",)),
        ),
    ):
        result = CliRunner().invoke(maintenance, ["snapshot", "--node", "media"])

    assert result.exit_code == 0, result.output
    assert "verifying repository connection" in result.output
    assert "Repository verified" in result.output
    assert "media snapshot completed" in result.output
