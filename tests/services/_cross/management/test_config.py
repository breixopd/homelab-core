from __future__ import annotations

import stat
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError
from toolkit.core.config.config import (
    Config,
    ExternalHost,
    RuntimeConfig,
    load_config,
    save_config,
    save_local_config,
)
from toolkit.core.manifest.settings import service_setting_int, service_setting_str


def test_default_config_all_services_enabled():
    cfg = Config()
    services = cfg.services.model_dump()
    assert set(services) == {"cloud", "email", "management", "media", "notifications", "security"}
    for name, enabled in services.items():
        assert enabled is True, f"{name} should be enabled by default"


def test_default_media_settings_are_plugin_owned():
    cfg = Config()
    assert service_setting_str(cfg, "media-library", "server") == "jellyfin"
    assert service_setting_str(cfg, "jellyfin", "hardware-transcode") == "auto"
    assert service_setting_int(cfg, "qbittorrent", "listen-port") == 6881


def test_default_timezone():
    cfg = Config()
    assert cfg.timezone == "Europe/Madrid"


def test_mesh_prefixes_are_strict_supported_headscale_subnets() -> None:
    cfg = Config(
        network={
            "mesh_ipv4_cidr": "100.100.0.0/16",
            "mesh_ipv6_cidr": "fd7a:115c:a1e0:1200::/56",
        }
    )

    assert cfg.network.mesh_ipv4_cidr == "100.100.0.0/16"
    assert cfg.network.mesh_ipv6_cidr == "fd7a:115c:a1e0:1200::/56"
    for field, value in (
        ("mesh_ipv4_cidr", "10.0.0.0/8"),
        ("mesh_ipv4_cidr", "100.100.1.1/16"),
        ("mesh_ipv6_cidr", "fd00::/48"),
        ("mesh_ipv6_cidr", "fd7a:115c:a1e0:1200::1/56"),
    ):
        with pytest.raises(ValidationError):
            Config(network={field: value})


def test_container_network_pool_is_configurable_and_canonical() -> None:
    cfg = Config(network={"container_ipv4_cidr": "172.29.0.0/17", "container_network_prefix": 27})

    assert cfg.network.container_ipv4_cidr == "172.29.0.0/17"
    assert cfg.network.container_network_prefix == 27

    for network in (
        {"container_ipv4_cidr": "172.29.1.1/17"},
        {"container_ipv4_cidr": "2001:db8::/64"},
        {"container_ipv4_cidr": "172.29.0.0/24", "container_network_prefix": 24},
        {"container_ipv4_cidr": "172.29.0.0/16", "container_network_prefix": 30},
    ):
        with pytest.raises(ValidationError):
            Config(network=network)


def test_removed_fleet_mesh_cidr_is_rejected() -> None:
    with pytest.raises(ValidationError, match="mesh_cidr"):
        Config(fleet={"mesh_cidr": "100.100.0.0/16"})


@pytest.mark.parametrize("field", ["mail_remote_access", "dns_remote_access"])
def test_removed_ambiguous_remote_access_fields_are_rejected(field: str) -> None:
    with pytest.raises(ValidationError, match=field):
        Config(network={field: True})


def test_machine_network_cannot_overlap_mesh_address_pool() -> None:
    machines = Config().machines
    machines["infra"] = machines["infra"].model_copy(
        update={"address": "100.100.0.10", "gateway": "100.100.0.1", "cidr": 16}
    )

    with pytest.raises(ValidationError, match="overlaps the mesh IPv4 pool"):
        Config(network={"mesh_ipv4_cidr": "100.100.0.0/16"}, machines=machines)


def test_machine_network_cannot_overlap_container_address_pool() -> None:
    machines = Config().machines
    machines["infra"] = machines["infra"].model_copy(
        update={"address": "172.29.0.10", "gateway": "172.29.0.1", "cidr": 17}
    )

    with pytest.raises(ValidationError, match="overlaps the container IPv4 pool"):
        Config(network={"container_ipv4_cidr": "172.29.0.0/17"}, machines=machines)


def test_service_settings_accept_dynamic_service_owned_scalar_overrides() -> None:
    cfg = Config(
        service_settings={
            "music-sync": {
                "enabled": False,
                "interval-minutes": 45,
                "spotify-playlists": "playlist-1",
            }
        }
    )

    assert cfg.service_settings["music-sync"] == {
        "enabled": False,
        "interval-minutes": 45,
        "spotify-playlists": "playlist-1",
    }


@pytest.mark.parametrize(
    "service_settings",
    [
        {"Bad Service": {"enabled": True}},
        {"music-sync": {"Bad Setting": True}},
        {"music-sync": {"enabled": [True]}},
    ],
)
def test_service_settings_reject_invalid_identifiers_and_non_scalars(service_settings: dict) -> None:
    with pytest.raises(ValidationError):
        Config(service_settings=service_settings)


