from __future__ import annotations

import stat
from pathlib import Path

import pytest
from tests.helpers.machines import enabled_machines, single_control_machines
from toolkit.core.compose.registry import load_all
from toolkit.core.config.config import (
    Config,
    FleetConfig,
    RuntimeConfig,
    ServicesConfig,
    save_config,
)
from toolkit.core.config.storage import config_path
from toolkit.core.generate.generate import (
    _build_env_vars,
    _env_escape,
    generate_all,
    generate_configs,
    render_env,
    write_env,
)

load_all()


def _caddy_site(content: str, host: str) -> str:
    start = content.index(f"{host} {{")
    opening = content.index("{", start)
    depth = 0
    for index in range(opening, len(content)):
        if content[index] == "{":
            depth += 1
        elif content[index] == "}":
            depth -= 1
            if depth == 0:
                return content[start : index + 1]
    raise AssertionError(f"unclosed Caddy site block for {host}")


def _write_config(root: Path, **overrides) -> Config:
    cfg = Config(domain="example.com", email="admin@example.com", **overrides)
    save_config(cfg, config_path(root))
    return cfg


def test_env_has_base_domain():
    config = Config(domain="example.com")
    env = render_env(_build_env_vars(config, "infra", {}))
    assert "BASE_DOMAIN=example.com" in env


def test_gitea_host_uses_git_subdomain():
    config = Config(domain="example.com")
    env = _build_env_vars(config, "apps", {})
    assert env["GITEA_HOST"] == "git.example.com"
    assert env["GITEA_ROOT_URL"] == "https://git.example.com"


def test_env_has_timezone():
    config = Config(timezone="Europe/Madrid")
    env = render_env(_build_env_vars(config, "infra", {}))
    assert "TZ=Europe/Madrid" in env


def test_env_database_connections_are_service_scoped():
    config = Config(domain="example.com")
    infra = _build_env_vars(config, "infra", {})
    apps = _build_env_vars(config, "apps", {})

    assert infra["AUTHELIA_DB_HOST"] == "postgres"
    assert infra["GRAFANA_DB_HOST"] == "postgres"
    assert apps["GITEA_DB_HOST"] == "10.10.10.10"
    assert apps["IMMICH_DB_HOST"] == "immich-postgres"
    assert apps["NEXTCLOUD_DB_HOST"] == "10.10.10.10"
    assert apps["VAULTWARDEN_DB_HOST"] == "10.10.10.10"
    assert infra["AUTHELIA_REDIS_HOST"] == "redis"
    assert infra["HOMELAB_REDIS_HOST"] == "redis"
    assert "IMMICH_REDIS_HOST" not in apps
    assert apps["NEXTCLOUD_REDIS_HOST"] == "10.10.10.10"
    assert "POSTGRES_HOST" not in infra
    assert "POSTGRES_HOST" not in apps
    assert "REDIS_HOST" not in infra
    assert "REDIS_HOST" not in apps


def test_env_includes_declared_role_secrets(tmp_path: Path):
    config = Config(domain="example.com")
    compose = tmp_path / "toolkit" / "services" / "postgres" / "compose.yaml"
    compose.parent.mkdir(parents=True)
    compose.write_text("environment:\n  POSTGRES_PASSWORD: ${POSTGRES_PASSWORD}\n  REDIS_PASSWORD: ${REDIS_PASSWORD}\n")
    secrets = {"POSTGRES_PASSWORD": "test123", "REDIS_PASSWORD": "abc"}
    env = render_env(_build_env_vars(config, "infra", secrets, tmp_path))
    assert "POSTGRES_PASSWORD=test123" in env
    assert "REDIS_PASSWORD=abc" in env


def test_env_quotes_special_chars():
    env = render_env({"SOME_KEY": "value with spaces"})
    assert 'SOME_KEY="value with spaces"' in env


