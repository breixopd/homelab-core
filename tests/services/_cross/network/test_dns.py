import httpx
import pytest
from toolkit.core.manifest.routes import route_fqdn
from toolkit.core.ops.dns import (
    HOMELAB_DNS_COMMENT,
    DNSRecord,
    desired_records_from_config,
    email_dns_records,
    mark_managed_record,
    resolve_public_dns_ip,
    verify_dns_propagation,
)


def test_verify_dns_propagation_requires_origin_for_unproxied(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_args, **_kwargs: [(0, 0, 0, "", ("1.2.3.4", 0))],
    )
    assert verify_dns_propagation("app.example.com", "1.2.3.4", max_retries=1, interval=0)
    assert not verify_dns_propagation("app.example.com", "5.6.7.8", max_retries=1, interval=0)


def test_verify_dns_propagation_accepts_global_cloudflare_ip_for_proxied(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_args, **_kwargs: [(0, 0, 0, "", ("104.16.1.1", 0))],
    )
    assert verify_dns_propagation("app.example.com", "10.0.0.5", max_retries=1, interval=0, proxied=True)


def test_verify_dns_propagation_rejects_private_or_malformed_proxied_answers(monkeypatch):
    monkeypatch.setattr(
        "socket.getaddrinfo",
        lambda *_args, **_kwargs: [
            (0, 0, 0, "", ("127.0.0.1", 0)),
            (0, 0, 0, "", ("not-an-ip", 0)),
            (0, 0, 0, "", ("2001:db8::1", 0)),
        ],
    )
    assert not verify_dns_propagation("app.example.com", "10.0.0.5", max_retries=1, interval=0, proxied=True)


def test_dns_record_creation():
    r = DNSRecord(name="test.example.com", type="A", content="1.2.3.4", proxied=True)
    assert r.name == "test.example.com"
    assert r.proxied is True


def test_external_hosts_dns_records():
    from toolkit.core.config.config import Config, ExternalHost
    from toolkit.core.ops.dns import external_host_fqdn, external_hosts_dns_records

    cfg = Config(
        domain="example.com",
        external_hosts=[ExternalHost(name="NAS-01", ip="203.0.113.10")],
    )
    records = external_hosts_dns_records(cfg)
    assert len(records) == 1
    assert records[0].name == external_host_fqdn("NAS-01", "example.com")
    assert records[0].content == "203.0.113.10"
    assert records[0].proxied is False


def test_desired_records_includes_external_hosts():
    from toolkit.core.config.config import Config, DNSConfig, ExternalHost, ServicesConfig
    from toolkit.core.ops.dns import desired_records_from_config

    cfg = Config(
        domain="example.com",
        dns=DNSConfig(proxy_enabled=True),
        services=ServicesConfig(
            media=False,
            cloud=False,
            notifications=False,
            email=False,
            security=False,
        ),
        external_hosts=[ExternalHost(name="edge", ip="198.51.100.2")],
    )
    records = desired_records_from_config(cfg, "1.2.3.4")
    ext = [r for r in records if r.content == "198.51.100.2"]
    assert len(ext) == 1
    assert ext[0].name == "edge.example.com"


def test_route_fqdn_apex_and_subdomain():
    assert route_fqdn("", "example.com") == "example.com"
    assert route_fqdn("auth", "example.com") == "auth.example.com"


def test_desired_records_includes_portal_apex():
    from toolkit.core.config.config import Config, DNSConfig, ServicesConfig

    cfg = Config(
        domain="example.com",
        dns=DNSConfig(proxy_enabled=True),
        services=ServicesConfig(
            management=True,
            media=False,
            cloud=False,
            notifications=False,
            email=False,
            security=False,
        ),
    )
    names = {r.name for r in desired_records_from_config(cfg, "1.2.3.4")}
    assert "example.com" in names
    assert ".example.com" not in names