@pytest.mark.parametrize(
    "service_settings",
    [
        {"unknown-service": {"enabled": True}},
        {"music-sync": {"unknown-setting": True}},
        {"music-sync": {"interval-minutes": "hourly"}},
    ],
)
def test_service_settings_reject_values_outside_manifest_contract(service_settings: dict) -> None:
    with pytest.raises(ValidationError):
        Config(service_settings=service_settings)


def test_load_save_roundtrip(tmp_path: Path):
    path = tmp_path / "config.yaml"
    original = Config(domain="example.com", email="admin@example.com")
    save_config(original, path)
    loaded = load_config(path)
    assert loaded.domain == original.domain
    assert loaded.email == original.email
    assert loaded.services.model_dump() == original.services.model_dump()
    assert loaded.service_settings == original.service_settings


def test_minimal_config_fills_defaults(tmp_path: Path):
    path = tmp_path / "config.yaml"
    path.write_text("domain: mysite.com\n")
    cfg = load_config(path)
    assert cfg.domain == "mysite.com"
    assert cfg.timezone == "Europe/Madrid"
    assert cfg.category_enabled("media") is True
    assert service_setting_str(cfg, "media-library", "server") == "jellyfin"


def test_validation_rejects_removed_core_media_config():
    with pytest.raises(ValidationError):
        Config(media={"server": "invalid"})  # type: ignore[arg-type]


def test_validation_rejects_bad_media_server_setting():
    with pytest.raises(ValidationError):
        Config(service_settings={"media-library": {"server": "invalid"}})


def test_enabled_vms_all_services():
    cfg = Config()
    assert cfg.enabled_nodes == ["infra", "media", "apps"]


def test_machine_enablement_is_independent_of_service_categories():
    machines = Config().machines
    machines["apps"] = machines["apps"].model_copy(update={"enabled": False})
    cfg = Config(services={"cloud": True}, machines=machines)

    assert "media" in cfg.enabled_nodes
    assert "apps" not in cfg.enabled_nodes


def test_is_multi_node():
    cfg = Config()
    assert cfg.is_multi_node is True


def test_enabled_categories():
    cfg = Config()
    cats = cfg.enabled_categories
    assert "management" in cats
    assert "media" in cats
    assert len(cats) == 6


def test_service_categories_are_manifest_discovered_and_strict() -> None:
    cfg = Config(services={"cloud": False})

    assert cfg.category_enabled("cloud") is False
    assert cfg.category_enabled("media") is True
    with pytest.raises(ValidationError, match="unknown service categories"):
        Config(services={"typo-category": True})
    with pytest.raises(ValidationError):
        Config(services={"cloud": "yes"})  # type: ignore[dict-item]


def test_enabled_category_cannot_disable_its_plugin_dependency(monkeypatch) -> None:
    from toolkit.categories import Category
    from toolkit.core.compose.registry import all_categories

    categories = [
        *all_categories(),
        Category(
            name="photos",
            label="Photos",
            compose_file="docker-compose.yml",
            _depends_on=["cloud"],
        ),
    ]
    monkeypatch.setattr("toolkit.core.compose.registry.all_categories", lambda: categories)

    with pytest.raises(ValidationError, match="photos->cloud"):
        Config(services={"photos": True, "cloud": False})


def test_external_hosts_serialization(tmp_path: Path):
    path = tmp_path / "config.yaml"
    cfg = Config(
        domain="example.com",
        external_hosts=[
            ExternalHost(
                name="nas",
                ip="192.168.1.100",
                services=["media-cache"],
                integrations={"media-cache": {"path": "/srv/media"}},
            )
        ],
    )
    save_config(cfg, path)
    loaded = load_config(path)
    assert len(loaded.external_hosts) == 1
    assert loaded.external_hosts[0].name == "nas"
    assert loaded.external_hosts[0].ip == "192.168.1.100"
    assert loaded.external_hosts[0].integration_value("media-cache", "path") == "/srv/media"


def test_external_host_names_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="Managed host names must be unique"):
        Config(
            external_hosts=[
                ExternalHost(name="edge", ip="192.0.2.10"),
                ExternalHost(name="edge", ip="192.0.2.11"),
            ]
        )


def test_remote_backup_target_must_reference_a_backup_host() -> None:
    with pytest.raises(ValidationError, match="Remote backups require"):
        Config(backups={"enabled": True, "target": "remote", "storage_host": "missing"})


def test_reconciled_host_requires_timestamp() -> None:
    with pytest.raises(ValidationError, match="reconciliation timestamp"):
        ExternalHost(name="edge", ip="192.0.2.10", reconciled=True)


def test_management_cannot_be_disabled():
    with pytest.raises(ValidationError, match="always-on"):
        Config(services={"management": False})