def test_env_uses_typed_runtime_and_media_defaults():
    config = Config(
        domain="example.com",
        service_settings={
            "jellyfin": {"hardware-transcode": "vaapi"},
            "qbittorrent": {"listen-port": 4545},
            "gluetun": {"server-countries": "NL,CH"},
            "media-cache": {"enabled": True, "cold-after-days": 21, "uplink-mbps": 750},
            "music-sync": {"enabled": True, "interval-minutes": 30, "prune": True},
        },
        runtime=RuntimeConfig(puid=2000, pgid=3000),
    )

    env_vars = _build_env_vars(config, "media", {})

    assert env_vars["PUID"] == "2000"
    assert env_vars["PGID"] == "3000"
    assert env_vars["QBITTORRENT_PORT_BIND"] == "4545"
    assert env_vars["COLD_AFTER_DAYS"] == "21"
    assert env_vars["UPLINK_MBPS"] == "750"
    assert env_vars["MUSIC_SYNC_INTERVAL_MINUTES"] == "30"
    assert env_vars["MUSIC_SYNC_PRUNE"] == "true"
    assert env_vars["VPN_SERVER_COUNTRIES"] == "NL,CH"


def test_env_projects_service_manifest_worker_settings() -> None:
    config = Config(
        service_settings={
            "tdarr": {"cpu-workers": 3, "gpu-workers": 2, "health-cpu-workers": 4},
        },
    )

    env_vars = _build_env_vars(config, "media", {})

    assert env_vars["TDARR_CPU_WORKERS"] == "3"
    assert env_vars["TDARR_GPU_WORKERS"] == "2"
    assert env_vars["TDARR_HEALTH_CPU_WORKERS"] == "4"


def test_env_vars_use_service_settings_and_runtime():
    from toolkit.core.config.config import RuntimeConfig

    config = Config(
        domain="example.com",
        service_settings={
            "jellyfin": {"hardware-transcode": "nvidia"},
            "qbittorrent": {"listen-port": 7890},
            "gluetun": {"server-countries": "SE"},
            "music-sync": {"enabled": True, "interval-minutes": 15},
        },
        runtime=RuntimeConfig(puid=1111, pgid=2222),
    )
    env_vars = _build_env_vars(
        config,
        "media",
        {
            "PUID": "9999",
            "PGID": "8888",
            "QBITTORRENT_PORT_BIND": "1",
            "MUSIC_SYNC_INTERVAL_MINUTES": "1",
            "VPN_SERVER_COUNTRIES": "XX",
        },
    )

    assert env_vars["PUID"] == "1111"
    assert env_vars["PGID"] == "2222"
    assert env_vars["QBITTORRENT_PORT_BIND"] == "7890"
    assert env_vars["MUSIC_SYNC_INTERVAL_MINUTES"] == "15"
    assert env_vars["VPN_SERVER_COUNTRIES"] == "SE"


def test_write_env_does_not_retain_plaintext_backup(tmp_path: Path):
    config = Config(domain="example.com")
    path = write_env(config, "infra", {}, tmp_path)
    assert path.exists()

    write_env(config, "infra", {"NEW_KEY": "value"}, tmp_path)
    assert not path.with_suffix(".backup").exists()