def test_desired_records_from_config():
    from toolkit.core.config.config import Config, DNSConfig, NetworkConfig, ServicesConfig

    cfg = Config(
        domain="example.com",
        dns=DNSConfig(proxy_enabled=True),
        network=NetworkConfig(dns_public_access=False),
        services=ServicesConfig(
            management=True,
            media=False,
            cloud=False,
            notifications=False,
            email=False,
            security=False,
        ),
    )
    records = desired_records_from_config(cfg, "1.2.3.4")
    assert len(records) > 0
    assert all(r.content == "1.2.3.4" for r in records)
    assert all(r.name.endswith("example.com") for r in records)
    assert all(r.proxied is True for r in records if r.type == "A")
    assert all(HOMELAB_DNS_COMMENT in r.comment for r in records)


def test_desired_records_use_manifest_exposure():
    from toolkit.core.config.config import Config, DNSConfig, ServicesConfig

    cfg = Config(
        domain="example.com",
        dns=DNSConfig(proxy_enabled=True),
        services=ServicesConfig(
            management=True,
            media=False,
            cloud=False,
            notifications=False,
            email=False,
            security=False,
        ),
    )

    names = {r.name for r in desired_records_from_config(cfg, "1.2.3.4")}

    assert "auth.example.com" in names
    assert "homelab.example.com" in names
    assert "grafana.example.com" not in names


def test_desired_records_dns_only_when_proxy_disabled():
    from toolkit.core.config.config import Config, DNSConfig, ServicesConfig

    cfg = Config(
        domain="example.com",
        dns=DNSConfig(proxy_enabled=False),
        services=ServicesConfig(
            management=True,
            media=False,
            cloud=False,
            notifications=False,
            email=False,
            security=False,
        ),
    )
    records = desired_records_from_config(cfg, "1.2.3.4")
    assert all(r.proxied is False for r in records if r.type == "A")


def test_email_mail_record_overrides_public_route_proxy():
    """SMTP/IMAP must remain DNS-only even though the webmail route is proxied."""
    from toolkit.core.config.config import Config, DNSConfig, ServicesConfig

    cfg = Config(
        domain="example.com",
        dns=DNSConfig(proxy_enabled=True),
        services=ServicesConfig(
            management=True,
            media=False,
            cloud=False,
            notifications=False,
            email=True,
            security=False,
        ),
    )

    mail_records = [
        record
        for record in desired_records_from_config(cfg, "1.2.3.4")
        if record.name == "mail.example.com" and record.type == "A"
    ]

    assert len(mail_records) == 1
    assert mail_records[0].proxied is False
    autoconfig_records = [
        record for record in desired_records_from_config(cfg, "1.2.3.4") if record.name == "autoconfig.example.com"
    ]
    assert [(record.type, record.content) for record in autoconfig_records] == [("CNAME", "mail.example.com")]


def test_dns_sync_mocked():
    """DNS sync should create/update records via Cloudflare API."""
    from unittest.mock import patch

    from toolkit.core.ops.dns import CloudflareDNS

    client = CloudflareDNS(api_token="test-token", zone_id="zone123")

    with patch.object(client, "_request") as mock_req:
        mock_req.side_effect = [
            {"result": []},  # A records
            {"result": []},  # AAAA records
            {"result": []},  # CNAME records
            {"result": []},  # MX records
            {"result": []},  # TXT records
            {"result": {"id": "new1"}},  # create record 1
            {"result": {"id": "new2"}},  # create record 2
        ]

        desired = [
            DNSRecord(name="app.example.com", type="A", content="1.2.3.4", proxied=True),
            DNSRecord(name="cdn.example.com", type="A", content="1.2.3.4", proxied=True),
        ]

        stats = client.sync_records(desired)
        assert stats["created"] == 2
        assert stats["unchanged"] == 0
        assert stats["updated"] == 0
        created_payload = mock_req.call_args_list[-2].args[2]
        assert created_payload["comment"] == HOMELAB_DNS_COMMENT


