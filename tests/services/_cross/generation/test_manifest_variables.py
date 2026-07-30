from __future__ import annotations

from tests.helpers.machines import single_control_machines
from toolkit.core.config.config import Config
from toolkit.core.infra.edge_network import edge_network_values
from toolkit.core.manifest.catalog import load_service_catalog
from toolkit.core.manifest.schema import ServiceManifest
from toolkit.core.manifest.variables import (
    compile_manifest_host_sources,
    compile_manifest_variables,
    compile_role_secret_fallbacks,
    compile_role_secret_projections,
    compile_role_variables,
    domain_to_base_dn,
)


def test_compile_manifest_variables_resolves_typed_service_settings() -> None:
    manifest = load_service_catalog().require("qbittorrent")
    cfg = Config(service_settings={"qbittorrent": {"listen-port": 4545}})

    variables = compile_manifest_variables(cfg, manifest)

    assert variables["QBITTORRENT_PORT_BIND"] == "4545"


def test_compile_manifest_variables_resolves_service_placement() -> None:
    variables = compile_manifest_variables(Config(), load_service_catalog().require("kopia"))

    assert variables["KOPIA_SERVER_HOST"] == "10.10.10.10"


def test_grafana_dev_datasource_uses_dev_postgres_placement() -> None:
    cfg = Config()
    variables = compile_manifest_variables(
        cfg,
        load_service_catalog().require("grafana"),
        service_nodes={"dev-postgres": "media"},
    )

    assert variables["DEV_VM_IP"] == cfg.node_ip("media")


def test_gitea_trusted_proxy_follows_ingress_placement() -> None:
    cfg = Config()
    variables = compile_manifest_variables(
        cfg,
        load_service_catalog().require("gitea"),
        service_nodes={"caddy": "media"},
    )

    assert variables["GITEA_TRUSTED_PROXIES"] == cfg.node_ip("media")


def test_compile_manifest_variables_resolves_ingress_provider_placement() -> None:
    manifest = load_service_catalog().require("navidrome")
    cfg = Config()

    variables = compile_manifest_variables(
        cfg,
        manifest,
        service_nodes={"edge-router": "media"},
        capability_providers={"ingress": "edge-router"},
    )

    assert variables["NAVIDROME_EXTAUTH_TRUSTED_SOURCES"] == f"{cfg.node_ip('media')}/32"


def test_navidrome_trusted_proxy_uses_the_single_node_edge_address() -> None:
    cfg = Config(machines=single_control_machines())

    variables = compile_manifest_variables(cfg, load_service_catalog().require("navidrome"))

    assert variables["NAVIDROME_EXTAUTH_TRUSTED_SOURCES"] == f"{edge_network_values(cfg)[1]}/32"


def test_compile_manifest_variables_resolves_service_owned_settings() -> None:
    manifest = load_service_catalog().require("music-sync")

    defaults = compile_manifest_variables(Config(), manifest)
    overridden = compile_manifest_variables(
        Config(service_settings={"music-sync": {"interval-minutes": 25}}),
        manifest,
    )

    assert defaults["MUSIC_SYNC_INTERVAL_MINUTES"] == "60"
    assert overridden["MUSIC_SYNC_INTERVAL_MINUTES"] == "25"


def test_compile_manifest_variables_resolves_bounded_deployment_values() -> None:
    catalog = load_service_catalog()
    cfg = Config(domain="localhost", email="owner@example.com")

    lldap = compile_manifest_variables(cfg, catalog.require("lldap"))
    navidrome = compile_manifest_variables(cfg, catalog.require("navidrome"))

    assert lldap["LLDAP_HTTP_URL"] == "http://users.localhost"
    assert lldap["LLDAP_BASE_DN"] == "dc=home,dc=local"
    assert lldap["AUTHELIA_DEFAULT_EMAIL"] == "owner@example.com"
    assert navidrome["NAVIDROME_EXTAUTH_TRUSTED_SOURCES"] == f"{cfg.node_ip('infra')}/32"
    assert navidrome["NAVIDROME_EXTAUTH_LOGOUT_URL"] == "http://auth.localhost/logout"
    assert domain_to_base_dn("sub.example.co.uk") == "dc=sub,dc=example,dc=co,dc=uk"