def test_generate_all_creates_env_per_vm(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOMELAB_TEST_PLAINTEXT_SECRETS", "1")
    _write_config(tmp_path)
    results = generate_all(tmp_path)
    assert "infra" in results
    assert "media" in results
    assert "apps" in results
    for vm, path in results.items():
        assert path.exists(), f"{vm} .env not created"
        content = path.read_text()
        assert "BASE_DOMAIN=example.com" in content
        bundle = tmp_path / "generated" / "bundles" / vm / ".hooks.env"
        bundle_content = bundle.read_text(encoding="utf-8")
        assert "TZ=Europe/Madrid" in bundle_content
        assert "COMPOSE_PROFILES=" in bundle_content
        assert "PROXMOX_API_TOKEN_SECRET" not in bundle_content


def test_write_env_includes_manifest_runtime_profile_on_placed_nodes(tmp_path: Path):
    config = Config(domain="example.com")
    media_env = write_env(config, "media", {}, tmp_path).read_text()
    infra_env = write_env(config, "infra", {}, tmp_path).read_text()
    assert "monitoring-agent" in media_env
    assert "monitoring-agent" not in infra_env


def test_apps_env_contains_only_apps_runtime_secrets(tmp_path: Path):
    config = Config(domain="example.com")
    compose = tmp_path / "toolkit" / "services" / "immich-server" / "compose.yaml"
    compose.parent.mkdir(parents=True)
    compose.write_text("environment:\n  DB_PASSWORD: ${IMMICH_DB_PASSWORD}\n")

    path = write_env(
        config,
        "apps",
        {
            "IMMICH_DB_PASSWORD": "apps-secret",
            "SONARR_API_KEY": "media-secret",
            "PROXMOX_API_TOKEN_SECRET": "controller-secret",
            "CF_API_TOKEN": "dns-secret",
        },
        tmp_path,
    )
    content = path.read_text()
    assert "IMMICH_DB_PASSWORD=apps-secret" in content
    assert "SONARR_API_KEY" not in content
    assert "PROXMOX_API_TOKEN_SECRET" not in content
    assert "CF_API_TOKEN" not in content


def test_generate_all_with_partial_services(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("HOMELAB_TEST_PLAINTEXT_SECRETS", "1")
    _write_config(
        tmp_path,
        services=ServicesConfig(
            management=True,
            media=True,
            cloud=False,
            notifications=False,
            email=False,
            security=False,
        ),
        service_settings={"jellyfin": {"hardware-transcode": "none"}},
        machines=enabled_machines("infra", "media"),
    )
    results = generate_all(tmp_path)
    assert "infra" in results
    assert "media" in results
    assert "apps" not in results


def test_generate_all_supports_renamed_machine_ids(tmp_path: Path, monkeypatch) -> None:
    from tests.helpers.machines import renamed_default_machines

    monkeypatch.setenv("HOMELAB_TEST_PLAINTEXT_SECRETS", "1")
    _write_config(tmp_path, machines=renamed_default_machines())

    results = generate_all(tmp_path)

    assert set(results) == {"core", "stream", "data"}
    assert (tmp_path / "generated" / "core" / ".env").is_file()
    assert (tmp_path / "generated" / "stream" / ".env").is_file()
    assert (tmp_path / "generated" / "data" / ".env").is_file()


def test_manifest_host_sources_match_role_compose_layout(tmp_path):
    infra = _build_env_vars(Config(), "infra", {}, root=tmp_path)
    apps = _build_env_vars(Config(), "apps", {}, root=tmp_path)

    assert infra["ADGUARD_CONFIG_SOURCE"].endswith("/data/adguard/config")
    assert infra["ADGUARD_WORK_SOURCE"].endswith("/data/adguard/work")
    assert infra["POSTGRES_DATA_SOURCE"].endswith("/data/postgres")
    assert infra["PROMETHEUS_CONFIG_SOURCE"].endswith("/generated/prometheus.yml")
    assert "generated/authelia" in infra["AUTHELIA_CONFIG_SOURCE"]
    assert "FMD_DATA_SOURCE" not in infra
    assert apps["FMD_DATA_SOURCE"].endswith("/data/fmd")


def test_private_ip_set_per_vm():
    config = Config(domain="example.com")
    infra_vars = _build_env_vars(config, "infra", {})
    media_vars = _build_env_vars(config, "media", {})
    assert infra_vars["PRIVATE_IP"] == "10.10.10.10"
    assert media_vars["PRIVATE_IP"] == "10.10.10.11"


def test_navidrome_external_auth_trusts_only_the_immediate_caddy_peer():
    multi = Config(domain="example.com")
    multi_env = _build_env_vars(multi, "media", {})
    assert multi_env["NAVIDROME_EXTAUTH_TRUSTED_SOURCES"] == "10.10.10.10/32"

    single = Config(
        domain="example.com",
        services=ServicesConfig(media=False, cloud=False, email=False),
        machines=single_control_machines(),
    )
    single_env = _build_env_vars(single, "infra", {})
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.variables import compile_manifest_variables

    navidrome = compile_manifest_variables(single, load_service_catalog().require("navidrome"))
    assert navidrome["NAVIDROME_EXTAUTH_TRUSTED_SOURCES"] == f"{single_env['CADDY_EDGE_IP']}/32"
    assert single_env["EDGE_SUBNET"] == "172.31.250.0/24"
    assert single_env["EDGE_DYNAMIC_RANGE"] == "172.31.250.128/25"


def test_caddyfile_generated(tmp_path, seed_oidc_secrets):
    """Caddyfile is generated with routes."""
    cfg = Config(
        domain="example.com",
        email="test@example.com",
        services=ServicesConfig(management=True, media=True),
    )
    seed_oidc_secrets(cfg, tmp_path)
    generate_configs(cfg, tmp_path)

    caddyfile = tmp_path / "generated" / "Caddyfile"
    assert caddyfile.exists()
    content = caddyfile.read_text()
    assert "example.com" in content
    assert "reverse_proxy" in content
    assert "(compression)" in content
    assert "(authelia)" in content


def test_portal_plugin_generates_only_the_mounted_portal_path(tmp_path, seed_oidc_secrets):
    cfg = Config(domain="example.com", email="test@example.com")
    seed_oidc_secrets(cfg, tmp_path)

    generate_configs(cfg, tmp_path)

    assert (tmp_path / "generated/portal/index.html").is_file()
    assert not (tmp_path / "config/caddy/portal").exists()


def test_prometheus_targets_generated(tmp_path, seed_oidc_secrets):
    """Prometheus config includes plugin-declared targets for every managed node."""
    cfg = Config(domain="example.com")
    seed_oidc_secrets(cfg, tmp_path)
    generate_configs(cfg, tmp_path)

    content = (tmp_path / "generated" / "prometheus.yml").read_text(encoding="utf-8")
    assert "10.10.10.10:9100" in content
    assert "10.10.10.11:9100" in content
    assert "10.10.10.12:9100" in content
    assert not (tmp_path / "generated" / "prometheus" / "targets").exists()


def test_prometheus_primary_targets_use_configured_control_node(tmp_path, seed_oidc_secrets):
    base = Config(domain="example.com")
    raw = base.model_dump(mode="python")
    raw["machines"]["infra"]["labels"] = tuple(
        label for label in raw["machines"]["infra"]["labels"] if label != "control"
    )
    raw["machines"]["apps"]["labels"] = (*raw["machines"]["apps"]["labels"], "control")
    cfg = Config.model_validate(raw)
    seed_oidc_secrets(cfg, tmp_path)

    generate_configs(cfg, tmp_path)

    content = (tmp_path / "generated" / "prometheus.yml").read_text(encoding="utf-8")
    assert content.count("instance: 'apps'") >= 2


def test_prometheus_scrapes_manifest_declared_fmd_metrics(tmp_path, seed_oidc_secrets):
    cfg = Config(domain="example.com")
    seed_oidc_secrets(cfg, tmp_path)

    generate_configs(cfg, tmp_path)

    content = (tmp_path / "generated" / "prometheus.yml").read_text(encoding="utf-8")
    assert "job_name: fmd-server" in content
    assert "10.10.10.12:9101" in content
    assert "metrics_path: /metrics" in content


def test_full_generate_fails_when_artifact_validation_reports_errors(tmp_path, monkeypatch):
    from toolkit.core.generate.generate import GeneratedArtifactValidationError, run_full_generate
    from toolkit.core.generate.validate import ValidationReport

    cfg = Config(domain="example.com")
    monkeypatch.setattr("toolkit.core.generate.generate.generate_all", lambda _root: {})
    monkeypatch.setattr("toolkit.core.generate.generate.generate_configs", lambda _cfg, _root: [])
    monkeypatch.setattr(
        "toolkit.core.generate.validate.validate_generated_artifacts",
        lambda _root: ValidationReport(errors=["unreachable route"]),
    )

    with pytest.raises(GeneratedArtifactValidationError, match="unreachable route"):
        run_full_generate(tmp_path, cfg)


def test_headscale_acl_generated(tmp_path, seed_oidc_secrets):
    from toolkit.core.config.storage import secrets_path

    secrets_path(tmp_path).write_text("HEADSCALE_OIDC_CLIENT_SECRET: test\n")
    cfg = Config(
        domain="example.com",
        services=ServicesConfig(security=True),
        fleet=FleetConfig(headscale_tags=["tag:fleet-external", "tag:homelab"]),
    )
    seed_oidc_secrets(cfg, tmp_path)
    generate_configs(cfg, tmp_path)
    acl = tmp_path / "generated" / "headscale" / "acl.hujson"
    assert acl.exists()
    text = acl.read_text()
    assert "tag:fleet-external" in text
    assert "tagOwners" in text
    assert '"randomizeClientPort": false' in text
    config = (tmp_path / "generated" / "headscale" / "config.yaml").read_text()
    assert "node:\n  ephemeral:\n    inactivity_timeout: 30m" in config
    assert "ephemeral_node_inactivity_timeout" not in config
    assert "randomize_client_port" not in config
    assert "type: sqlite" in config
    assert "path: /var/lib/headscale/db.sqlite" in config
    assert "write_ahead_log: true" in config
    assert "wal_autocheckpoint: 1000" in config
    assert "parameterized_queries: true" in config
    assert "postgres:" not in config


def test_headscale_and_private_routes_follow_typed_network_config(tmp_path, seed_oidc_secrets):
    cfg = Config(
        domain="example.com",
        network={
            "mesh_ipv4_cidr": "100.100.0.0/16",
            "mesh_ipv6_cidr": "fd7a:115c:a1e0:1200::/56",
        },
    )
    seed_oidc_secrets(cfg, tmp_path)

    generate_configs(cfg, tmp_path)

    headscale = (tmp_path / "generated" / "headscale" / "config.yaml").read_text()
    assert "# Headscale configuration — auto-generated by homelab-toolkit (schema: v0.29)" in headscale
    assert "v4: 100.100.0.0/16" in headscale
    assert "v6: fd7a:115c:a1e0:1200::/56" in headscale
    assert "update_frequency: 3h" in headscale
    caddy = (tmp_path / "generated" / "Caddyfile").read_text()
    grafana = _caddy_site(caddy, "grafana.example.com")
    assert "@outside_mesh not remote_ip 10.10.10.0/24 100.100.0.0/16" in grafana


def test_seaweedfs_s3_json_generated_when_cloud_enabled(tmp_path, seed_oidc_secrets):
    from toolkit.core.config.storage import secrets_path

    secrets_path(tmp_path).write_text("SEAWEEDFS_S3_ACCESS_KEY: mykey\nSEAWEEDFS_S3_SECRET_KEY: mysecret\n")
    cfg = Config(
        domain="example.com",
        services=ServicesConfig(management=True, cloud=True, media=False),
    )
    seed_oidc_secrets(cfg, tmp_path)
    generate_configs(cfg, tmp_path)
    s3cfg = tmp_path / "generated" / "seaweedfs-s3.json"
    assert s3cfg.exists()
    body = s3cfg.read_text()
    assert "mykey" in body
    assert "mysecret" in body
    assert stat.S_IMODE(s3cfg.stat().st_mode) == 0o600


def test_env_escape_dollar():
    assert _env_escape("pa$$word") == "'pa$$word'"


def test_env_escape_dollar_round_trips_through_dotenv(tmp_path: Path):
    from dotenv import dotenv_values

    path = tmp_path / ".env"
    path.write_text(render_env({"ADMIN_TOKEN": "$argon2id$v=19$m=19456,t=2,p=1$hash"}))

    assert dotenv_values(path)["ADMIN_TOKEN"] == "$argon2id$v=19$m=19456,t=2,p=1$hash"


def test_env_escape_dollar_round_trips_through_docker_compose(tmp_path: Path):
    import shutil
    import subprocess

    if shutil.which("docker") is None:
        pytest.skip("docker compose CLI unavailable")
    value = "$argon2id$v=19$m=19456,t=2,p=1$hash"
    env = tmp_path / ".env"
    env.write_text(render_env({"ADMIN_TOKEN": value}))
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        "services:\n  vault:\n    image: busybox:stable\n    environment:\n      ADMIN_TOKEN: ${ADMIN_TOKEN}\n"
    )

    daemon = subprocess.run(["docker", "info"], capture_output=True, timeout=15, check=False)
    if daemon.returncode != 0:
        pytest.skip("docker daemon unavailable")

    result = subprocess.run(
        [
            "docker",
            "compose",
            "-f",
            str(compose),
            "--env-file",
            str(env),
            "run",
            "--rm",
            "--no-deps",
            "vault",
            "sh",
            "-c",
            f'test "$ADMIN_TOKEN" = {value!r}',
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stderr


def test_env_escape_hash():
    result = _env_escape("test#value")
    assert "#" not in result or result.startswith('"')


def test_env_escape_backtick():
    result = _env_escape("abc`def")
    assert "`" not in result or result.startswith('"')
    assert "\\`" in result


def test_env_escape_empty():
    assert _env_escape("") == ""


def test_recyclarr_not_generated(tmp_path, seed_oidc_secrets):
    """Recyclarr uses its own defaults, toolkit does not generate recyclarr.yml."""
    from toolkit.core.config.config import Config
    from toolkit.core.generate.generate import generate_configs

    cfg = Config(domain="example.com")
    seed_oidc_secrets(cfg, tmp_path)
    generate_configs(cfg, tmp_path)
    recyclarr = tmp_path / "generated" / "recyclarr.yml"
    assert not recyclarr.exists(), "recyclarr.yml should not be generated — recyclarr manages its own config"


def test_env_full_generation(full_config, tmp_path):
    """Generated .env must be sourceable by bash without errors."""
    import subprocess

    secrets = {"POSTGRES_PASSWORD": "test123", "FRP_TOKEN": "abc$def#ghi"}
    env = _build_env_vars(full_config, "infra", secrets, tmp_path)
    content = render_env(env)

    env_file = tmp_path / "test.env"
    env_file.write_text(content)

    result = subprocess.run(
        ["bash", "-c", f"set -a && source {env_file} && set +a"],
        capture_output=True,
        text=True,
        timeout=5,
    )
    assert result.returncode == 0, f"bash source failed: {result.stderr}"


def test_env_no_duplicate_keys(full_config, tmp_path):
    """No duplicate keys in generated .env."""
    secrets = {"POSTGRES_PASSWORD": "test"}
    env = _build_env_vars(full_config, "infra", secrets, tmp_path)
    content = render_env(env)

    keys = [line.split("=", 1)[0] for line in content.strip().split("\n") if "=" in line and not line.startswith("#")]
    assert len(keys) == len(set(keys)), f"Duplicate keys: {[k for k in keys if keys.count(k) > 1]}"


def test_authelia_yaml_special_chars(tmp_path, seed_oidc_secrets):
    """Authelia config must be generated and contain expected structure."""
    cfg = Config(domain="example.com")
    seed_oidc_secrets(cfg, tmp_path)
    generate_configs(cfg, tmp_path)

    authelia_file = tmp_path / "generated" / "authelia.yml"
    assert authelia_file.exists()

    content = authelia_file.read_text()
    # Authelia uses its own template syntax ({{ secret ... }}) so full
    # yaml.safe_load will fail; verify structural markers instead.
    assert "server:" in content
    assert "authentication_backend:" in content
    assert "implementation: 'lldap'" in content
    assert "cn=ldap-bind" in content
    assert "claims_policies:" in content
    assert "attribute: 'vaultwarden_roles'" in content
    assert "access_control:" in content
    assert stat.S_IMODE(authelia_file.stat().st_mode) == 0o600


def test_generated_credential_files_are_owner_only(tmp_path: Path, seed_oidc_secrets):
    cfg = Config(domain="example.com")
    seed_oidc_secrets(cfg, tmp_path)
    generate_configs(cfg, tmp_path)
    sensitive = [
        tmp_path / "generated" / "redis.conf",
        tmp_path / "generated" / "authelia" / "configuration.yml",
        tmp_path / "generated" / "authelia" / "oidc-client-hashes.yml",
        tmp_path / "generated" / "headscale" / "config.yaml",
        tmp_path / "generated" / "wazuh" / "internal_users.yml",
    ]
    for path in sensitive:
        assert path.is_file(), path
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path

    redis_health = (tmp_path / "generated" / "redis-healthcheck.sh").read_text()
    assert "/run/redis/redis.conf" in redis_health


def test_caddyfile_routes(tmp_path, seed_oidc_secrets):
    """Caddyfile must include routes for enabled services."""
    cfg = Config(domain="example.com")
    seed_oidc_secrets(cfg, tmp_path)
    generate_configs(cfg, tmp_path)

    caddyfile = tmp_path / "generated" / "Caddyfile"
    assert caddyfile.exists()
    content = caddyfile.read_text()

    assert "auth.example.com" in content
    assert "homelab.example.com" in content
    assert "grafana.example.com" in content or "monitor.example.com" in content
    assert "reverse_proxy" in content
    assert "request_header -Remote-User" in content
    grafana = _caddy_site(content, "grafana.example.com")
    assert "@outside_mesh not remote_ip 10.10.10.0/24 100.64.0.0/10" in grafana
    assert "respond @outside_mesh 404" in grafana


def test_caddyfile_gitea_registry_v2_without_forward_auth(tmp_path, seed_oidc_secrets):
    """Gitea OCI registry /v2 must bypass Authelia forward_auth."""
    cfg = Config(
        domain="example.com",
        services=ServicesConfig(management=True, cloud=True),
    )
    seed_oidc_secrets(cfg, tmp_path)
    generate_configs(cfg, tmp_path)
    content = (tmp_path / "generated" / "Caddyfile").read_text()
    git_block = _caddy_site(content, "git.example.com")
    marker = next(line.strip().split()[0] for line in git_block.splitlines() if " path /v2/*" in line)
    v2_start = git_block.index(f"handle {marker}")
    v2_end = git_block.index("handle {", v2_start)
    v2_section = git_block[v2_start:v2_end]
    assert "import authelia" not in v2_section
    assert "import authelia" in git_block[v2_end:]


def test_caddyfile_authelia_oidc_discovery_bypasses_forward_auth(tmp_path, seed_oidc_secrets):
    """Authelia OIDC discovery and token/JWKS endpoints must be public to OIDC clients."""
    cfg = Config(domain="example.com")
    seed_oidc_secrets(cfg, tmp_path)
    generate_configs(cfg, tmp_path)
    content = (tmp_path / "generated" / "Caddyfile").read_text()
    auth_block = _caddy_site(content, "auth.example.com")

    assert "reverse_proxy 10.10.10.10:9091" in auth_block
    assert "import authelia" not in auth_block


def test_service_oidc_issuer_vars_match_authelia_url(full_config, tmp_path):
    from toolkit.core.secrets.secrets import generate_all_secrets, get_required_secrets

    specs = get_required_secrets(full_config)
    secrets = generate_all_secrets(specs)
    env = _build_env_vars(full_config, "apps", secrets, root=tmp_path)
    issuer = "https://auth.test.example.com"

    assert env["VAULTWARDEN_SSO_AUTHORITY"] == issuer
    assert env["IMMICH_OAUTH_ISSUER_URL"] == issuer
    assert "AUTHELIA_ISSUER" not in env
    assert "AUTHELIA_URL" not in env


def test_service_oidc_variables_are_scoped_to_owning_nodes(full_config, tmp_path):
    infra = _build_env_vars(full_config, "infra", {}, root=tmp_path)
    apps = _build_env_vars(full_config, "apps", {}, root=tmp_path)

    assert infra["GRAFANA_OIDC_CLIENT_ID"] == "grafana"
    assert "GRAFANA_OIDC_CLIENT_ID" not in apps
    assert apps["VAULTWARDEN_SSO_CLIENT_ID"] == "vaultwarden"
    assert apps["VAULTWARDEN_SSO_AUTHORITY"] == "https://auth.test.example.com"
    assert "VAULTWARDEN_SSO_CLIENT_ID" not in infra


def test_caddyfile_infra_has_upstream_routes(tmp_path, seed_oidc_secrets):
    """Infra Caddyfile must proxy to other VM private IPs for non-local services."""
    cfg = Config(domain="example.com")
    seed_oidc_secrets(cfg, tmp_path)
    generate_configs(cfg, tmp_path)

    caddyfile = tmp_path / "generated" / "Caddyfile"
    assert caddyfile.exists()
    content = caddyfile.read_text()
    assert "reverse_proxy" in content
    assert "cloud.example.com" in content
    assert "reverse_proxy 10.10.10.12:8083" in content
    assert "vault.example.com" in content
    assert "reverse_proxy 10.10.10.12:8082" in content


def test_domain_to_base_dn():
    from toolkit.core.manifest.variables import domain_to_base_dn

    assert domain_to_base_dn("localhost") == "dc=home,dc=local"
    assert domain_to_base_dn("") == "dc=home,dc=local"
    assert domain_to_base_dn("example.com") == "dc=example,dc=com"
    assert domain_to_base_dn("sub.example.co.uk") == "dc=sub,dc=example,dc=co,dc=uk"


def test_service_url_vars_real_domain(sample_config, tmp_config):
    """Service URLs use https and correct subdomains for real domains."""
    from toolkit.core.config.config import save_config
    from toolkit.core.secrets.secrets import generate_all_secrets, get_required_secrets

    sample_config.domain = "example.com"
    sample_config.email = "admin@example.com"
    save_config(sample_config, tmp_config / "config.yaml")

    specs = get_required_secrets(sample_config)
    secrets = generate_all_secrets(specs)
    infra = _build_env_vars(sample_config, "infra", secrets)
    media = _build_env_vars(sample_config, "media", secrets)

    assert media["JELLYFIN_HOST"] == "jellyfin.example.com"
    assert "JELLYFIN_HOST" not in infra
    assert infra["NTFY_BASE_URL"] == "https://ntfy.example.com"
    assert infra["MAIL_HOSTNAME"] == "mail.example.com"
    assert infra["LLDAP_BASE_DN"] == "dc=example,dc=com"
    assert infra["LLDAP_HTTP_URL"] == "https://users.example.com"
    assert infra["AUTHELIA_DEFAULT_EMAIL"] == "admin@example.com"
    assert infra["PUBLIC_URL_PROTOCOL"] == "https"


def test_service_url_vars_localhost(sample_config, tmp_config):
    """Service URLs use http for localhost domain."""
    from toolkit.core.config.config import save_config
    from toolkit.core.secrets.secrets import generate_all_secrets, get_required_secrets

    sample_config.domain = "localhost"
    save_config(sample_config, tmp_config / "config.yaml")

    specs = get_required_secrets(sample_config)
    secrets = generate_all_secrets(specs)
    env = _build_env_vars(sample_config, "infra", secrets)

    assert env["PUBLIC_URL_PROTOCOL"] == "http"
    assert env["NTFY_BASE_URL"] == "http://ntfy.localhost"
    assert env["LLDAP_BASE_DN"] == "dc=home,dc=local"


def test_cf_api_token_mirrors_cloudflare(sample_config, tmp_config):
    """CF_API_TOKEN mirrors CLOUDFLARE_API_TOKEN from secrets."""
    from toolkit.core.config.config import save_config
    from toolkit.core.secrets.secrets import generate_all_secrets, get_required_secrets

    save_config(sample_config, tmp_config / "config.yaml")
    specs = get_required_secrets(sample_config)
    secrets = generate_all_secrets(specs)
    secrets["CLOUDFLARE_API_TOKEN"] = "test-cf-token-123"
    env = _build_env_vars(sample_config, "infra", secrets)

    assert env["CF_API_TOKEN"] == "test-cf-token-123"


def test_cf_runtime_alias_cannot_be_supplied_as_a_stored_secret(sample_config):
    env = _build_env_vars(sample_config, "infra", {"CF_API_TOKEN": "runtime-alias"})

    assert env["CF_API_TOKEN"] == ""