def test_cleanup_only_deletes_marked_stale_records(tmp_path, monkeypatch):
    from toolkit.core.config.config import Config, DNSConfig, NetworkConfig, ServicesConfig, save_config
    from toolkit.core.config.storage import config_path
    from toolkit.core.ops import dns as dns_mod

    cfg = Config(
        domain="example.com",
        dns=DNSConfig(public_ip="1.2.3.4"),
        network=NetworkConfig(),
        services=ServicesConfig(
            management=True,
            media=False,
            cloud=False,
            notifications=False,
            email=False,
            security=False,
        ),
    )
    save_config(cfg, config_path(tmp_path))
    monkeypatch.setattr(
        "toolkit.core.secrets.secrets.load_secrets_plaintext",
        lambda _path: {"CLOUDFLARE_API_TOKEN": "token", "CLOUDFLARE_ZONE_ID": "zone"},
    )

    class FakeCloudflare:
        deleted: list[str] = []

        def __init__(self, api_token: str, zone_id: str = ""):
            self._zone_id = zone_id

        def list_records(self, record_type: str = "A"):
            if record_type != "A":
                return []
            return [
                mark_managed_record(DNSRecord(name="stale.example.com", type="A", content="1.2.3.4", record_id="old")),
                DNSRecord(name="custom.example.com", type="A", content="1.2.3.4", record_id="custom"),
                mark_managed_record(
                    DNSRecord(name="homelab.example.com", type="A", content="1.2.3.4", record_id="current")
                ),
            ]

        def list_all_managed_records(self):
            return self.list_records("A")

        def delete_record(self, record_id: str) -> None:
            self.deleted.append(record_id)

    monkeypatch.setattr(dns_mod, "CloudflareDNS", FakeCloudflare)

    deleted = dns_mod.cleanup_stale_homelab_dns(tmp_path)

    assert deleted == 1
    assert FakeCloudflare.deleted == ["old"]


def test_full_dns_sync_prunes_stale_managed_records(tmp_path, monkeypatch):
    from toolkit.core.config.config import Config, DNSConfig
    from toolkit.core.ops import dns as dns_mod

    cfg = Config(domain="example.com", dns=DNSConfig(public_ip="1.2.3.4", proxy_enabled=False))

    class FakeClient:
        def get_zone_setting(self, _setting):
            return "full"

        def sync_records(self, desired):
            assert desired
            return {"created": 1, "updated": 0, "unchanged": 0}

    client = FakeClient()
    monkeypatch.setattr(dns_mod, "cloudflare_client_from_root", lambda _root: (cfg, client))
    monkeypatch.setattr(dns_mod, "_cleanup_stale_homelab_dns_records", lambda *_args: 2)
    monkeypatch.setattr(dns_mod, "prune_leaked_private_cloudflare_records", lambda *_args: 1)

    result = dns_mod.sync_cloudflare_dns(tmp_path)

    assert result == {"created": 1, "updated": 0, "unchanged": 0, "pruned": 3}


def test_stale_cleanup_preserves_active_dkim_when_key_discovery_is_unavailable(monkeypatch):
    from toolkit.core.config.config import Config, ServicesConfig
    from toolkit.core.ops import dns as dns_mod

    cfg = Config(domain="example.com", services=ServicesConfig(email=True))

    class FakeClient:
        deleted: list[str] = []

        def list_all_managed_records(self):
            return [
                mark_managed_record(
                    DNSRecord(
                        name="mail._domainkey.example.com",
                        type="TXT",
                        content="v=DKIM1; p=current",
                        record_id="active",
                    )
                ),
                mark_managed_record(
                    DNSRecord(
                        name="default._domainkey.example.com",
                        type="TXT",
                        content="v=DKIM1; p=old",
                        record_id="old",
                    )
                ),
            ]

        def delete_record(self, record_id):
            self.deleted.append(record_id)

    monkeypatch.setattr(dns_mod, "desired_records_from_config", lambda _cfg, _ip: [])
    client = FakeClient()

    deleted = dns_mod._cleanup_stale_homelab_dns_records(cfg, client, lambda _message: None)

    assert deleted == 1
    assert client.deleted == ["old"]


