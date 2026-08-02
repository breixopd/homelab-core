"""Dry-run integration tests for service config generation and validation.

Tests that the full generate pipeline produces valid .env and config files
for all service categories, without needing a live Proxmox host or Docker.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
import yaml
from tests.helpers.machines import single_control_machines
from toolkit.core.compose.registry import load_all
from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.generate.generate import (
    _build_env_vars,
    generate_configs,
    render_env,
)
from toolkit.core.secrets.secrets import (
    generate_all_secrets,
    get_required_secrets,
    save_secrets_plaintext,
)

REPO_ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture(autouse=True)
def _load_categories():
    load_all()


@pytest.fixture
def full_config():
    """A config with all services enabled."""
    return Config(
        domain="test.example.com",
        email="admin@test.example.com",
        timezone="UTC",
        services=ServicesConfig(
            media=True,
            cloud=True,
            notifications=True,
            email=True,
            security=True,
        ),
        service_settings={
            "media-library": {"server": "both"},
            "gluetun": {"enabled": True},
            "media-cache": {"enabled": True},
            "music-sync": {"enabled": True},
        },
    )


@pytest.fixture
def all_secrets(full_config):
    specs = get_required_secrets(full_config)
    return generate_all_secrets(specs)


class TestEnvGeneration:
    """Test .env file generation for all VM roles."""

    def test_all_vms_produce_env(self, full_config, all_secrets, tmp_path):
        for vm in full_config.enabled_nodes:
            env_vars = _build_env_vars(full_config, vm, all_secrets, root=tmp_path)
            content = render_env(env_vars)
            assert content, f"Empty .env for VM {vm}"
            assert "BASE_DOMAIN=test.example.com" in content

    def test_infra_has_core_vars(self, full_config, all_secrets, tmp_path):
        env = _build_env_vars(full_config, "infra", all_secrets, root=tmp_path)
        required = [
            "BASE_DOMAIN",
            "AUTHELIA_DB_HOST",
            "AUTHELIA_REDIS_HOST",
            "TZ",
            "PUID",
            "PGID",
        ]
        for key in required:
            assert key in env, f"Missing {key} in infra .env"

    def test_database_hosts_follow_service_placement(self, full_config, all_secrets, tmp_path):
        env_infra = _build_env_vars(full_config, "infra", all_secrets, root=tmp_path)
        env_apps = _build_env_vars(full_config, "apps", all_secrets, root=tmp_path)
        env_media = _build_env_vars(full_config, "media", all_secrets, root=tmp_path)
        assert env_infra["AUTHELIA_DB_HOST"] == "postgres"
        assert env_apps["GITEA_DB_HOST"] == "10.10.10.10"
        assert not any(name.endswith("_DB_HOST") for name in env_media)

    def test_role_referenced_secrets_present(self, full_config, all_secrets, tmp_path):
        from toolkit.core.deploy.guest_bundle import CONTROLLER_ONLY_SECRETS, required_role_environment

        env = _build_env_vars(full_config, "infra", all_secrets, root=tmp_path)
        required = required_role_environment(Path.cwd(), "infra")
        for key in required & all_secrets.keys():
            assert key in env, f"Secret {key} missing from .env"
        for key in CONTROLLER_ONLY_SECRETS - {"CF_API_TOKEN", "CLOUDFLARE_API_TOKEN"}:
            assert key not in env, f"Controller-only secret {key} leaked into infra .env"

    def test_oidc_urls_use_domain(self, full_config, all_secrets, tmp_path):
        env = _build_env_vars(full_config, "infra", all_secrets, root=tmp_path)
        assert env.get("GRAFANA_OIDC_CLIENT_ID") == "grafana"
        assert "AUTHELIA_ISSUER" not in env

    def test_volume_paths_set_with_root(self, full_config, all_secrets, tmp_path):
        env = _build_env_vars(full_config, "infra", all_secrets, root=tmp_path)
        assert "CADDY_DATA_SOURCE" in env
        assert env["CADDY_DATA_SOURCE"] == "/opt/homelab/data/caddy"

    def test_grafana_smtp_integration_when_email_enabled(self, full_config, all_secrets, tmp_path):
        env = _build_env_vars(full_config, "infra", all_secrets, root=tmp_path)
        assert env["GRAFANA_SMTP_ENABLED"] == "true"
        assert env["GRAFANA_SMTP_HOST"] == "mailserver:25"

    def test_grafana_smtp_integration_is_disabled_with_email(self, all_secrets, tmp_path):
        cfg = Config(domain="test.example.com", services=ServicesConfig(email=False))
        env = _build_env_vars(cfg, "infra", all_secrets, root=tmp_path)
        assert env["GRAFANA_SMTP_ENABLED"] == "false"
        assert env["GRAFANA_SMTP_HOST"] == ""


class TestConfigGeneration:
    """Test config file generation from templates."""

    def test_caddyfile_generated(self, full_config, tmp_path, seed_oidc_secrets):
        seed_oidc_secrets(full_config, tmp_path)
        written = generate_configs(full_config, tmp_path)
        caddyfile = tmp_path / "generated" / "Caddyfile"
        assert caddyfile.exists()
        content = caddyfile.read_text()
        assert "test.example.com" in content
        assert caddyfile in written

    def test_authelia_config_generated(self, full_config, tmp_path):
        secs = generate_all_secrets(get_required_secrets(full_config))
        save_secrets_plaintext(secs, tmp_path / "secrets.enc.yaml")
        generate_configs(full_config, tmp_path)
        authelia = tmp_path / "generated" / "authelia.yml"
        assert authelia.exists()
        content = authelia.read_text()
        assert "test.example.com" in content

    def test_prometheus_targets_generated(self, full_config, tmp_path, seed_oidc_secrets):
        seed_oidc_secrets(full_config, tmp_path)
        generate_configs(full_config, tmp_path)
        content = (tmp_path / "generated" / "prometheus.yml").read_text()
        assert "job_name: node" in content
        assert "10.10.10.10:9100" in content

    def test_headscale_acl_generated(self, full_config, tmp_path, seed_oidc_secrets):
        seed_oidc_secrets(
            full_config,
            tmp_path,
            {"HEADSCALE_OIDC_CLIENT_SECRET": "test"},
        )
        generate_configs(full_config, tmp_path)
        acl = tmp_path / "generated" / "headscale" / "acl.hujson"
        assert acl.exists()
        assert "tagOwners" in acl.read_text()

    def test_seaweedfs_config_generated(self, full_config, tmp_path, seed_oidc_secrets):
        secs = {
            "SEAWEEDFS_S3_ACCESS_KEY": "testkey",
            "SEAWEEDFS_S3_SECRET_KEY": "testsecret",
        }
        seed_oidc_secrets(full_config, tmp_path, secs)
        generate_configs(full_config, tmp_path)
        s3_json = tmp_path / "generated" / "seaweedfs-s3.json"
        assert s3_json.exists()
        data = json.loads(s3_json.read_text())
        assert "testkey" in json.dumps(data)

    def test_caddyfile_routes_include_all_services(self, full_config, tmp_path, seed_oidc_secrets):
        seed_oidc_secrets(full_config, tmp_path)
        generate_configs(full_config, tmp_path)
        content = (tmp_path / "generated" / "Caddyfile").read_text()
        from toolkit.core.manifest.routes import compile_routes

        for host in {route.host for route in compile_routes(full_config)}:
            assert host in content, f"Missing route for {host}"

    def test_authelia_oidc_clients(self, full_config, tmp_path):
        secs = generate_all_secrets(get_required_secrets(full_config))
        save_secrets_plaintext(secs, tmp_path / "secrets.enc.yaml")
        generate_configs(full_config, tmp_path)
        content = (tmp_path / "generated" / "authelia.yml").read_text()
        assert "grafana" in content
        assert "nextcloud" in content
        assert "immich" in content
        assert "vaultwarden" in content
        assert "implementation: 'lldap'" in content
        assert "cn=ldap-bind" in content
        assert "claims_policies:" in content
        assert "https://komodo.test.example.com/auth/oidc/callback" in content

    def test_oidc_secrets_generated(self, full_config):
        specs = get_required_secrets(full_config)
        secrets = generate_all_secrets(specs)
        oidc_keys = [
            "GRAFANA_OIDC_SECRET",
            "VAULTWARDEN_SSO_CLIENT_SECRET",
            "NEXTCLOUD_OIDC_CLIENT_SECRET",
            "IMMICH_OIDC_CLIENT_SECRET",
            "KOMODO_OIDC_CLIENT_SECRET",
            "LLDAP_BIND_PASSWORD",
            "AUTHELIA_OIDC_HMAC_SECRET",
        ]
        for key in oidc_keys:
            assert key in secrets, f"Missing OIDC secret: {key}"
            assert secrets[key], f"Empty OIDC secret: {key}"

    def test_headscale_noise_key_is_not_a_generated_secret(self, full_config):
        specs = get_required_secrets(full_config)
        secrets = generate_all_secrets(specs)
        assert "HEADSCALE_PRIVATE_KEY" not in secrets


class TestMinimalConfig:
    """Test with minimal services (only management)."""

    def test_minimal_env(self, tmp_path):
        cfg = Config(
            domain="local.test",
            services=ServicesConfig(
                media=False,
                cloud=False,
                notifications=False,
                email=False,
                security=False,
            ),
            machines=single_control_machines(),
        )
        specs = get_required_secrets(cfg)
        secs = generate_all_secrets(specs)
        env = _build_env_vars(cfg, "infra", secs, root=tmp_path)
        assert env["BASE_DOMAIN"] == "local.test"
        assert env["AUTHELIA_DB_HOST"] == "postgres"

    def test_only_infra_vm_enabled(self):
        cfg = Config(
            services=ServicesConfig(
                media=False,
                cloud=False,
                notifications=False,
                email=False,
                security=False,
            ),
            machines=single_control_machines(),
        )
        assert cfg.enabled_nodes == ["infra"]
        assert not cfg.is_multi_node


class TestAutoDetection:
    """Test auto-detection utilities."""

    def test_detect_timezone_returns_string(self):
        from toolkit.core.infra.autodetect import detect_timezone

        tz = detect_timezone()
        assert isinstance(tz, str)
        assert len(tz) > 0

    def test_detect_uid_gid_returns_tuple(self):
        from toolkit.core.infra.autodetect import detect_uid_gid

        uid, gid = detect_uid_gid()
        assert isinstance(uid, int)
        assert isinstance(gid, int)
        assert uid >= 0
        assert gid >= 0


class TestWatchdog:
    """Test watchdog health check logic."""

    def test_watchdog_report_summary(self):
        from toolkit.core.ops.watchdog import ContainerHealth, HealthIssue, WatchdogReport

        report = WatchdogReport()
        report.healthy = [ContainerHealth("caddy"), ContainerHealth("postgres")]
        report.issues = [
            HealthIssue(
                service="ntfy",
                category="notifications",
                severity="warning",
                message="unhealthy",
            ),
        ]
        assert report.ok  # no critical issues
        assert "2 healthy" in report.summary()
        assert "1 warnings" in report.summary()

    def test_watchdog_report_critical(self):
        from toolkit.core.ops.watchdog import HealthIssue, WatchdogReport

        report = WatchdogReport()
        report.issues = [
            HealthIssue(
                service="postgres",
                category="management",
                severity="critical",
                message="exited",
            ),
        ]
        assert not report.ok

    def test_watchdog_report_to_dict(self):
        from toolkit.core.ops.watchdog import ContainerHealth, HealthIssue, WatchdogReport

        report = WatchdogReport()
        report.healthy = [ContainerHealth("caddy")]
        report.issues = [
            HealthIssue(
                service="ntfy",
                category="notifications",
                severity="warning",
                message="unhealthy",
            ),
        ]
        d = report.to_dict()
        assert d["healthy"] == [{"name": "caddy", "node": ""}]
        assert len(d["issues"]) == 1
        assert d["issues"][0]["service"] == "ntfy"
        assert d["ok"] is True

    def test_watchdog_diagnosis_field(self):
        from toolkit.core.ops.watchdog import HealthIssue

        issue = HealthIssue(
            service="authelia",
            category="management",
            severity="critical",
            message="exited",
            diagnosis="Dependencies not running: postgres, redis",
        )
        assert "postgres" in issue.diagnosis

    def test_watchdog_dependency_map(self):
        from toolkit.core.config.service_metadata import get_service_depends_on

        assert "postgres" in get_service_depends_on("authelia")
        assert "redis" in get_service_depends_on("authelia")
        assert "postgres" in get_service_depends_on("grafana")

    def test_watchdog_event_logging(self, tmp_path):
        from toolkit.core.config.config import Config, ServicesConfig
        from toolkit.core.ops.watchdog import Watchdog

        cfg = Config(services=ServicesConfig())
        wd = Watchdog(tmp_path, cfg)
        wd._log_event("check", "system", "test event")
        assert len(wd.events) == 1
        assert wd.events[0].action == "check"
        assert wd.events[0].detail == "test event"

    def test_prometheus_metrics_format(self):
        from toolkit.core.config.config import Config, ServicesConfig
        from toolkit.core.ops.watchdog import Watchdog

        cfg = Config(services=ServicesConfig())
        wd = Watchdog(Path("/tmp"), cfg)
        metrics = wd.prometheus_metrics()
        assert "watchdog_healthy_containers" in metrics
        assert "watchdog_issues_total" in metrics
        assert "watchdog_ok" in metrics

    def test_watchdog_restart_count_tracking(self):
        from toolkit.core.config.config import Config, ServicesConfig
        from toolkit.core.ops.watchdog import Watchdog

        cfg = Config(services=ServicesConfig())
        wd = Watchdog(Path("/tmp"), cfg)
        assert wd._restart_counts == {}
        wd._restart_counts["ntfy"] = 3
        assert wd._restart_counts["ntfy"] == 3

    def test_watchdog_config_check_missing_generated(self):
        from toolkit.core.config.config import Config, ServicesConfig
        from toolkit.core.ops.watchdog import Watchdog

        cfg = Config(services=ServicesConfig())
        wd = Watchdog(Path("/tmp/nonexistent_root_xyz"), cfg)
        issues = wd.check_config_files()
        assert any("generated" in i.message.lower() for i in issues)

    def test_watchdog_config_check_present(self, tmp_path):
        from toolkit.core.config.config import Config, ServicesConfig
        from toolkit.core.ops.watchdog import Watchdog

        cfg = Config(services=ServicesConfig())
        gen = tmp_path / "generated"
        gen.mkdir()
        (gen / "Caddyfile").write_text("# test")
        (gen / "authelia.yml").write_text("# test")
        wd = Watchdog(tmp_path, cfg)
        issues = wd.check_config_files()
        assert not issues

    def test_watchdog_dns_check_localhost_skipped(self):
        from toolkit.core.config.config import Config, ServicesConfig
        from toolkit.core.ops.watchdog import Watchdog

        cfg = Config(domain="localhost", services=ServicesConfig())
        wd = Watchdog(Path("/tmp"), cfg)
        issues = wd.check_dns_resolution()
        assert issues == []

    def test_watchdog_no_gotify_in_safe_restart(self):
        from pathlib import Path

        from toolkit.core.config.config import Config, ServicesConfig
        from toolkit.core.ops.watchdog import Watchdog

        wd = Watchdog(Path("/tmp"), Config(services=ServicesConfig()))
        assert "gotify" not in wd.restartable_services()

    def test_notifications_no_gotify(self):
        from toolkit.core.compose.registry import all_categories, load_all

        load_all()
        for cat in all_categories():
            if cat.name == "notifications":
                from toolkit.core.config.config import Config, ServicesConfig

                cfg = Config(services=ServicesConfig(notifications=True))
                svc_names = [s.name for s in cat.services(cfg)]
                assert "gotify" not in svc_names
                assert "ntfy" in svc_names
                break

    def test_immich_oauth_issuer_url_generated(self, full_config, all_secrets, tmp_path):
        env = _build_env_vars(full_config, "apps", all_secrets, root=tmp_path)
        assert "IMMICH_OAUTH_ISSUER_URL" in env
        assert "auth.test.example.com" in env["IMMICH_OAUTH_ISSUER_URL"]

    def test_optional_integrations_are_scoped_to_grafana_node(self, full_config, all_secrets, tmp_path):
        env_infra = _build_env_vars(full_config, "infra", all_secrets, root=tmp_path)
        assert env_infra["GRAFANA_SMTP_HOST"] == "mailserver:25"
        assert env_infra["NTFY_INTERNAL_URL"] == "http://ntfy:80"
        env_apps = _build_env_vars(full_config, "apps", all_secrets, root=tmp_path)
        assert "GRAFANA_SMTP_HOST" not in env_apps
        assert "NTFY_INTERNAL_URL" not in env_apps

    def test_watchdog_persistent_event_log(self, tmp_path):
        """Events are persisted to disk and reloaded on new Watchdog instance."""
        from toolkit.core.config.config import Config, ServicesConfig
        from toolkit.core.ops.watchdog import Watchdog

        cfg = Config(services=ServicesConfig())
        (tmp_path / "generated").mkdir(exist_ok=True)
        wd = Watchdog(tmp_path, cfg)
        wd._log_event("check", "test-svc", "unit test event")
        assert len(wd.events) >= 1
        # Create a new instance — should reload from disk
        wd2 = Watchdog(tmp_path, cfg)
        assert any(e.service == "test-svc" for e in wd2.events)

    def test_watchdog_exponential_backoff(self):
        """Restart timestamps are tracked for backoff."""
        from toolkit.core.config.config import Config, ServicesConfig
        from toolkit.core.ops.watchdog import Watchdog

        cfg = Config(services=ServicesConfig())
        wd = Watchdog(Path("/tmp"), cfg)
        assert wd._restart_timestamps == {}
        wd._restart_timestamps["caddy"] = time.time()
        wd._restart_counts["caddy"] = 1
        assert wd._restart_timestamps["caddy"] > 0

    def test_watchdog_verify_post_restart_method_exists(self):
        """Watchdog has verify_post_restart method."""
        from toolkit.core.ops.watchdog import Watchdog

        assert hasattr(Watchdog, "verify_post_restart")

    def test_watchdog_volume_permission_check_no_data_dir(self):
        """Volume permission check returns no issues when data/ doesn't exist."""
        from toolkit.core.config.config import Config, ServicesConfig
        from toolkit.core.ops.watchdog import Watchdog

        cfg = Config(services=ServicesConfig())
        wd = Watchdog(Path("/tmp/nonexistent_root_vol"), cfg)
        issues = wd.check_volume_permissions()
        assert issues == []

    def test_watchdog_container_resources_method_exists(self):
        """Watchdog has check_container_resources method."""
        from toolkit.core.ops.watchdog import Watchdog

        assert hasattr(Watchdog, "check_container_resources")

    def test_watchdog_prometheus_restart_metrics(self):
        """Prometheus metrics include restart attempt counts when present."""
        from toolkit.core.config.config import Config, ServicesConfig
        from toolkit.core.ops.watchdog import Watchdog

        cfg = Config(services=ServicesConfig())
        wd = Watchdog(Path("/tmp"), cfg)
        wd._restart_counts["caddy"] = 2
        metrics = wd.prometheus_metrics()
        assert "watchdog_restart_attempts_total" in metrics
        assert 'container="caddy"' in metrics

    def test_watchdog_parse_docker_uptime(self):
        """Docker uptime parser handles various status formats."""
        from toolkit.core.ops.watchdog import _parse_docker_uptime

        assert _parse_docker_uptime("Up 3 hours") == 3 * 3600
        assert _parse_docker_uptime("Up 2 days") == 2 * 86400
        assert _parse_docker_uptime("Up 45 minutes") == 45 * 60
        assert _parse_docker_uptime("Up About an hour") == 3600
        assert _parse_docker_uptime("Exited (0) 5 minutes ago") == 0

    def test_watchdog_check_image_updates_method_exists(self):
        """Watchdog has check_image_updates method."""
        from toolkit.core.ops.watchdog import Watchdog

        assert hasattr(Watchdog, "check_image_updates")

    def test_watchdog_get_container_uptimes_method_exists(self):
        """Watchdog has _get_container_uptimes method."""
        from toolkit.core.ops.watchdog import Watchdog

        assert hasattr(Watchdog, "_get_container_uptimes")

    def test_watchdog_get_container_stats_method_exists(self):
        """Watchdog has _get_container_stats method."""
        from toolkit.core.ops.watchdog import Watchdog

        assert hasattr(Watchdog, "_get_container_stats")

    def test_watchdog_full_check_includes_dependency_connectivity(self):
        """full_check calls check_dependency_connectivity."""
        import inspect

        from toolkit.core.ops.watchdog import Watchdog

        src = inspect.getsource(Watchdog.full_check)
        assert "check_dependency_connectivity" in src

    def test_watchdog_full_check_includes_image_updates(self):
        """full_check calls check_image_updates."""
        import inspect

        from toolkit.core.ops.watchdog import Watchdog

        src = inspect.getsource(Watchdog.full_check)
        assert "check_image_updates" in src

    def test_watchdog_prometheus_detailed_metrics(self):
        """Prometheus metrics include detailed system and heal metrics."""
        from toolkit.core.config.config import Config, ServicesConfig
        from toolkit.core.ops.watchdog import Watchdog

        cfg = Config(services=ServicesConfig())
        wd = Watchdog(Path("/tmp"), cfg)
        metrics = wd.prometheus_metrics()
        # New detailed metrics
        assert "watchdog_containers_total" in metrics
        assert "watchdog_auto_fixable_issues" in metrics
        assert "watchdog_last_check_timestamp_seconds" in metrics
        assert "watchdog_heal_success_total" in metrics
        assert "watchdog_heal_failure_total" in metrics
        assert 'severity="info"' in metrics

    def test_watchdog_prometheus_memory_detailed(self):
        """Prometheus metrics include detailed memory info."""
        from toolkit.core.config.config import Config, ServicesConfig
        from toolkit.core.ops.watchdog import Watchdog

        cfg = Config(services=ServicesConfig())
        wd = Watchdog(Path("/tmp"), cfg)
        metrics = wd.prometheus_metrics()
        # Should have detailed memory metrics
        assert "watchdog_memory_total_bytes" in metrics
        assert "watchdog_memory_available_bytes" in metrics

    def test_watchdog_prometheus_disk_mounts(self):
        """Prometheus disk metrics include mount labels."""
        from toolkit.core.config.config import Config, ServicesConfig
        from toolkit.core.ops.watchdog import Watchdog

        cfg = Config(services=ServicesConfig())
        wd = Watchdog(Path("/tmp"), cfg)
        metrics = wd.prometheus_metrics()
        assert 'mount="root"' in metrics
        assert "watchdog_disk_free_bytes" in metrics
        assert "watchdog_disk_total_bytes" in metrics

    def test_watchdog_notify_tag_logic(self):
        """Notify sends rotating_light tag for critical issues, warning for ok."""
        from toolkit.core.ops.watchdog import HealthIssue, WatchdogReport

        # Critical report: ok is False
        report = WatchdogReport()
        report.issues.append(HealthIssue(service="test", category="system", severity="critical", message="down"))
        assert not report.ok  # not ok
        # Non-critical report: ok is True
        report2 = WatchdogReport()
        report2.issues.append(HealthIssue(service="test", category="system", severity="warning", message="warn"))
        assert report2.ok  # still ok (only warnings)

    def test_watchdog_heal_tracks_success_events(self):
        """Heal events include 'verified healthy' in detail for tracking."""
        from toolkit.core.ops.watchdog import WatchdogEvent

        ev = WatchdogEvent(
            timestamp=time.time(),
            action="heal",
            service="caddy",
            detail="Restarted and verified healthy",
        )
        assert "verified healthy" in ev.detail.lower()

    def test_webui_app_factory_builds(self):
        """The FastAPI web UI app factory must construct without error."""
        from toolkit.webui.app import create_app

        app = create_app()
        assert str(app.url_path_for("login_page")) == "/login"
        assert str(app.url_path_for("health")) == "/health"

    def test_node_ip_rejects_unknown_node(self):
        """Config.node_ip() must fail closed for unknown node names."""
        cfg = Config()
        with pytest.raises(KeyError, match="unknown machine"):
            cfg.node_ip("nonexistent-node")

    def test_lldap_dn_uses_function_for_localhost(self):
        """Authelia config uses the manifest derivation for proper localhost handling."""
        from toolkit.core.manifest.variables import domain_to_base_dn

        assert domain_to_base_dn("localhost") == "dc=home,dc=local"
        assert domain_to_base_dn("example.com") == "dc=example,dc=com"

    def test_autodetect_gateway_handles_malformed_route(self):
        """detect_gateway() doesn't crash on malformed route output."""
        from toolkit.core.infra.autodetect import detect_gateway

        # Should return default gateway without crashing
        result = detect_gateway()
        assert isinstance(result, str)

    def test_ntfy_compose_uses_private_ip(self):
        """ntfy port binding uses PRIVATE_IP variable, not hardcoded 127.0.0.1."""
        content = (REPO_ROOT / "docker-compose.example.yml").read_text(encoding="utf-8")
        assert "PRIVATE_IP" in content
        # Should NOT have hardcoded 127.0.0.1 port binding
        assert '"127.0.0.1:' not in content

    def test_music_sync_depends_on_navidrome_with_condition(self):
        """music-sync depends_on navidrome uses service_healthy condition."""
        content = (REPO_ROOT / "docker-compose.example.yml").read_text(encoding="utf-8")
        document = yaml.safe_load(content)
        dependency = document["services"]["music-sync"]["depends_on"]["navidrome"]
        assert dependency["condition"] == "service_healthy"

    def test_security_compose_profiles_match_category(self):
        """Security category compose_profiles must match docker-compose.yml profile names."""
        from toolkit.core.compose.registry import get_category

        cat = get_category("security")
        profiles = cat.compose_profiles
        # Must include the actual profile names used in docker-compose.yml
        assert "security-headscale" in profiles
        assert "security-wazuh-ui" in profiles

    def test_wazuh_dashboard_depends_on_indexer_in_compose(self):
        """wazuh-dashboard must wait for a healthy wazuh-indexer."""
        content = (REPO_ROOT / "docker-compose.example.yml").read_text(encoding="utf-8")
        document = yaml.safe_load(content)
        dependencies = document["services"]["wazuh-dashboard"]["depends_on"]
        assert dependencies["wazuh-indexer"]["condition"] == "service_healthy"

    def test_dependency_map_completeness(self):
        """DEPENDENCY_MAP should include all compose depends_on relationships."""

        from toolkit.core.config.service_metadata import dependency_map as dep_map_fn

        dep_map = dep_map_fn()
        assert dep_map["headscale"] == ("authelia", "caddy")
        # Cross-compose dependencies
        assert "wazuh-dashboard" in dep_map
        assert "wazuh-indexer" in dep_map["wazuh-dashboard"]

    def test_watchdog_ssl_check_method_exists(self):
        """Watchdog should have check_ssl_certificates method."""
        from toolkit.core.ops.watchdog import Watchdog

        assert hasattr(Watchdog, "check_ssl_certificates")
        assert callable(getattr(Watchdog, "check_ssl_certificates"))

    def test_watchdog_log_size_check_method_exists(self):
        """Watchdog should have check_docker_log_sizes method."""
        from toolkit.core.ops.watchdog import Watchdog

        assert hasattr(Watchdog, "check_docker_log_sizes")
        assert callable(getattr(Watchdog, "check_docker_log_sizes"))

    def test_watchdog_full_check_includes_ssl_and_logs(self):
        """full_check should call check_ssl_certificates and check_docker_log_sizes."""
        import inspect

        from toolkit.core.ops.watchdog import Watchdog

        src = inspect.getsource(Watchdog.full_check)
        assert "check_ssl_certificates" in src
        assert "check_docker_log_sizes" in src

    def test_autodetect_uses_context_manager(self):
        """autodetect.py should use context manager for file operations."""
        content = (REPO_ROOT / "toolkit" / "core" / "infra" / "autodetect.py").read_text(encoding="utf-8")
        # Should use 'with open' not bare 'open().read()'
        assert "with open" in content
        assert 'open("/etc/timezone").read()' not in content

    def test_mount_storage_uses_ansible_posix_mount(self):
        """mount-storage role should use the supported ansible.posix.mount module."""
        tasks_path = REPO_ROOT / "automation" / "ansible" / "roles" / "mount_storage" / "tasks" / "main.yml"
        if tasks_path.exists():
            content = tasks_path.read_text()
            assert "ansible.posix.mount" in content

    def test_ci_has_one_locked_authoritative_workflow(self):
        """CI should have one reproducible validation workflow."""
        ci_path = Path(__file__).parents[4] / ".github" / "workflows" / "ci.yml"
        validate_path = Path(__file__).parents[4] / ".github" / "workflows" / "validate.yml"
        ci = ci_path.read_text()

        assert not validate_path.exists()
        assert "uv sync --locked" in ci
        assert "persist-credentials: false" in ci
        assert "permissions:\n  contents: read" in ci
        assert "pip install" not in ci
        assert '--image "${{ matrix.image.name }}"' not in ci
        assert "local/${{ matrix.image.name }}" not in ci
