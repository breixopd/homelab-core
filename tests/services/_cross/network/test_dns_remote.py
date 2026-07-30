from toolkit.core.config.config import Config, DNSConfig, NetworkConfig, ServicesConfig
from toolkit.core.ops.dns import (
    desired_records_from_config,
    dns_public_a_record,
    dns_public_access_enabled,
    dns_resolver_fqdn,
    private_route_fqdns,
)


def test_dns_public_access_follows_explicit_network_policy():
    off = Config(network=NetworkConfig(dns_public_access=False))
    assert not dns_public_access_enabled(off)
    on = Config(network=NetworkConfig(dns_public_access=True))
    assert dns_public_access_enabled(on)


def test_dns_public_a_record_unproxied():
    cfg = Config(
        domain="example.com",
        network=NetworkConfig(dns_public_access=True),
        services=ServicesConfig(management=True),
    )
    record = dns_public_a_record(cfg, "203.0.113.1")
    assert record is not None
    assert record.name == dns_resolver_fqdn(cfg) == "dns.example.com"
    assert record.content == "203.0.113.1"
    assert record.proxied is False


def test_desired_records_includes_public_dns_when_enabled():
    cfg = Config(
        domain="example.com",
        dns=DNSConfig(proxy_enabled=True),
        network=NetworkConfig(dns_public_access=True),
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
    dns_records = [r for r in records if r.name == "dns.example.com"]
    assert len(dns_records) == 1
    assert dns_records[0].proxied is False
    assert dns_records[0].content == "1.2.3.4"


def test_private_cloudflare_exceptions_allows_public_dns():
    from toolkit.core.config.config import Config, NetworkConfig, ServicesConfig
    from toolkit.core.ops.dns import dns_resolver_fqdn, private_cloudflare_exceptions, private_route_fqdns

    cfg = Config(
        domain="example.com",
        network=NetworkConfig(dns_public_access=True),
        services=ServicesConfig(management=True),
    )
    private = private_route_fqdns(cfg)
    assert dns_resolver_fqdn(cfg) in private
    assert dns_resolver_fqdn(cfg) in private_cloudflare_exceptions(cfg)


def test_private_cloudflare_exceptions_allows_mail():
    from toolkit.core.config.config import Config, ServicesConfig
    from toolkit.core.ops.dns import private_cloudflare_exceptions

    cfg = Config(domain="example.com", services=ServicesConfig(email=True))
    assert "mail.example.com" in private_cloudflare_exceptions(cfg)


def test_private_route_fqdns_excludes_public_routes():
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
    private = private_route_fqdns(cfg)
    assert "grafana.example.com" in private
    assert "homelab.example.com" not in private
    assert "auth.example.com" not in private
