"""Unit tests for kopia plugin verify()."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import BackupsConfig, Config, ServicesConfig
from toolkit.core.machines import MachineSpec
from toolkit.core.verify.models import VerifyStatus


def _plugin():
    module = load_plugin("kopia")
    for name in dir(module):
        if not name.endswith("Plugin") or name == "ServicePlugin":
            continue
        obj = getattr(module, name)
        if isinstance(obj, type):
            return obj()
    raise RuntimeError("no kopia plugin")


def test_snapshot_runtimes_have_only_the_read_capability_required_for_private_sources() -> None:
    compose = yaml.safe_load(
        (Path(__file__).parents[3] / "toolkit/services/kopia/compose.yaml").read_text(encoding="utf-8")
    )

    for service in ("kopia", "kopia-agent"):
        runtime = compose["services"][service]
        assert runtime["cap_drop"] == ["ALL"]
        assert runtime["cap_add"] == ["DAC_READ_SEARCH"]
        assert runtime["read_only"] is True
        assert runtime.get("privileged") is not True
    assert (
        "${KOPIA_AGENT_LOGS_SOURCE:-./data/kopia-agent/logs}:/app/logs" in compose["services"]["kopia-agent"]["volumes"]
    )


def test_post_start_bootstraps_once_and_rejects_failed_repository(tmp_path):
    cfg = Config(
        domain="example.com",
        services=ServicesConfig(management=True),
        backups=BackupsConfig(enabled=True),
    )
    with patch(
        "toolkit.services.kopia.bootstrap.bootstrap_kopia_repository",
        return_value=["Kopia: repository connect failed"],
    ) as bootstrap:
        with pytest.raises(RuntimeError, match="repository connect failed"):
            _plugin().post_start(cfg, {"KOPIA_REPOSITORY_PASSWORD": "secret"}, root=tmp_path)

    bootstrap.assert_called_once()


class TestKopiaVerify:
    def test_skips_when_backups_disabled(self, tmp_path):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))
        cfg.backups = BackupsConfig(enabled=False)
        checks = _plugin().verify(cfg, {}, "10.10.10.10", tmp_path)
        assert checks[0].passed
        assert checks[0].status is VerifyStatus.NOT_APPLICABLE
        assert "disabled" in checks[0].detail

    def test_multi_vm_snapshots_recent(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))
        cfg.backups = BackupsConfig(enabled=True)
        recent = time.time() - 3600
        snap_json = json.dumps(
            [
                {"startTime": recent, "id": role, "source": {"host": f"homelab-{role}"}}
                for role in ("infra", "media", "apps")
            ]
        )

        def fake_ssh(_cfg, _ip, cmd, root=None, timeout=30):
            if "repository status" in cmd:
                return 0, "connected to filesystem repository", ""
            if "snapshot list" in cmd:
                return 0, snap_json, ""
            return 1, "", ""

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.ssh_on_vm", fake_ssh)

        checks = {c.check: c for c in _plugin().verify(cfg, {}, "10.10.10.10", tmp_path)}
        assert checks["repository"].passed
        assert all(checks[f"snapshot-{role}"].passed for role in ("infra", "media", "apps"))

    def test_single_host_uses_container_state_not_unauthenticated_http(self, tmp_path, monkeypatch):
        cfg = Config(
            domain="example.com",
            services=ServicesConfig(management=True, media=False, cloud=False, email=False),
            backups=BackupsConfig(enabled=True),
            machines={
                "core": MachineSpec(
                    hostname="core-01",
                    address="10.10.10.10",
                    gateway="10.10.10.1",
                    vmid=810,
                    labels=("control",),
                )
            },
        )
        recent = time.time() - 3600
        snapshot = json.dumps([{"startTime": recent, "source": {"host": "homelab-core"}}])

        def fake_exec(_container, command, **_kwargs):
            return (0, "connected to filesystem repository") if "status" in command else (0, snapshot)

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.core.ops.automation.docker_exec", fake_exec)

        checks = {c.check: c for c in _plugin().verify(cfg, {}, "127.0.0.1", tmp_path)}

        assert checks["repository"].passed
        assert checks["snapshot-core"].passed

    def test_stale_snapshots_fail(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))
        cfg.backups = BackupsConfig(enabled=True)
        old = time.time() - (48 * 3600)
        snap_json = json.dumps(
            [{"startTime": old, "source": {"host": f"homelab-{role}"}} for role in ("infra", "media", "apps")]
        )

        def fake_ssh(_cfg, _ip, cmd, root=None, timeout=30):
            if "repository status" in cmd:
                return 0, "connected to filesystem repository", ""
            if "snapshot list" in cmd:
                return 0, snap_json, ""
            return 1, "", ""

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.ssh_on_vm", fake_ssh)

        checks = {c.check: c for c in _plugin().verify(cfg, {}, "10.10.10.10", tmp_path)}
        assert all(checks[f"snapshot-{role}"].passed is False for role in ("infra", "media", "apps"))

    def test_missing_node_snapshot_fails_only_that_node(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(management=True))
        cfg.backups = BackupsConfig(enabled=True)
        recent = time.time() - 3600
        snap_json = json.dumps(
            [{"startTime": recent, "source": {"host": f"homelab-{role}"}} for role in ("infra", "media")]
        )

        def fake_ssh(_cfg, _ip, cmd, root=None, timeout=30):
            return (0, "connected to filesystem repository", "") if "repository status" in cmd else (0, snap_json, "")

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.ssh_on_vm", fake_ssh)

        checks = {c.check: c for c in _plugin().verify(cfg, {}, "10.10.10.10", tmp_path)}
        assert checks["snapshot-infra"].passed
        assert checks["snapshot-media"].passed
        assert not checks["snapshot-apps"].passed
        assert "missing" in checks["snapshot-apps"].detail

    def test_remote_target_rejects_connected_local_repository(self, tmp_path, monkeypatch):
        host = {
            "name": "nas",
            "ip": "10.10.10.20",
            "services": ["backup-storage"],
            "integrations": {"backup-storage": {"path": "/srv/kopia"}},
        }
        cfg = Config(
            domain="example.com",
            backups={"enabled": True, "target": "remote", "storage_host": "nas"},
            external_hosts=[host],
        )

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "toolkit.services.sdk.ssh_on_vm",
            lambda *_a, **_k: (0, "Connected to filesystem repository", ""),
        )

        checks = _plugin().verify(cfg, {}, "10.10.10.10", tmp_path)

        assert not checks[0].passed
        assert "SFTP" in checks[0].detail