def test_adguard_mesh_rewrites_include_private_service(monkeypatch):
    from toolkit.core.config.config import Config, ServicesConfig
    from toolkit.core.ops.dns import AdGuardDNS

    cfg = Config(
        domain="example.com",
        services=ServicesConfig(
            management=True,
            media=False,
            cloud=False,
            notifications=False,
            email=False,
            security=False,
        ),
    )
    client = AdGuardDNS(base_url="http://adguard:3000", password="x")
    captured = {}

    def fake_rewrite_stats(desired, **_kwargs):
        captured.update(desired)
        return {"created": 0, "updated": 0, "unchanged": len(desired), "removed": 0}

    monkeypatch.setattr(client, "_rewrite_stats", fake_rewrite_stats)

    client.sync_mesh_service_rewrites(cfg, "10.10.10.10")

    assert captured["grafana.example.com"] == "10.10.10.10"
    assert "auth.example.com" not in captured
    assert "homelab.example.com" not in captured


def test_adguard_internal_dns_uses_manifest_aliases_and_service_owners(monkeypatch):
    from tests.helpers.machines import renamed_default_machines
    from toolkit.core.config.config import Config
    from toolkit.core.ops.dns import AdGuardDNS

    cfg = Config(domain="example.com", machines=renamed_default_machines())
    client = AdGuardDNS(base_url="http://adguard:3000", password="x")
    captured: dict[str, str] = {}

    def fake_rewrite_stats(desired, **_kwargs):
        captured.update(desired)
        return {"created": len(desired), "updated": 0, "unchanged": 0, "removed": 0}

    monkeypatch.setattr(client, "_rewrite_stats", fake_rewrite_stats)

    stats = client.sync_internal_dns(cfg)

    assert stats["created"] == len(captured)
    assert captured["postgres.internal.example.com"] == "10.10.10.10"
    assert captured["auth.internal.example.com"] == "10.10.10.10"
    assert captured["loki.internal.example.com"] == "10.10.10.10"


def test_adguard_service_rewrite_pruning_preserves_other_dns_namespaces(monkeypatch):
    from toolkit.core.config.config import Config, ServicesConfig
    from toolkit.core.ops.dns import AdGuardDNS

    cfg = Config(
        domain="example.com",
        services=ServicesConfig(
            management=True,
            media=False,
            cloud=False,
            notifications=False,
            email=False,
            security=False,
        ),
    )
    client = AdGuardDNS(base_url="http://adguard:3000", password="x")
    monkeypatch.setattr(
        client,
        "list_rewrites",
        lambda: [
            {"domain": "auth.example.com", "answer": "10.10.10.10"},
            {"domain": "nas.example.com", "answer": "10.10.10.20"},
            {"domain": "phone.mesh.example.com", "answer": "100.64.0.2"},
        ],
    )
    monkeypatch.setattr(client, "add_rewrite", lambda *_args: None)
    deleted: list[str] = []
    monkeypatch.setattr(client, "delete_rewrite", lambda domain, _answer: deleted.append(domain))

    client.sync_mesh_service_rewrites(cfg, "10.10.10.10")

    assert "auth.example.com" in deleted
    assert "nas.example.com" not in deleted
    assert "phone.mesh.example.com" not in deleted


def test_adguard_remove_host_rewrite():
    from unittest.mock import patch

    from toolkit.core.ops.dns import AdGuardDNS

    client = AdGuardDNS(base_url="http://adguard:3000", password="x")
    with patch.object(
        client,
        "list_rewrites",
        return_value=[
            {"domain": "nas-01.example.com", "answer": "10.0.0.9"},
            {"domain": "other.example.com", "answer": "10.0.0.2"},
        ],
    ):
        with patch.object(client, "delete_rewrite") as deleted:
            removed = client.remove_host_rewrite("nas-01.example.com")
    assert removed == 1
    deleted.assert_called_once_with("nas-01.example.com", "10.0.0.9")


