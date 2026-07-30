from __future__ import annotations

from pathlib import Path

import pytest
from toolkit.controller.desired_state_api import (
    DesiredStateConflictError,
    config_revision,
    read_dns_view,
    update_dns_public_ip,
)
from toolkit.controller.read_models import DnsIpUpdate
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.mutations import ConfigurationBusyError
from toolkit.core.config.storage import config_path
from toolkit.core.deploy.operation_lease import OperationLease


def _configured_root(tmp_path: Path) -> Path:
    save_config(
        Config(
            domain="example.test",
            dns={"public_ip": "1.2.3.4"},
            network={"expose_via_internet": True},
        ),
        config_path(tmp_path),
    )
    (tmp_path / "secrets.enc.yaml").write_text("placeholder\n")
    return tmp_path


def test_dns_view_exposes_credential_presence_only(monkeypatch, tmp_path: Path) -> None:
    root = _configured_root(tmp_path)
    canary = "cloudflare-token-canary"
    monkeypatch.setattr(
        "toolkit.controller.desired_state_api.load_secrets_plaintext",
        lambda _path: {"CLOUDFLARE_API_TOKEN": canary, "CLOUDFLARE_ZONE_ID": "zone-canary"},
    )

    view = read_dns_view(root)

    assert view.has_cloudflare_credentials is True
    assert view.public_ip == "1.2.3.4"
    assert canary not in view.model_dump_json()
    assert len(view.revision) == 64


def test_dns_ip_update_rejects_stale_revision_without_writing(tmp_path: Path) -> None:
    root = _configured_root(tmp_path)
    original = config_path(root).read_text()

    with pytest.raises(DesiredStateConflictError):
        update_dns_public_ip(
            root,
            DnsIpUpdate(expected_revision="a" * 64, public_ip="5.6.7.8"),
        )

    assert config_path(root).read_text() == original


def test_dns_ip_update_returns_new_revision(monkeypatch, tmp_path: Path) -> None:
    root = _configured_root(tmp_path)
    monkeypatch.setattr("toolkit.controller.desired_state_api.load_secrets_plaintext", lambda _path: {})
    before = config_revision(root)

    view = update_dns_public_ip(
        root,
        DnsIpUpdate(expected_revision=before, public_ip="5.6.7.8"),
    )

    assert view.public_ip == "5.6.7.8"
    assert view.revision != before


def test_dns_mutation_is_rejected_while_operation_lease_is_held(tmp_path: Path) -> None:
    root = _configured_root(tmp_path)
    before = config_revision(root)
    lease = OperationLease.acquire(root, "deploy")
    try:
        with pytest.raises(ConfigurationBusyError):
            update_dns_public_ip(root, DnsIpUpdate(expected_revision=before, public_ip="5.6.7.8"))
    finally:
        lease.release()
    assert config_revision(root) == before
