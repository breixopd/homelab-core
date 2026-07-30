from __future__ import annotations

from click.testing import CliRunner
from toolkit.cli import main
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path


def test_snapshot_dispatches_to_the_selected_managed_node(tmp_path, monkeypatch) -> None:
    cfg = Config(backups={"enabled": True})
    save_config(cfg, config_path(tmp_path))
    calls: list[tuple[str, str, int]] = []

    def fake_ssh(_cfg, ip, command, *, root, timeout):
        assert root == tmp_path
        calls.append((ip, command, timeout))
        return 0, "Snapshot [infra] complete", ""

    monkeypatch.delenv("HOMELAB_NODE", raising=False)
    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)

    result = CliRunner().invoke(
        main,
        ["--root", str(tmp_path), "maintenance", "snapshot", "--node", "infra"],
    )

    assert result.exit_code == 0, result.output
    assert "Snapshot [infra] complete" in result.output
    assert calls == [
        (
            cfg.node_ip("infra"),
            "env HOMELAB_NODE=infra /opt/homelab/.venv/bin/python3 -m toolkit.cli "
            "--root /opt/homelab maintenance snapshot --node infra",
            3600,
        )
    ]


def test_snapshot_executes_locally_on_the_selected_node(tmp_path, monkeypatch) -> None:
    cfg = Config(backups={"enabled": True})
    save_config(cfg, config_path(tmp_path))
    calls: list[str] = []

    class Result:
        ok = True
        actions = ("Repository connected",)
        message = "Snapshot [infra] complete"

    monkeypatch.setenv("HOMELAB_NODE", "infra")
    monkeypatch.setattr(
        "toolkit.core.ops.backups.run_node_snapshot",
        lambda _root, role, **_kwargs: calls.append(role) or Result(),
    )

    result = CliRunner().invoke(
        main,
        ["--root", str(tmp_path), "maintenance", "snapshot", "--node", "infra"],
    )

    assert result.exit_code == 0, result.output
    assert calls == ["infra"]
    assert "Repository connected" in result.output
