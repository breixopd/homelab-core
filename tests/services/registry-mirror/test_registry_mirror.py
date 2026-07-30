from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import patch

registry_mirror = importlib.import_module("toolkit.services.registry-mirror.bootstrap")
ensure_registry_mirror = registry_mirror.ensure_registry_mirror
purge_registry_mirror_cache = registry_mirror.purge_registry_mirror_cache


def test_purge_registry_mirror_cache_stops_running_mirror():
    with (
        patch.object(registry_mirror, "_mirror_running", return_value=True),
        patch.object(registry_mirror, "_run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        logs = purge_registry_mirror_cache()
    assert any("purged" in line.lower() for line in logs)
    assert mock_run.call_count >= 2


def test_ensure_registry_mirror_skips_when_healthy(tmp_path: Path):
    root = tmp_path / "homelab"
    (root / "generated" / "infra").mkdir(parents=True)
    (root / "generated" / "infra" / ".env").write_text("PRIVATE_IP=127.0.0.1\n")
    with (
        patch.object(registry_mirror, "_mirror_running", return_value=True),
        patch.object(registry_mirror, "_mirror_http_ok", return_value=True),
    ):
        logs = ensure_registry_mirror(root)
    assert logs == ["Registry mirror: already running"]


def test_purge_registry_mirror_cache_skips_stop_when_not_running():
    with (
        patch.object(registry_mirror, "_mirror_running", return_value=False),
        patch.object(registry_mirror, "_run") as mock_run,
    ):
        mock_run.return_value.returncode = 0
        logs = purge_registry_mirror_cache()
    assert any("purged" in line.lower() for line in logs)
    stop_calls = [c for c in mock_run.call_args_list if c[0][0][:2] == ["docker", "stop"]]
    assert stop_calls == []


def test_purge_registry_mirror_cache_reports_failure():
    with (
        patch.object(registry_mirror, "_mirror_running", return_value=False),
        patch.object(registry_mirror, "_run") as mock_run,
    ):
        mock_run.return_value.returncode = 1
        mock_run.return_value.stderr = "volume busy"
        logs = purge_registry_mirror_cache()
    assert any("purge failed" in line.lower() for line in logs)


def test_ensure_registry_mirror_missing_env(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    with (
        patch.object(registry_mirror, "_mirror_running", return_value=False),
        patch.object(registry_mirror, "_mirror_http_ok", return_value=False),
    ):
        logs = ensure_registry_mirror(root)
    assert any("missing" in line.lower() for line in logs)


def test_ensure_registry_mirror_start_failure(tmp_path: Path):
    root = tmp_path / "homelab"
    (root / "generated" / "infra").mkdir(parents=True)
    (root / "generated" / "infra" / ".env").write_text("PRIVATE_IP=127.0.0.1\n")
    (root / "docker-compose.yml").write_text("name: homelab\n")

    failed = type("Proc", (), {"returncode": 1, "stdout": "", "stderr": "compose error"})()
    with (
        patch.object(registry_mirror, "_mirror_running", return_value=False),
        patch.object(registry_mirror, "_mirror_http_ok", return_value=False),
        patch.object(registry_mirror, "_run", return_value=failed),
    ):
        logs = ensure_registry_mirror(root)
    assert any("start failed" in line.lower() for line in logs)


def test_ensure_registry_mirror_waits_for_http_probe(tmp_path: Path):
    root = tmp_path / "homelab"
    (root / "generated" / "infra").mkdir(parents=True)
    (root / "generated" / "infra" / ".env").write_text("PRIVATE_IP=127.0.0.1\n")
    (root / "docker-compose.yml").write_text("name: homelab\n")

    ok = type("Proc", (), {"returncode": 0, "stdout": "", "stderr": ""})()
    http_states = iter([False, True])

    with (
        patch.object(registry_mirror, "_mirror_running", return_value=False),
        patch.object(registry_mirror, "_mirror_http_ok", side_effect=lambda **_: next(http_states)),
        patch.object(registry_mirror, "_run", return_value=ok),
        patch.object(registry_mirror.time, "sleep"),
    ):
        logs = ensure_registry_mirror(root)
    assert any("running on port 3128" in line for line in logs)


def test_ensure_registry_mirror_with_purge_cache(tmp_path: Path):
    root = tmp_path / "homelab"
    (root / "generated" / "infra").mkdir(parents=True)
    (root / "generated" / "infra" / ".env").write_text("PRIVATE_IP=127.0.0.1\n")
    with (
        patch.object(registry_mirror, "purge_registry_mirror_cache", return_value=["purged"]),
        patch.object(registry_mirror, "_mirror_running", return_value=True),
        patch.object(registry_mirror, "_mirror_http_ok", return_value=True),
    ):
        logs = ensure_registry_mirror(root, purge_cache=True)
    assert logs[0] == "purged"
    assert logs[-1] == "Registry mirror: already running"