def test_compile_role_variables_resolves_service_owned_database_connections() -> None:
    cfg = Config()

    infra = compile_role_variables(cfg, "infra")
    apps = compile_role_variables(cfg, "apps")
    media = compile_role_variables(cfg, "media")

    assert infra["AUTHELIA_DB_HOST"] == "postgres"
    assert infra["AUTHELIA_DB_PORT"] == "5432"
    assert infra["AUTHELIA_DB_NAME"] == "authelia"
    assert infra["AUTHELIA_DB_USER"] == "authelia"
    assert infra["GRAFANA_DB_HOST"] == "postgres"
    assert apps["GITEA_DB_HOST"] == cfg.node_ip("infra")
    assert apps["GITEA_DB_PORT"] == "5432"
    assert apps["GITEA_DB_NAME"] == "gitea"
    assert apps["GITEA_DB_USER"] == "gitea"
    assert apps["IMMICH_DB_HOST"] == "immich-postgres"
    assert apps["IMMICH_DB_NAME"] == "immich"
    assert apps["NEXTCLOUD_DB_HOST"] == cfg.node_ip("infra")
    assert apps["VAULTWARDEN_DB_HOST"] == cfg.node_ip("infra")
    assert infra["AUTHELIA_REDIS_HOST"] == "redis"
    assert infra["AUTHELIA_REDIS_PORT"] == "6379"
    assert infra["HOMELAB_REDIS_HOST"] == "redis"
    assert "IMMICH_REDIS_HOST" not in apps
    assert apps["NEXTCLOUD_REDIS_HOST"] == cfg.node_ip("infra")
    assert not any(name.endswith("_DB_HOST") for name in media)
    assert "REDIS_HOST" not in infra
    assert "REDIS_HOST" not in apps
    assert "REDIS_HOST" not in media
    assert infra["ALLOY_LOKI_HOST"] == "loki"
    assert infra["ALLOY_LOKI_PORT"] == "3100"
    assert media["ALLOY_LOKI_HOST"] == cfg.node_ip("infra")
    assert apps["ALLOY_LOKI_HOST"] == cfg.node_ip("infra")
    assert infra["MAILSERVER_LLDAP_HOST"] == "lldap"
    assert infra["MAILSERVER_LLDAP_PORT"] == "3890"
    assert infra["LLDAP_BIND_DN"] == "cn=ldap-bind,ou=people,dc=home,dc=local"
    assert "MAILSERVER_LLDAP_HOST" not in apps


def test_compile_role_secret_projections_are_scoped_to_owner_node() -> None:
    cfg = Config()
    secrets = {
        "CLOUDFLARE_API_TOKEN": "cloudflare-token",
        "CROWDSEC_CADDY_BOUNCER_KEY": "crowdsec-bouncer-token",
    }

    assert compile_role_secret_projections(cfg, "infra", secrets) == {
        "CF_API_TOKEN": "cloudflare-token",
        "CADDY_BOUNCER_API_KEY": "crowdsec-bouncer-token",
    }
    assert compile_role_secret_projections(cfg, "apps", secrets) == {}


def test_compile_role_secret_fallbacks_are_scoped_without_leaking_source() -> None:
    cfg = Config()
    secrets = {"SSO_USER_PASSWORD": "owner-password"}

    infra = compile_role_secret_fallbacks(cfg, "infra", secrets)
    apps = compile_role_secret_fallbacks(cfg, "apps", secrets)

    assert infra["GRAFANA_ADMIN_PASSWORD"] == "owner-password"
    assert apps["NEXTCLOUD_ADMIN_PASSWORD"] == "owner-password"
    assert "SSO_USER_PASSWORD" not in apps


def test_compile_manifest_host_sources_resolves_root_and_variant() -> None:
    manifest = ServiceManifest.model_validate(
        {
            "name": "example",
            "label": "Example",
            "description": "Example service",
            "icon": "box",
            "category": "cloud",
            "placement": "apps",
            "priority": 50,
            "host_sources": {
                "EXAMPLE_DATA_SOURCE": {
                    "path": "data/example",
                    "variants": [
                        {
                            "when": {"path": "network.expose_via_internet", "equals": True},
                            "path": "data/public-example",
                        }
                    ],
                }
            },
        }
    )

    private = compile_manifest_host_sources(Config(network={"expose_via_internet": False}), manifest, "/opt/homelab")
    public = compile_manifest_host_sources(Config(network={"expose_via_internet": True}), manifest, "/opt/homelab")

    assert private == {"EXAMPLE_DATA_SOURCE": "/opt/homelab/data/example"}
    assert public == {"EXAMPLE_DATA_SOURCE": "/opt/homelab/data/public-example"}
