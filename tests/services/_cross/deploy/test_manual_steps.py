from __future__ import annotations

from toolkit.core.config.config import Config, DNSConfig, ProxmoxConfig, ServicesConfig
from toolkit.core.manifest.catalog import ServiceCatalog
from toolkit.core.manifest.schema import ServiceManifest
from toolkit.core.ops.manual_steps import (
    format_manual_steps_cli,
    get_all_manual_guidance,
    get_manual_steps,
    get_prerequisite_steps,
)


def test_manual_steps_empty_when_no_hooks_needed():
    cfg = Config(
        domain="example.com",
        services=ServicesConfig(
            media=False,
            cloud=False,
            email=False,
            security=False,
            notifications=False,
        ),
        dns=DNSConfig(provider="local"),
    )
    steps = get_manual_steps(cfg)
    assert steps == []


def test_manual_steps_hook_failure_recovery():
    cfg = Config(domain="example.com", services=ServicesConfig(media=True))
    steps = get_manual_steps(cfg, {"media": ["Hook error: sonarr unreachable"]})
    recovery = [s for s in steps if s.service == "recovery"]
    assert len(recovery) == 1
    assert "deploy recover" in recovery[0].instructions


def test_manual_steps_music_sync_and_headscale_when_enabled():
    cfg = Config(
        domain="example.com",
        services=ServicesConfig(media=True, security=True),
        service_settings={"music-sync": {"enabled": True}},
    )
    steps = get_manual_steps(cfg)
    services = {s.service for s in steps}
    assert "music-sync" in services
    assert "headscale" in services
    headscale = next(s for s in steps if s.service == "headscale")
    assert headscale.category == "Optional"
    assert "jellyfin" not in services
    assert "vaultwarden" not in services


def test_custom_service_contributes_guidance_without_core_change() -> None:
    manifest = ServiceManifest.model_validate(
        {
            "name": "example",
            "label": "Example",
            "description": "Custom service",
            "icon": "box",
            "category": "cloud",
            "placement": "apps",
            "priority": 50,
            "routes": [
                {
                    "subdomain": "custom",
                    "upstream": "example:8080",
                    "exposure": "private",
                    "auth": {"mode": "forward_auth"},
                }
            ],
            "guidance": [
                {
                    "id": "finish-setup",
                    "phase": "post_deploy",
                    "category": "Required",
                    "title": "Finish setup",
                    "instructions": "Visit {url} for {domain}.",
                    "route_url": True,
                }
            ],
        }
    )

    steps = get_manual_steps(
        Config(domain="example.com", services={"cloud": True}),
        catalog=ServiceCatalog((manifest,)),
    )

    assert [(step.service, step.url, step.instructions) for step in steps] == [
        ("example", "https://custom.example.com", "Visit https://custom.example.com for example.com.")
    ]


def test_format_manual_steps_none_message():
    cfg = Config(
        domain="example.com",
        services=ServicesConfig(
            media=False,
            cloud=False,
            email=False,
            security=False,
            notifications=False,
        ),
        dns=DNSConfig(provider="local"),
        proxmox=ProxmoxConfig(provision_machines=False),
        service_settings={"gluetun": {"enabled": False}},
    )
    text = format_manual_steps_cli(get_all_manual_guidance(cfg))
    assert "No manual steps" in text


def test_prerequisites_hide_satisfied_infrastructure_credentials() -> None:
    cfg = Config(
        domain="example.com",
        proxmox=ProxmoxConfig(provision_machines=True),
        dns=DNSConfig(provider="cloudflare"),
    )

    steps = get_all_manual_guidance(
        cfg,
        secrets={
            "CLOUDFLARE_API_TOKEN": "token",
            "CLOUDFLARE_ZONE_ID": "zone",
            "PROXMOX_API_TOKEN_ID": "root@pam!homelab",
            "PROXMOX_API_TOKEN_SECRET": "secret",
        },
    )

    assert not {step.service for step in steps} & {"cloudflare", "proxmox"}


def test_pre_deploy_guidance_hides_when_active_user_secrets_are_present() -> None:
    manifest = ServiceManifest.model_validate(
        {
            "name": "example",
            "label": "Example",
            "description": "Custom service",
            "icon": "box",
            "category": "cloud",
            "placement": "apps",
            "priority": 50,
            "routes": [],
            "guidance": [
                {
                    "id": "setup",
                    "phase": "pre_deploy",
                    "category": "Prerequisite",
                    "title": "Provide token",
                    "instructions": "Provide the token during setup.",
                }
            ],
            "required_secrets": [
                {
                    "name": "EXAMPLE_TOKEN",
                    "tier": "user",
                    "description": "Example token",
                    "setup": {"label": "Token", "input": "password"},
                }
            ],
        }
    )
    cfg = Config(
        domain="example.com",
        services={"cloud": True},
        dns=DNSConfig(provider="local"),
        proxmox=ProxmoxConfig(provision_machines=False),
    )
    catalog = ServiceCatalog((manifest,))

    assert get_prerequisite_steps(cfg, catalog=catalog, secrets={"EXAMPLE_TOKEN": "present"}) == []
    assert [step.title for step in get_prerequisite_steps(cfg, catalog=catalog, secrets={})] == ["Provide token"]
