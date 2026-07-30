from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError
from toolkit.controller.desired_state_api import (
    DesiredStateConflictError,
    read_settings_view,
    update_settings,
)
from toolkit.controller.read_models import SettingsUpdate, SettingsValues, SettingsView
from toolkit.core.config.config import Config, load_config, save_config, save_local_config
from toolkit.core.config.storage import config_path


def test_settings_view_is_allowlisted_and_never_contains_local_credentials(tmp_path: Path) -> None:
    cfg = Config(
        domain="example.test",
        owner_password="owner-password-canary",
        ssh={"password": "ssh-password-canary"},
        proxmox={
            "api_url": "https://pve.example.test:8006",
            "control_host": "pve-admin.example.test",
            "ssh": {"user": "operator", "port": 2222, "key_file": "/keys/pve"},
            "lxc_storage": "guest-zfs",
            "lxc_template_datastore": "templates",
            "lxc_template_url": "https://images.example.test/debian.tar.zst",
            "lxc_template_checksum": "b" * 64,
            "tls_ca_file": "/etc/homelab/pve-ca.pem",
        },
    )
    save_config(cfg, config_path(tmp_path))
    save_local_config(cfg, tmp_path)

    view = read_settings_view(tmp_path)
    serialized = view.model_dump_json()

    assert view.values.domain == "example.test"
    assert view.values.proxmox_control_host == "pve-admin.example.test"
    assert view.values.proxmox_ssh_user == "operator"
    assert view.values.proxmox_ssh_port == 2222
    assert view.values.proxmox_ssh_key_file == "/keys/pve"
    assert view.values.proxmox_template_datastore == "templates"
    assert view.values.proxmox_template_url == "https://images.example.test/debian.tar.zst"
    assert view.values.proxmox_template_checksum == "b" * 64
    assert view.values.proxmox_tls_ca_file == "/etc/homelab/pve-ca.pem"
    assert view.values.container_ipv4_cidr == "172.31.0.0/17"
    assert view.values.container_network_prefix == 28
    assert "owner-password-canary" not in serialized
    assert "ssh-password-canary" not in serialized
    assert "service_exposure" not in SettingsValues.model_fields
    assert "exposure_services" not in SettingsView.model_fields
    assert "always_public_services" not in SettingsView.model_fields
    for plugin_owned_field in (
        "media_server",
        "hw_transcode",
        "media_vpn",
        "media_tdarr",
        "tdarr_cpu_workers",
        "qbittorrent_port",
        "vpn_server_countries",
        "media_cache",
        "music_sync",
        "music_sync_interval",
        "cache_cold_after",
        "cache_uplink_mbps",
    ):
        assert plugin_owned_field not in type(view.values).model_fields
    assert view.service_toggles == ["security", "media", "cloud", "notifications", "email"]
    assert set(view.values.services) == set(view.service_toggles)


def test_global_settings_update_preserves_plugin_owned_service_configuration(tmp_path: Path) -> None:
    save_config(
        Config(
            domain="example.test",
            service_settings={
                "media-cache": {"enabled": False, "cold-after-days": 47, "uplink-mbps": 810},
                "music-sync": {"enabled": False, "interval-minutes": 75},
            },
        ),
        config_path(tmp_path),
    )
    view = read_settings_view(tmp_path)

    update_settings(
        tmp_path,
        SettingsUpdate(
            expected_revision=view.revision,
            values=view.values.model_copy(update={"timezone": "Europe/Madrid"}),
        ),
    )

    settings = load_config(config_path(tmp_path)).service_settings
    assert settings["media-cache"] == {"enabled": False, "cold-after-days": 47, "uplink-mbps": 810}
    assert settings["music-sync"] == {"enabled": False, "interval-minutes": 75}


def test_settings_update_is_revision_guarded(tmp_path: Path) -> None:
    save_config(Config(domain="example.test"), config_path(tmp_path))
    view = read_settings_view(tmp_path)
    changed = view.values.model_copy(update={"timezone": "Europe/Madrid"})

    updated = update_settings(
        tmp_path,
        SettingsUpdate(expected_revision=view.revision, values=changed),
    )

    assert updated.values.timezone == "Europe/Madrid"
    with pytest.raises(DesiredStateConflictError):
        update_settings(
            tmp_path,
            SettingsUpdate(expected_revision=view.revision, values=view.values),
        )


def test_settings_update_persists_complete_proxmox_provider_contract(tmp_path: Path) -> None:
    save_config(Config(domain="example.test"), config_path(tmp_path))
    view = read_settings_view(tmp_path)
    values = view.values.model_copy(
        update={
            "proxmox_control_host": "pve-admin.example.test",
            "proxmox_ssh_user": "operator",
            "proxmox_ssh_port": 2222,
            "proxmox_ssh_key_file": "/keys/pve",
            "proxmox_ssh_connect_timeout": 19,
            "proxmox_ssh_command_timeout": 181,
            "proxmox_ssh_retries": 4,
            "proxmox_template_datastore": "templates",
            "proxmox_template_url": "https://images.example.test/debian.tar.zst",
            "proxmox_template_checksum": "c" * 64,
            "proxmox_tls_ca_file": "/etc/homelab/pve-ca.pem",
        }
    )

    update_settings(tmp_path, SettingsUpdate(expected_revision=view.revision, values=values))

    proxmox = load_config(config_path(tmp_path)).proxmox
    assert proxmox.control_host == "pve-admin.example.test"
    assert proxmox.ssh.user == "operator"
    assert proxmox.ssh.port == 2222
    assert proxmox.ssh.key_file == "/keys/pve"
    assert proxmox.ssh.connect_timeout == 19
    assert proxmox.ssh.command_timeout == 181
    assert proxmox.ssh.retries == 4
    assert proxmox.lxc_template_datastore == "templates"
    assert proxmox.lxc_template_url == "https://images.example.test/debian.tar.zst"
    assert proxmox.lxc_template_checksum == "c" * 64
    assert proxmox.tls_ca_file == "/etc/homelab/pve-ca.pem"


def test_settings_update_persists_container_network_pool(tmp_path: Path) -> None:
    save_config(Config(domain="example.test"), config_path(tmp_path))
    view = read_settings_view(tmp_path)
    values = view.values.model_copy(update={"container_ipv4_cidr": "172.29.0.0/17", "container_network_prefix": 27})

    updated = update_settings(tmp_path, SettingsUpdate(expected_revision=view.revision, values=values))

    assert updated.values.container_ipv4_cidr == "172.29.0.0/17"
    assert updated.values.container_network_prefix == 27
    network = load_config(config_path(tmp_path)).network
    assert network.container_ipv4_cidr == "172.29.0.0/17"
    assert network.container_network_prefix == 27


def test_stale_service_exposure_configuration_is_rejected(tmp_path: Path) -> None:
    config_path(tmp_path).write_text(
        "domain: example.test\nnetwork:\n  service_exposure:\n    grafana: public\n",
        encoding="utf-8",
    )

    with pytest.raises(ValidationError, match="service_exposure"):
        load_config(config_path(tmp_path))
