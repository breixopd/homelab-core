from __future__ import annotations

from pathlib import Path

import pytest
from toolkit.controller.desired_state_api import DesiredStateConflictError
from toolkit.controller.managed_hosts_api import (
    create_managed_host,
    read_managed_hosts_view,
    remove_managed_host,
    update_managed_host,
)
from toolkit.controller.read_models import ManagedHostCreate, ManagedHostSpec, ManagedHostUpdate
from toolkit.core.config.config import Config, ExternalHost, load_config, save_config
from toolkit.core.config.storage import config_path


def _setup(root: Path, *, hosts: list[ExternalHost] | None = None) -> None:
    save_config(Config(domain="example.test", external_hosts=hosts or []), config_path(root))


def _spec(**updates) -> ManagedHostSpec:
    values = {
        "name": "edge-01",
        "ip": "192.0.2.20",
        "kind": "fleet",
        "ssh_user": "root",
        "ssh_port": 22,
        "cluster_group": "edge",
        "lldap_email": "ops@example.test",
        "headscale_tags": ["tag:edge"],
        "services": ["monitoring-agent", "vpn-client", "ldap-client"],
        "integrations": {},
    }
    values.update(updates)
    return ManagedHostSpec(**values)


def test_managed_hosts_view_is_revisioned_and_catalog_driven(tmp_path: Path) -> None:
    _setup(
        tmp_path,
        hosts=[ExternalHost(name="nas-01", ip="192.0.2.10", services=["monitoring-agent"])],
    )

    view = read_managed_hosts_view(tmp_path)

    assert len(view.revision) == 64
    assert view.hosts[0].name == "nas-01"
    assert len(view.hosts[0].fingerprint) == 64
    ldap = next(choice for choice in view.service_choices if choice.name == "ldap-client")
    assert ldap.fleet_only is True
    assert ldap.default_for_plain is False


def test_managed_host_create_and_update_are_revision_guarded(tmp_path: Path) -> None:
    _setup(tmp_path)
    initial = read_managed_hosts_view(tmp_path)

    created = create_managed_host(
        tmp_path,
        ManagedHostCreate(expected_revision=initial.revision, host=_spec()),
    )

    assert created.hosts[0].kind == "fleet"
    assert created.hosts[0].reconciled is False
    assert created.revision != initial.revision
    with pytest.raises(DesiredStateConflictError):
        update_managed_host(
            tmp_path,
            "edge-01",
            ManagedHostUpdate(expected_revision=initial.revision, host=_spec(ip="192.0.2.21")),
        )

    updated = update_managed_host(
        tmp_path,
        "edge-01",
        ManagedHostUpdate(
            expected_revision=created.revision,
            host=_spec(
                ip="192.0.2.21",
                services=["monitoring-agent", "backup-storage"],
                integrations={"backup-storage": {"path": "/srv/backups"}},
            ),
        ),
    )

    assert updated.hosts[0].ip == "192.0.2.21"
    cfg = load_config(config_path(tmp_path))
    assert cfg.backups.enabled is True
    assert cfg.backups.target == "remote"
    assert cfg.backups.storage_host == "edge-01"


def test_managed_host_remove_uses_entity_fingerprint_and_resets_backup_target(
    tmp_path: Path,
    monkeypatch,
) -> None:
    host = ExternalHost(
        name="backup-01",
        ip="192.0.2.30",
        kind="fleet",
        services=["backup-storage"],
        applied_services=["backup-storage"],
        integrations={"backup-storage": {"path": "/srv/backups"}},
        reconciled=True,
        last_reconcile_at="2026-07-11T01:00:00+00:00",
    )
    cfg = Config(
        domain="example.test",
        external_hosts=[host],
        backups={"enabled": True, "target": "remote", "storage_host": "backup-01"},
    )
    save_config(cfg, config_path(tmp_path))
    current = read_managed_hosts_view(tmp_path).hosts[0]
    cleaned: list[str] = []
    monkeypatch.setattr(
        "toolkit.controller.managed_hosts_api.cleanup_host_resources",
        lambda _root, removed, on_log=None: cleaned.append(removed.name) or ["cleanup complete"],
    )

    logs = remove_managed_host(tmp_path, "backup-01", current.fingerprint)

    assert logs == ["cleanup complete"]
    assert cleaned == ["backup-01"]
    result = load_config(config_path(tmp_path))
    assert result.external_hosts == []
    assert result.backups.target == "local"
    assert result.backups.storage_host == ""


def test_managed_host_remove_rejects_changed_entity_before_cleanup(tmp_path: Path, monkeypatch) -> None:
    _setup(tmp_path, hosts=[ExternalHost(name="edge-01", ip="192.0.2.20")])
    original = read_managed_hosts_view(tmp_path).hosts[0]
    cfg = load_config(config_path(tmp_path))
    cfg.external_hosts[0] = cfg.external_hosts[0].model_copy(update={"ip": "192.0.2.21"})
    save_config(cfg, config_path(tmp_path))
    cleanup = monkeypatch.setattr(
        "toolkit.controller.managed_hosts_api.cleanup_host_resources",
        lambda *_args, **_kwargs: pytest.fail("cleanup must not run for stale state"),
    )

    with pytest.raises(DesiredStateConflictError):
        remove_managed_host(tmp_path, "edge-01", original.fingerprint)

    assert cleanup is None
