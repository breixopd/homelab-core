from __future__ import annotations

import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

WAZUH_BOOTSTRAP = importlib.import_module("toolkit.services.wazuh-indexer.bootstrap")


def test_ensure_wazuh_skips_when_container_missing(tmp_path: Path):
    with patch.object(WAZUH_BOOTSTRAP.subprocess, "run") as run:
        run.return_value.stdout = ""
        run.return_value.returncode = 0
        logs = WAZUH_BOOTSTRAP.ensure_wazuh_indexer_healthy(tmp_path)
    assert any("skip heal" in line for line in logs)


def test_wazuh_authenticated_probe_keeps_password_out_of_process_arguments(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "toolkit.core.secrets.secrets.load_secrets_plaintext",
        lambda _path: {"WAZUH_INDEXER_PASSWORD": "wazuh-test-password"},
    )
    calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def fake_curl(*args: object, **kwargs: object) -> tuple[int, str]:
        calls.append((args, kwargs))
        return 0, '{"status":"green"}'

    monkeypatch.setattr(WAZUH_BOOTSTRAP, "docker_curl", fake_curl)

    assert WAZUH_BOOTSTRAP._indexer_auth_ok(tmp_path, docker_bin="docker-probe")
    assert calls[0][0] == ("wazuh-indexer", "https://localhost:9200/_cluster/health")
    assert "wazuh-test-password" not in " ".join(str(value) for value in calls[0][0])
    assert calls[0][1]["docker_bin"] == "docker-probe"
    assert calls[0][1]["insecure_tls"] is True


def test_ensure_wazuh_reconciles_security_without_deleting_indexer_data(tmp_path: Path):
    data = tmp_path / "data" / "wazuh" / "indexer"
    data.mkdir(parents=True)
    marker = data / "must-survive"
    marker.write_text("index state")

    def fake_run(command, **_kwargs):
        output = ""
        if "{{.State.Status}}" in command:
            output = "running"
        elif "{{.State.Health.Status}}" in command:
            output = "unhealthy"
        return SimpleNamespace(returncode=0, stdout=output, stderr="")

    with (
        patch.object(WAZUH_BOOTSTRAP.subprocess, "run", side_effect=fake_run),
        patch.object(WAZUH_BOOTSTRAP, "_wait_indexer_http", return_value=True),
        patch.object(WAZUH_BOOTSTRAP, "_indexer_auth_ok", side_effect=[False, True]),
        patch.object(WAZUH_BOOTSTRAP, "_run_securityadmin", return_value="Wazuh: securityadmin applied internal_users"),
    ):
        logs = WAZUH_BOOTSTRAP.ensure_wazuh_indexer_healthy(tmp_path)

    assert marker.read_text() == "index state"
    assert logs == [
        "Wazuh: securityadmin applied internal_users",
        "Wazuh: credentials reconciled; indexer state preserved",
    ]


def test_ensure_wazuh_preserves_state_when_restart_loop_needs_operator(tmp_path: Path) -> None:
    data = tmp_path / "data" / "wazuh" / "indexer"
    data.mkdir(parents=True)
    marker = data / "must-survive"
    marker.write_text("index state")

    with patch.object(WAZUH_BOOTSTRAP.subprocess, "run") as run:
        run.return_value = SimpleNamespace(returncode=0, stdout="restarting", stderr="")
        logs = WAZUH_BOOTSTRAP.ensure_wazuh_indexer_healthy(tmp_path)

    assert marker.read_text() == "index state"
    assert logs == ["Wazuh: wazuh-indexer is restarting; persisted index data preserved; operator repair required"]