def test_ssh_config_defaults():
    from toolkit.core.config.config import SSHConfig

    ssh = SSHConfig()
    assert ssh.auth_method == "key"
    assert ssh.key_file == ""
    assert ssh.password == ""

    with pytest.raises(ValidationError, match="user"):
        SSHConfig.model_validate({"user": "root"})


def test_ssh_config_roundtrip(tmp_path):
    from toolkit.core.config.config import SSHConfig

    cfg = Config(
        ssh=SSHConfig(
            auth_method="password",
            key_file="/root/.ssh/custom",
            password="secret123",
        )
    )
    path = tmp_path / "config.yaml"
    save_config(cfg, path)
    save_local_config(cfg, tmp_path)
    loaded = load_config(path)
    assert loaded.ssh.auth_method == "password"
    assert loaded.ssh.key_file == "/root/.ssh/custom"
    assert loaded.ssh.password == "secret123"
    tracked = path.read_text()
    assert "/root/.ssh/custom" not in tracked
    assert "secret123" not in tracked


def test_runtime_config_roundtrip(tmp_path):
    cfg = Config(runtime=RuntimeConfig(puid=2000, pgid=3000))

    path = tmp_path / "config.yaml"
    save_config(cfg, path)
    loaded = load_config(path)

    assert loaded.runtime.puid == 2000
    assert loaded.runtime.pgid == 3000


def test_storage_config_roundtrip(tmp_path):
    from toolkit.core.config.config import StorageConfig

    cfg = Config(
        storage=StorageConfig(
            filesystem="zfs",
            raid_level="raidz1",
            raw_disks_gb=4000,
            disk_count=3,
        )
    )
    path = tmp_path / "config.yaml"
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.storage.filesystem == "zfs"
    assert loaded.storage.raid_level == "raidz1"
    assert loaded.storage.raw_disks_gb == 4000
    assert loaded.storage.disk_count == 3
    assert loaded.storage.usable_gb > 0


def test_node_ip_helper():
    machines = Config().machines
    machines["infra"] = machines["infra"].model_copy(update={"address": "10.10.10.99"})
    cfg = Config(machines=machines)

    assert cfg.node_ip("infra") == "10.10.10.99"


def test_unknown_node_fails_closed():
    with pytest.raises(KeyError, match="unknown machine"):
        Config().node_ip("missing")


def test_proxmox_config_defaults():
    from toolkit.core.config.config import (
        DEFAULT_LXC_TEMPLATE_SHA256,
        DEFAULT_LXC_TEMPLATE_URL,
        DEFAULT_PROXMOX_NODE,
    )

    cfg = Config()
    assert cfg.proxmox.api_url == ""
    assert cfg.proxmox.node == DEFAULT_PROXMOX_NODE
    assert cfg.proxmox.lxc_template_url == DEFAULT_LXC_TEMPLATE_URL
    assert cfg.proxmox.lxc_template_checksum == DEFAULT_LXC_TEMPLATE_SHA256
    assert cfg.proxmox.lxc_template_datastore == "local"
    assert cfg.proxmox.tls_ca_file == ""
    assert cfg.proxmox.provision_machines is True
    assert cfg.proxmox.ssh.user == "root"
    assert cfg.proxmox.ssh.port == 22
    assert cfg.proxmox.ssh.key_file == ""


def test_proxmox_control_key_is_local_only(tmp_path: Path) -> None:
    cfg = Config(
        proxmox={
            "ssh": {
                "user": "operator",
                "port": 2222,
                "key_file": "/keys/proxmox",
                "connect_timeout": 17,
            }
        },
        ssh={"key_file": "/keys/guests", "password": "guest-password"},
    )

    save_config(cfg, tmp_path)
    save_local_config(cfg, tmp_path)

    tracked = yaml.safe_load((tmp_path / "config.yaml").read_text())
    local = yaml.safe_load((tmp_path / "config.local.yaml").read_text())
    assert tracked["proxmox"]["ssh"] == {
        "user": "operator",
        "port": 2222,
        "connect_timeout": 17,
        "command_timeout": 120,
        "retries": 3,
    }
    assert "key_file" not in tracked["ssh"]
    assert "password" not in tracked["ssh"]
    assert local["proxmox"]["ssh"]["key_file"] == "/keys/proxmox"
    assert local["ssh"] == {"key_file": "/keys/guests", "password": "guest-password"}
    loaded = load_config(tmp_path)
    assert loaded.proxmox.ssh.key_file == "/keys/proxmox"
    assert loaded.ssh.key_file == "/keys/guests"


def test_proxmox_config_rejects_unknown_and_removed_keys():
    from toolkit.core.config.config import ProxmoxConfig

    for field in ("ciuser", "lxc_template_id", "tls_insecure", "unmodeled_setting"):
        with pytest.raises(ValidationError, match="extra_forbidden"):
            ProxmoxConfig.model_validate({field: "value"})


