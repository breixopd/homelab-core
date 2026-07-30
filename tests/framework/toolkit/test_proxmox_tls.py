from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest
from toolkit.core.config.config import Config
from toolkit.core.infra import proxmox_tls
from toolkit.core.infra.proxmox_tls import ensure_proxmox_ca_bundle


def _system_ca() -> Path:
    return proxmox_tls._system_ca_bundle()


def test_system_ca_bundle_uses_platform_fallback_when_ssl_reports_no_bundle(tmp_path: Path, monkeypatch) -> None:
    fallback_bundle = tmp_path / "ca-bundle.pem"
    fallback_bundle.write_bytes(_system_ca().read_bytes())
    monkeypatch.setattr(
        proxmox_tls.ssl,
        "get_default_verify_paths",
        lambda: SimpleNamespace(cafile=None, openssl_cafile=None, openssl_cafile_env="SSL_CERT_FILE"),
    )
    monkeypatch.setattr(proxmox_tls, "_SYSTEM_CA_BUNDLE_PATHS", (fallback_bundle,))

    assert proxmox_tls._system_ca_bundle() == fallback_bundle


def test_proxmox_ca_bundle_combines_system_and_ssh_fetched_ca(tmp_path: Path, monkeypatch):
    private_ca = _system_ca().read_text(encoding="utf-8").split("-----END CERTIFICATE-----", 1)[0]
    private_ca += "-----END CERTIFICATE-----\n"
    key_file = tmp_path / "pve-key"
    key_file.write_text("test key")
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=private_ca, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cfg = Config(
        proxmox={
            "api_url": "https://192.0.2.10:8006",
            "control_host": "pve-admin.example.test",
            "ssh": {"user": "operator", "port": 2222, "key_file": str(key_file)},
        },
        ssh={"key_file": "/keys/guest"},
    )

    bundle = ensure_proxmox_ca_bundle(tmp_path, cfg)

    assert bundle == tmp_path / ".homelab-state" / "trust" / "proxmox-ca-bundle.pem"
    assert bundle.is_file()
    assert bundle.stat().st_mode & 0o777 == 0o600
    assert private_ca in bundle.read_text(encoding="utf-8")
    assert commands == [
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={tmp_path / 'automation' / 'ansible' / 'inventory' / 'known_hosts'}",
            "-o",
            "ConnectTimeout=30",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=4",
            "-p",
            "2222",
            "-i",
            str(key_file),
            "operator@pve-admin.example.test",
            "cat /etc/pve/pve-root-ca.pem",
        ]
    ]


def test_proxmox_ca_bundle_uses_configured_ca_without_ssh(tmp_path: Path, monkeypatch):
    configured = tmp_path / "operator-ca.pem"
    configured.write_bytes(_system_ca().read_bytes())
    monkeypatch.setattr(subprocess, "run", lambda *_args, **_kwargs: pytest.fail("SSH must not run"))
    cfg = Config(
        proxmox={
            "api_url": "https://pve.example.test:8006",
            "tls_ca_file": str(configured),
        }
    )

    bundle = ensure_proxmox_ca_bundle(tmp_path, cfg)

    assert bundle is not None
    assert configured.read_text(encoding="utf-8") in bundle.read_text(encoding="utf-8")


def test_proxmox_ca_bundle_uses_controller_automation_key(tmp_path: Path, monkeypatch):
    private_ca = _system_ca().read_text(encoding="utf-8").split("-----END CERTIFICATE-----", 1)[0]
    private_ca += "-----END CERTIFICATE-----\n"
    automation_key = tmp_path / "ssh" / "homelab_admin_ed25519"
    automation_key.parent.mkdir()
    automation_key.write_text("test key")
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        return subprocess.CompletedProcess(command, 0, stdout=private_ca, stderr="")

    monkeypatch.setenv("HOMELAB_NODE", "infra")
    monkeypatch.setattr(subprocess, "run", fake_run)
    cfg = Config(
        proxmox={
            "api_url": "https://192.0.2.10:8006",
            "control_host": "pve-admin.example.test",
            "ssh": {"user": "operator", "port": 2222},
        }
    )

    bundle = ensure_proxmox_ca_bundle(tmp_path, cfg)

    assert bundle is not None
    assert commands[0][commands[0].index("-i") + 1] == str(automation_key.resolve())


def test_proxmox_ca_bundle_fails_closed_when_ca_fetch_fails(tmp_path: Path, monkeypatch):
    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 255, stdout="", stderr="host key verification failed")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cfg = Config(proxmox={"api_url": "https://pve.example.test:8006"})

    with pytest.raises(RuntimeError, match="could not retrieve the Proxmox CA"):
        ensure_proxmox_ca_bundle(tmp_path, cfg)


def test_proxmox_ca_bundle_keeps_valid_cache_during_transient_ssh_failure(tmp_path: Path, monkeypatch):
    cached = tmp_path / ".homelab-state" / "trust" / "proxmox-ca.pem"
    cached.parent.mkdir(parents=True)
    cached.write_bytes(_system_ca().read_bytes())

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 255, stdout="", stderr="connection refused")

    monkeypatch.setattr(subprocess, "run", fake_run)
    cfg = Config(proxmox={"api_url": "https://pve.example.test:8006"})

    bundle = ensure_proxmox_ca_bundle(tmp_path, cfg)

    assert bundle is not None
    assert bundle.is_file()
