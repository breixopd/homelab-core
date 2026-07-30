from __future__ import annotations

from pathlib import Path
from unittest.mock import Mock

from toolkit.core.config.config import Config
from toolkit.services.ntfy.client import resolve_infra_ntfy_url
from toolkit.services.sdk.vaultwarden import vaultwarden_url


def test_ntfy_controller_url_uses_the_manifest_service_owner(monkeypatch) -> None:
    monkeypatch.setattr(
        "toolkit.core.manifest.placement.service_address",
        lambda _cfg, service: "192.0.2.41" if service == "ntfy" else "",
    )

    assert resolve_infra_ntfy_url(Config()) == "http://192.0.2.41:8090"


def test_vaultwarden_url_uses_the_manifest_service_owner(monkeypatch) -> None:
    monkeypatch.setattr(
        "toolkit.core.manifest.placement.service_address",
        lambda _cfg, service: "192.0.2.42" if service == "vaultwarden" else "",
    )

    assert vaultwarden_url(Config()) == "http://192.0.2.42:8082"


def test_tdarr_operator_link_uses_the_manifest_service_owner(monkeypatch) -> None:
    from toolkit.services.tdarr import bootstrap as tdarr_automation

    monkeypatch.setattr(tdarr_automation, "wait_for_tdarr", lambda *_args, **_kwargs: False)
    cruddb = Mock()
    monkeypatch.setattr(tdarr_automation, "_cruddb", cruddb)
    monkeypatch.setattr(
        "toolkit.core.manifest.placement.service_address",
        lambda _cfg, service: "192.0.2.43" if service == "tdarr" else "",
    )

    logs = tdarr_automation.configure_tdarr(Config(), install_root=Path("/opt/homelab"))

    assert "Tdarr UI (VPN/LAN): http://192.0.2.43:8265" in logs
    cruddb.assert_not_called()
