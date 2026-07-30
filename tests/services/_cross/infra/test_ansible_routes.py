from __future__ import annotations

from toolkit.core.ansible.ansible_routes import build_route_group_vars
from toolkit.core.config.config import Config, ServicesConfig


def test_ansible_routes_are_exact_manifest_projections() -> None:
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

    routes = build_route_group_vars(cfg)

    assert routes["published_subdomains"] == {"infra-01": ["auth", "homelab"]}
    assert routes["private_subdomains"] == {"infra-01": ["dns", "grafana", "komodo", "prometheus", "users"]}
    assert routes["expected_urls"] == {
        "infra-01": ["https://auth.example.com", "https://example.com", "https://homelab.example.com"]
    }
    assert all("internal" not in key for key in routes)
