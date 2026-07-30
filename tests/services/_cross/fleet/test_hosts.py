from __future__ import annotations

from pathlib import Path

import pytest
from toolkit.core.config.config import Config, ExternalHost, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.infra.hosts import (
    add_host,
    managed_host_fingerprint,
    mark_host_reconciled,
    reconcile_host_integrations,
    remove_host,
)


class TestExternalHostValidation:
    def test_valid_host(self):
        ExternalHost(name="nas-01", ip="10.0.0.5", ssh_user="admin", ssh_port=22)

    def test_valid_host_with_dashes_underscores(self):
        ExternalHost(
            name="nas_01-backup",
            ip="192.168.1.100",
            ssh_user="backup_user",
            ssh_port=2222,
        )

    def test_invalid_ipv4_format(self):
        with pytest.raises(ValueError, match="Invalid IPv4 address"):
            ExternalHost(name="bad", ip="not.an.ip.address", ssh_user="root", ssh_port=22)

    def test_invalid_ipv4_octet_over_255(self):
        with pytest.raises(ValueError, match="Invalid IPv4 address"):
            ExternalHost(name="bad", ip="10.0.0.999", ssh_user="root", ssh_port=22)

    def test_invalid_ipv4_out_of_range(self):
        with pytest.raises(ValueError, match="Invalid IPv4 address"):
            ExternalHost(name="bad", ip="256.256.256.256", ssh_user="root", ssh_port=22)

    def test_invalid_ssh_port_zero(self):
        # Pydantic validates the port range at construction time.
        with pytest.raises(ValueError, match="Port must be 1-65535"):
            ExternalHost(name="bad", ip="10.0.0.5", ssh_user="root", ssh_port=0)

    def test_invalid_ssh_port_too_high(self):
        with pytest.raises(ValueError, match="Port must be 1-65535"):
            ExternalHost(name="bad", ip="10.0.0.5", ssh_user="root", ssh_port=70000)

    def test_invalid_ssh_user_starts_with_number(self):
        with pytest.raises(ValueError, match="Invalid SSH user"):
            ExternalHost(name="bad", ip="10.0.0.5", ssh_user="123root", ssh_port=22)

    def test_invalid_ssh_user_uppercase(self):
        with pytest.raises(ValueError, match="Invalid SSH user"):
            ExternalHost(name="bad", ip="10.0.0.5", ssh_user="Root", ssh_port=22)

    def test_invalid_ssh_user_special_chars(self):
        with pytest.raises(ValueError, match="Invalid SSH user"):
            ExternalHost(name="bad", ip="10.0.0.5", ssh_user="root@host", ssh_port=22)

    def test_invalid_name_with_spaces(self):
        with pytest.raises(ValueError, match="Invalid host name"):
            ExternalHost(name="bad host", ip="10.0.0.5", ssh_user="root", ssh_port=22)

    def test_invalid_name_with_special_chars(self):
        with pytest.raises(ValueError, match="Invalid host name"):
            ExternalHost(name="bad@host!", ip="10.0.0.5", ssh_user="root", ssh_port=22)

    def test_valid_ssh_user_with_underscore(self):
        ExternalHost(name="good", ip="10.0.0.5", ssh_user="_service", ssh_port=22)

    def test_valid_ssh_user_single_char(self):
        ExternalHost(name="good", ip="10.0.0.5", ssh_user="a", ssh_port=22)

    def test_max_valid_port(self):
        ExternalHost(name="good", ip="10.0.0.5", ssh_user="root", ssh_port=65535)

    def test_min_valid_port(self):
        ExternalHost(name="good", ip="10.0.0.5", ssh_user="root", ssh_port=1)

    def test_unknown_service_rejected(self):
        with pytest.raises(ValueError, match="Unknown plain host service"):
            ExternalHost(name="good", ip="10.0.0.5", services=["backup-agent"])

    def test_known_services_accepted(self):
        ExternalHost(
            name="good",
            ip="10.0.0.5",
            services=["monitoring-agent", "wazuh-agent", "vpn-client", "dns-client"],
        )

    def test_fleet_only_service_rejected_for_plain_host(self):
        with pytest.raises(ValueError, match="Unknown plain host service"):
            ExternalHost(name="good", ip="10.0.0.5", services=["ldap-client"])

    def test_fleet_only_service_accepted_for_fleet_host(self):
        ExternalHost(name="good", ip="10.0.0.5", kind="fleet", services=["ldap-client"])

    def test_media_cache_requires_declared_field(self):
        with pytest.raises(ValueError, match="media-cache.path is required"):
            ExternalHost(name="good", ip="10.0.0.5", services=["media-cache"])

    def test_media_cache_with_declared_field_ok(self):
        host = ExternalHost(
            name="good",
            ip="10.0.0.5",
            services=["media-cache"],
            integrations={"media-cache": {"path": "/srv/cache"}},
        )
        assert host.integration_value("media-cache", "path") == "/srv/cache"

    def test_host_rejects_settings_for_unselected_integration(self):
        with pytest.raises(ValueError, match="not selected"):
            ExternalHost(
                name="good",
                ip="10.0.0.5",
                integrations={"media-cache": {"path": "/srv/cache"}},
            )

    def test_host_rejects_undeclared_integration_field(self):
        with pytest.raises(ValueError, match="Unknown media-cache integration field"):
            ExternalHost(
                name="good",
                ip="10.0.0.5",
                services=["media-cache"],
                integrations={"media-cache": {"mount": "/srv/cache"}},
            )

    def test_host_rejects_unsafe_integration_path(self):
        with pytest.raises(ValueError, match="absolute path"):
            ExternalHost(
                name="good",
                ip="10.0.0.5",
                services=["media-cache"],
                integrations={"media-cache": {"path": "../cache"}},
            )

    def test_host_rejects_oversized_integration_value(self):
        with pytest.raises(ValueError, match="4096"):
            ExternalHost(
                name="good",
                ip="10.0.0.5",
                services=["media-cache"],
                integrations={"media-cache": {"path": "/" + "a" * 4_096}},
            )

    def test_plain_host_rejects_fleet_metadata(self):
        with pytest.raises(ValueError, match="Fleet enrollment metadata"):
            ExternalHost(name="good", ip="10.0.0.5", cluster_group="edge")