def test_adguard_rewrite_sync_fails_closed_when_inventory_read_fails(monkeypatch):
    from toolkit.core.ops.dns import AdGuardDNS

    client = AdGuardDNS(base_url="http://adguard:3000", password="x")
    monkeypatch.setattr(client, "list_rewrites", lambda: (_ for _ in ()).throw(httpx.ReadTimeout("timeout")))
    added: list[tuple[str, str]] = []
    deleted: list[tuple[str, str]] = []
    monkeypatch.setattr(client, "add_rewrite", lambda domain, answer: added.append((domain, answer)))
    monkeypatch.setattr(client, "delete_rewrite", lambda domain, answer: deleted.append((domain, answer)))

    with pytest.raises(httpx.ReadTimeout):
        client._rewrite_stats({"grafana.example.com": "10.10.10.10"})

    assert added == []
    assert deleted == []


def test_adguard_external_host_sync_fails_closed_when_inventory_shape_is_invalid(monkeypatch):
    from toolkit.core.config.config import Config, ExternalHost
    from toolkit.core.ops.dns import AdGuardDNS

    cfg = Config(
        domain="example.com",
        external_hosts=[ExternalHost(name="nas-01", ip="10.0.0.9")],
    )
    client = AdGuardDNS(base_url="http://adguard:3000", password="x")
    monkeypatch.setattr(client, "_request", lambda *_args, **_kwargs: {"unexpected": "object"})
    added: list[tuple[str, str]] = []
    monkeypatch.setattr(client, "add_rewrite", lambda domain, answer: added.append((domain, answer)))

    with pytest.raises(RuntimeError, match="unexpected response"):
        client.sync_external_hosts_rewrites(cfg)

    assert added == []


def test_prune_leaked_private_cloudflare_records():
    from unittest.mock import MagicMock

    from toolkit.core.config.config import Config, DNSConfig, ServicesConfig
    from toolkit.core.ops.dns import DNSRecord, prune_leaked_private_cloudflare_records

    cfg = Config(
        domain="example.com",
        dns=DNSConfig(public_ip="1.2.3.4"),
        services=ServicesConfig(
            management=True, media=False, cloud=False, notifications=False, email=False, security=False
        ),
    )
    client = MagicMock()
    client.list_records.return_value = [
        DNSRecord(name="grafana.example.com", type="A", content="1.2.3.4", record_id="leak1"),
        DNSRecord(name="auth.example.com", type="A", content="1.2.3.4", record_id="keep1"),
    ]
    deleted = prune_leaked_private_cloudflare_records(cfg, client, "1.2.3.4")
    assert deleted == 1
    client.delete_record.assert_called_once_with("leak1")


def test_remove_external_host_dns_deletes_cloudflare_and_adguard(tmp_path, monkeypatch):
    from unittest.mock import MagicMock

    from toolkit.core.config.config import Config, ExternalHost, save_config
    from toolkit.core.config.storage import config_path
    from toolkit.core.ops.dns import DNSRecord, external_host_fqdn, remove_external_host_dns

    cfg = Config(
        domain="example.com",
        external_hosts=[ExternalHost(name="nas-01", ip="10.0.0.9")],
    )
    save_config(cfg, config_path(tmp_path))
    fqdn = external_host_fqdn("nas-01", "example.com")

    cf_client = MagicMock()
    cf_client.list_records.return_value = [
        DNSRecord(name=fqdn, type="A", content="10.0.0.9", record_id="rec1"),
        DNSRecord(name="keep.example.com", type="A", content="1.2.3.4", record_id="rec2"),
    ]
    monkeypatch.setattr("toolkit.core.ops.dns.cloudflare_client_from_root", lambda root: (cfg, cf_client))

    ag_client = MagicMock()
    ag_client.remove_host_rewrite.return_value = 1
    monkeypatch.setattr("toolkit.core.ops.dns.adguard_client_from_root", lambda root, **kw: ag_client)

    stats = remove_external_host_dns(tmp_path, "nas-01")
    assert stats["cloudflare_deleted"] == 1
    assert stats["adguard_deleted"] == 1
    cf_client.delete_record.assert_called_once_with("rec1")
    ag_client.remove_host_rewrite.assert_called_once_with(fqdn)