def test_proxmox_control_host_is_explicit_or_derived_from_api_url():
    from toolkit.core.config.config import ProxmoxConfig

    derived = ProxmoxConfig(api_url="https://pve.example.test:8006/api2/json")
    explicit = ProxmoxConfig(
        api_url="https://pve.example.test:8006",
        control_host="10.0.0.5",
    )

    assert derived.resolved_control_host == "pve.example.test"
    assert explicit.resolved_control_host == "10.0.0.5"


def test_unknown_top_level_config_is_rejected():
    with pytest.raises(ValidationError, match="extra_forbidden"):
        Config.model_validate({"legacy_machine_map": {}})


def test_proxmox_config_roundtrip(tmp_path):
    from toolkit.core.config.config import ProxmoxConfig

    cfg = Config(
        proxmox=ProxmoxConfig(
            api_url="https://192.168.1.100:8006",
            node="mynode",
            provision_machines=False,
        )
    )
    path = tmp_path / "config.yaml"
    save_config(cfg, path)
    loaded = load_config(path)
    assert loaded.proxmox.api_url == "https://192.168.1.100:8006"
    assert loaded.proxmox.node == "mynode"
    assert loaded.proxmox.provision_machines is False


def test_dns_config_roundtrip(tmp_path):
    from toolkit.core.config.config import DNSConfig

    cfg = Config(dns=DNSConfig(provider="cloudflare", public_ip="1.2.3.4"))

    path = tmp_path / "config.yaml"
    save_config(cfg, path)
    loaded = load_config(path)

    assert loaded.dns.provider == "cloudflare"
    assert loaded.dns.public_ip == "1.2.3.4"


def test_discover_proxmox_machines_bad_url():
    from toolkit.core.infra.autodetect import discover_proxmox_machines

    # Bad URL should return empty dict, not crash
    result = discover_proxmox_machines(
        api_url="https://0.0.0.0:1",
        token_id="test@pam!test",
        token_secret="bad",
    )
    assert result == {}


def test_discover_proxmox_vms_invalid_scheme():
    import pytest
    from toolkit.core.infra.proxmox import validate_proxmox_url

    with pytest.raises(ValueError, match="scheme"):
        validate_proxmox_url("ftp://example.com")


def test_discover_proxmox_vms_valid_url():
    from toolkit.core.infra.proxmox import validate_proxmox_url

    result = validate_proxmox_url("https://192.168.1.100:8006")
    assert result.endswith("/api2/json")


def test_owner_password_not_in_tracked_config(tmp_path):
    """The plaintext SSO owner_password must NEVER be persisted in the git-tracked config.yaml.

    Regression: save_config() used to serialize owner_password via model_dump()
    because the strip list (_LOCAL_CONFIG_FIELDS) only covered nested sections
    like proxmox.ssh_public_key, not top-level fields. Result: the plaintext
    SSO password landed in the git-tracked file whenever an operator set it.
    """
    cfg = Config(domain="example.com", owner_password="my-secret-sso-password")
    path = tmp_path / "config.yaml"
    save_config(cfg, path)

    tracked_content = path.read_text()
    assert "my-secret-sso-password" not in tracked_content, "owner_password leaked into tracked config.yaml!"
    assert "owner_password" not in tracked_content, "owner_password field name present in tracked config.yaml"


def test_owner_password_persisted_in_local_config(tmp_path):
    """The owner_password should be moved to the gitignored config.local.yaml."""
    from toolkit.core.config.config import save_local_config

    cfg = Config(domain="example.com", owner_password="my-secret-sso-password")
    save_local_config(cfg, tmp_path)

    local_path = tmp_path / "config.local.yaml"
    assert local_path.exists(), "config.local.yaml not created"
    local_content = local_path.read_text()
    assert "my-secret-sso-password" in local_content, "owner_password not in config.local.yaml — should be saved there"
    assert stat.S_IMODE(local_path.stat().st_mode) == 0o600


def test_owner_password_loads_from_local_override(tmp_path):
    """When load_config merges config.yaml + config.local.yaml, owner_password comes back."""
    cfg = Config(domain="example.com", owner_password="sso-pass-123")
    save_config(cfg, tmp_path)
    save_local_config(cfg, tmp_path)

    loaded = load_config(tmp_path / "config.yaml")
    assert loaded.owner_password == "sso-pass-123"


def test_owner_username_is_persisted_and_rejects_service_accounts(tmp_path):
    cfg = Config(domain="example.com", email="owner@example.com", owner_username="owner")

    save_config(cfg, tmp_path)

    assert load_config(tmp_path).owner_username == "owner"
    with pytest.raises(ValidationError, match="reserved"):
        Config(owner_username="admin")