def _setup_config(root: Path) -> None:
    cfg = Config(domain="example.com", email="admin@example.com")
    save_config(cfg, config_path(root))


class TestRemoveHostCleanup:
    def test_remove_unknown_host_returns_false(self, tmp_path: Path):
        _setup_config(tmp_path)
        assert remove_host(tmp_path, "nope") is False

    def test_remove_backup_host_resets_backup_target(self, tmp_path: Path, monkeypatch):
        _setup_config(tmp_path)
        add_host(
            tmp_path,
            "backup-01",
            "10.0.0.8",
            services=["backup-storage"],
            integrations={"backup-storage": {"path": "/srv/backups"}},
        )
        monkeypatch.setattr("toolkit.core.infra.hosts.cleanup_host_resources", lambda *_args, **_kwargs: [])

        assert remove_host(tmp_path, "backup-01") is True

        from toolkit.core.config.config import load_config

        cfg = load_config(config_path(tmp_path))
        assert cfg.backups.target == "local"
        assert cfg.backups.storage_host == ""

    def test_add_host_rejects_duplicate_name(self, tmp_path: Path):
        _setup_config(tmp_path)
        add_host(tmp_path, "edge", "10.0.0.7")

        with pytest.raises(ValueError, match="already exists"):
            add_host(tmp_path, "edge", "10.0.0.8")

    def test_add_host_preserves_explicit_empty_services(self, tmp_path: Path):
        _setup_config(tmp_path)

        host = add_host(tmp_path, "storage", "10.0.0.7", services=[])

        assert host.services == []

    def test_reconciliation_status_rejects_stale_host_snapshot(self, tmp_path: Path):
        _setup_config(tmp_path)
        host = add_host(tmp_path, "edge", "10.0.0.7")
        fingerprint = managed_host_fingerprint(host)
        from toolkit.core.config.config import load_config

        cfg = load_config(config_path(tmp_path))
        cfg.external_hosts[0] = cfg.external_hosts[0].model_copy(update={"ip": "10.0.0.8"})
        save_config(cfg, config_path(tmp_path))

        assert mark_host_reconciled(tmp_path, "edge", fingerprint) is False
        assert load_config(config_path(tmp_path)).external_hosts[0].reconciled is False

    def test_reconciliation_records_the_applied_service_set(self, tmp_path: Path):
        _setup_config(tmp_path)
        host = add_host(tmp_path, "edge", "10.0.0.7", services=["monitoring-agent"])

        assert mark_host_reconciled(tmp_path, "edge", managed_host_fingerprint(host)) is True

        from toolkit.core.config.config import load_config

        applied = load_config(config_path(tmp_path)).external_hosts[0]
        assert applied.reconciled is True
        assert applied.applied_services == ["monitoring-agent"]

    def test_reconcile_pins_remote_backup_host_key(self, tmp_path: Path, monkeypatch):
        _setup_config(tmp_path)
        host = add_host(
            tmp_path,
            "backup-01",
            "10.0.0.8",
            services=["backup-storage"],
            integrations={"backup-storage": {"path": "/srv/kopia"}},
        )
        known_hosts = tmp_path / "automation" / "ansible" / "inventory" / "known_hosts"
        known_hosts.parent.mkdir(parents=True)
        known_hosts.write_text("10.0.0.8 ssh-ed25519 AAAAbackup\n", encoding="utf-8")
        monkeypatch.setattr("toolkit.core.ops.dns.sync_external_hosts_dns", lambda *_args, **_kwargs: {})
        result = reconcile_host_integrations(tmp_path, host)

        assert (tmp_path / "config" / "kopia" / "known_hosts").read_text() == known_hosts.read_text()
        assert result.ok is True
        assert any("pinned SFTP" in message for message in result.logs)

    def test_remove_host_cleans_dns(self, tmp_path: Path, monkeypatch):
        _setup_config(tmp_path)
        add_host(
            tmp_path,
            "nas-01",
            "10.0.0.9",
            services=["monitoring-agent"],
        )

        dns_calls: list[str] = []
        monkeypatch.setattr(
            "toolkit.core.ops.dns.remove_external_host_dns",
            lambda root, name, **kw: dns_calls.append(name),
        )

        assert remove_host(tmp_path, "nas-01") is True

        from toolkit.core.config.config import load_config

        assert load_config(config_path(tmp_path)).external_hosts == []
        assert dns_calls == ["nas-01"]

    def test_remove_host_survives_cleanup_errors(self, tmp_path: Path, monkeypatch):
        _setup_config(tmp_path)
        add_host(tmp_path, "edge", "10.0.0.4", services=["monitoring-agent"])

        def boom(*a, **k):
            raise RuntimeError("network down")

        monkeypatch.setattr("toolkit.core.ops.dns.remove_external_host_dns", boom)
        assert remove_host(tmp_path, "edge") is True
        from toolkit.core.config.config import load_config

        assert load_config(config_path(tmp_path)).external_hosts == []