def test_remove_external_host_dns_tolerates_failures(tmp_path, monkeypatch):
    from toolkit.core.config.config import Config, ExternalHost, save_config
    from toolkit.core.config.storage import config_path
    from toolkit.core.ops.dns import remove_external_host_dns

    cfg = Config(
        domain="example.com",
        external_hosts=[ExternalHost(name="nas-01", ip="10.0.0.9")],
    )
    save_config(cfg, config_path(tmp_path))

    def boom(*a, **k):
        raise ValueError("no token")

    monkeypatch.setattr("toolkit.core.ops.dns.cloudflare_client_from_root", boom)
    monkeypatch.setattr("toolkit.core.ops.dns.adguard_client_from_root", boom)

    stats = remove_external_host_dns(tmp_path, "nas-01")
    assert stats == {"cloudflare_deleted": 0, "adguard_deleted": 0}


def test_email_dns_records():
    records = email_dns_records("example.com", "1.2.3.4")
    types = [r.type for r in records]
    assert "MX" in types
    assert "TXT" in types
    names = [r.name for r in records]
    assert "mail.example.com" in names
    assert "_dmarc.example.com" in names


def test_email_dns_records_are_not_published_when_public_mail_is_disabled():
    assert email_dns_records("example.com", "1.2.3.4", mail_public_access=False) == []


def test_email_dns_records_only_publish_autoconfig_with_a_real_endpoint():
    records = email_dns_records("example.com", "1.2.3.4", autoconfig_enabled=False)
    names = {record.name for record in records}

    assert "autoconfig.example.com" not in names
    assert "autodiscover.example.com" not in names


def test_resolve_public_dns_ip_prefers_explicit_config():
    from toolkit.core.config.config import Config, DNSConfig, ProxmoxConfig

    cfg = Config(
        dns=DNSConfig(public_ip="1.2.3.4"),
        proxmox=ProxmoxConfig(api_url="https://10.0.0.10:8006"),
    )

    public_ip, source = resolve_public_dns_ip(cfg)

    assert public_ip == "1.2.3.4"
    assert source == "config"


def test_resolve_public_dns_ip_uses_override_when_valid():
    from toolkit.core.config.config import Config

    public_ip, source = resolve_public_dns_ip(Config(), "5.6.7.8")

    assert public_ip == "5.6.7.8"
    assert source == "override"


def test_resolve_public_dns_ip_uses_autodetect_before_proxmox_url(monkeypatch):
    from toolkit.core.config.config import Config, ProxmoxConfig

    monkeypatch.setattr("toolkit.core.infra.autodetect.detect_public_ip", lambda: "8.8.4.4")

    cfg = Config(proxmox=ProxmoxConfig(api_url="https://10.20.30.40:8006"))

    public_ip, source = resolve_public_dns_ip(cfg)

    assert public_ip == "8.8.4.4"
    assert source == "autodetect"


def test_resolve_public_dns_ip_falls_back_to_proxmox_ipv4_only(monkeypatch):
    from toolkit.core.config.config import Config, ProxmoxConfig

    monkeypatch.setattr("toolkit.core.infra.autodetect.detect_public_ip", lambda: "")
    cfg = Config(proxmox=ProxmoxConfig(api_url="https://10.20.30.40:8006"))

    public_ip, source = resolve_public_dns_ip(cfg)

    assert public_ip == "10.20.30.40"
    assert source == "proxmox-url"


def test_resolve_public_dns_ip_rejects_hostname_fallback(monkeypatch):
    from toolkit.core.config.config import Config, ProxmoxConfig

    monkeypatch.setattr("toolkit.core.infra.autodetect.detect_public_ip", lambda: "")
    cfg = Config(proxmox=ProxmoxConfig(api_url="https://pve.internal.example:8006"))

    public_ip, source = resolve_public_dns_ip(cfg)

    assert public_ip == ""
    assert source == "missing"
