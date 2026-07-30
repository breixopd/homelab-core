"""Unit tests for crowdsec plugin verify()."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import yaml
from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ExternalHost, ServicesConfig


def _plugin():
    return load_plugin("crowdsec").CrowdSecPlugin()


def _cfg() -> Config:
    return Config(domain="example.com", services=ServicesConfig(security=True, cloud=True))


def test_agent_machine_name_is_stable_and_agent_status_is_fail_closed() -> None:
    from toolkit.services.crowdsec.plugin import _check_agents, crowdsec_agent_machine_name

    name = crowdsec_agent_machine_name("edge-01")
    assert name == "homelab-edge-01"
    assert _check_agents("[]", [name])[0].passed is False
    now = datetime.now(UTC).isoformat()
    checks = _check_agents(
        json.dumps([{"machineId": name, "isValidated": True, "last_heartbeat": now}]),
        [name],
    )
    assert checks[0].passed
    assert not _check_agents(
        json.dumps([{"name": name, "validated": True, "last_heartbeat": now}]),
        [name],
    )[0].passed

    future = (datetime.now(UTC) + timedelta(minutes=6)).isoformat()
    assert not _check_agents(
        json.dumps([{"machineId": name, "isValidated": True, "last_heartbeat": future}]),
        [name],
    )[0].passed
    stale = (datetime.now(UTC) - timedelta(minutes=16)).isoformat()
    assert not _check_agents(
        json.dumps([{"machineId": name, "isValidated": True, "last_heartbeat": stale}]),
        [name],
    )[0].passed


def test_agent_ranges_use_mesh_cidr_for_fleet_hosts_before_join() -> None:
    from toolkit.services.crowdsec.plugin import _crowdsec_agent_ranges

    cfg = _cfg().model_copy(
        update={
            "external_hosts": [
                ExternalHost(
                    name="edge-01",
                    ip="198.51.100.4",
                    kind="fleet",
                    services=["crowdsec-agent"],
                )
            ]
        }
    )
    assert _crowdsec_agent_ranges(cfg) == [cfg.network.mesh_ipv4_cidr]


def test_agent_ranges_use_management_ip_for_plain_hosts() -> None:
    from toolkit.services.crowdsec.plugin import _crowdsec_agent_ranges

    cfg = _cfg().model_copy(
        update={
            "external_hosts": [
                ExternalHost(name="edge-01", ip="198.51.100.4", services=["crowdsec-agent"]),
            ]
        }
    )
    assert _crowdsec_agent_ranges(cfg) == ["198.51.100.4/32"]


def test_agent_role_pins_supply_chain_and_keeps_enrollment_secret() -> None:
    root = Path(__file__).parents[3]
    role = root / "automation" / "ansible" / "roles" / "crowdsec_agent"
    defaults = yaml.safe_load((role / "defaults" / "main.yml").read_text(encoding="utf-8"))
    tasks = yaml.safe_load((role / "tasks" / "main.yml").read_text(encoding="utf-8"))
    by_name = {task["name"]: task for task in tasks}

    assert defaults["crowdsec_package_version"] == "1.7.8"
    assert len(defaults["crowdsec_package_key_sha256"]) == 64
    key_task = by_name["Add CrowdSec apt repository key"]["ansible.builtin.get_url"]
    assert "crowdsec_package_key_sha256" in key_task["checksum"]
    install = by_name["Install CrowdSec agent (non-interactive)"]["ansible.builtin.apt"]
    assert "crowdsec_package_version" in install["name"]

    refresh = by_name["Enroll when CrowdSec machine credentials are missing or rejected"]
    refresh_tasks = {task["name"]: task for task in refresh["block"]}
    register = refresh_tasks["Register the agent with the LAPI using the private auto-registration token"]
    assert register["no_log"] is True
    command = register["ansible.builtin.shell"]["cmd"]
    assert "--url" in command and "--machine" in command and "--file" in command
    assert '--token "$CROWDSEC_LAPI_TOKEN"' in command
    assert "crowdsec_lapi_token" not in command
    assert register["environment"] == {"CROWDSEC_LAPI_TOKEN": "{{ crowdsec_lapi_token }}"}
    assert by_name["Probe existing CrowdSec machine credentials"]["failed_when"] is False
    assert "Remove stale CrowdSec machine credentials for self-healing enrollment" not in by_name
    assert "crowdsec_credentials_rejected" in refresh["when"]
    rejection = by_name["Classify confirmed CrowdSec credential rejection"]["ansible.builtin.set_fact"]
    assert "401|403" in rejection["crowdsec_credentials_rejected"]
    transport_guard = by_name["Require encrypted or private-mesh LAPI transport"]["ansible.builtin.assert"]["that"][0]
    assert "urlsplit('hostname')" in transport_guard
    assert "^http://(10" not in transport_guard
    assert ")$'" in transport_guard
    pattern_literal = re.search(r"is match\(\s*'([^']+)'", transport_guard)
    assert pattern_literal is not None
    private_host_pattern = pattern_literal.group(1).replace("\\\\", "\\")
    assert re.fullmatch(private_host_pattern, "10.0.0.5")
    assert re.fullmatch(private_host_pattern, "192.168.1.8")
    assert re.fullmatch(private_host_pattern, "172.31.255.254")
    assert re.fullmatch(private_host_pattern, "100.64.0.1")
    assert not re.fullmatch(private_host_pattern, "10.attacker.example")
    assert not re.fullmatch(private_host_pattern, "100.128.0.1")
    promote = refresh_tasks["Promote validated CrowdSec machine credentials"]["ansible.builtin.copy"]
    assert promote["remote_src"] is True
    assert promote["mode"] == "0600"


def test_container_health_proves_lapi_is_ready_on_the_service_network() -> None:
    root = Path(__file__).parents[3]
    compose = yaml.safe_load((root / "toolkit/services/crowdsec/compose.yaml").read_text(encoding="utf-8"))
    command = compose["services"]["crowdsec"]["healthcheck"]["test"][1]

    assert "hostname -i" in command
    assert "127.0.0.1:8080" not in command
    assert "cscli lapi status" in command


class TestCrowdsecVerify:
    def test_lapi_bouncers_and_collections(self, tmp_path, monkeypatch):
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_curl",
            lambda *_a, **_k: (0, json.dumps({"status": "up"})),
        )

        def fake_cscli(_cfg, _ip, _root, args, **_kw):
            if args[0] == "metrics":
                return 0, "ok"
            if args[0] == "bouncers":
                return 0, json.dumps(
                    [{"name": "caddy@10.10.10.10", "revoked": False, "last_pull": datetime.now(UTC).isoformat()}]
                )
            if args[0] == "collections":
                return 0, " crowdsecurity/caddy\n crowdsecurity/base-http-scenarios\n"
            return 1, ""

        monkeypatch.setattr("toolkit.services.sdk.crowdsec_cscli", fake_cscli)

        checks = {c.check: c for c in _plugin().verify(_cfg(), {}, "10.10.10.10", tmp_path)}
        assert checks["local-api-health"].passed
        assert checks["bouncers"].passed
        assert checks["collections"].passed

    def test_bouncer_stale_fails(self, tmp_path, monkeypatch):
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_a, **_k: (0, '{"status":"up"}'))
        monkeypatch.setattr(
            "toolkit.services.sdk.crowdsec_cscli",
            lambda _cfg, _ip, _root, args, **_kw: (
                (
                    0,
                    json.dumps(
                        [
                            {
                                "name": "caddy@10.10.10.10",
                                "revoked": False,
                                "last_pull": (datetime.now(UTC) - timedelta(hours=2)).isoformat(),
                            }
                        ]
                    ),
                )
                if args[0] == "bouncers"
                else (0, "ok")
            ),
        )

        checks = {c.check: c for c in _plugin().verify(_cfg(), {}, "10.10.10.10", tmp_path)}
        assert checks["bouncers"].passed is False

    def test_bouncer_uses_fresh_registration_when_old_registration_remains(self):
        from toolkit.services.crowdsec.plugin import _check_bouncers

        now = datetime.now(UTC)
        output = json.dumps(
            [
                {
                    "name": "caddy",
                    "ip_address": "172.31.250.2",
                    "revoked": False,
                    "last_pull": (now - timedelta(hours=5)).isoformat(),
                },
                {
                    "name": "caddy@172.31.126.193",
                    "ip_address": "172.31.126.193",
                    "revoked": False,
                    "last_pull": (now - timedelta(seconds=10)).isoformat(),
                },
            ]
        )

        passed, detail = _check_bouncers(output)

        assert passed, detail
        assert "last pull" in detail

    def test_bouncer_future_timestamp_fails(self):
        from toolkit.services.crowdsec.plugin import _check_bouncers

        future = (datetime.now(UTC) + timedelta(minutes=6)).isoformat()
        passed, detail = _check_bouncers(
            json.dumps([{"name": "caddy@10.10.10.10", "revoked": False, "last_pull": future}])
        )
        assert passed is False
        assert "future" in detail

    def test_empty_bouncers_fail_closed(self, tmp_path, monkeypatch):
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_a, **_k: (0, '{"status":"up"}'))
        monkeypatch.setattr(
            "toolkit.services.sdk.crowdsec_cscli",
            lambda _cfg, _ip, _root, args, **_kw: (0, "[]") if args[0] == "bouncers" else (0, "ok"),
        )

        checks = {c.check: c for c in _plugin().verify(_cfg(), {}, "10.10.10.10", tmp_path)}
        assert checks["bouncers"].passed is False
        assert "no bouncers" in checks["bouncers"].detail

    def test_bouncer_requires_ip_bound_name(self):
        from toolkit.services.crowdsec.plugin import _check_bouncers

        fresh = datetime.now(UTC).isoformat()
        output = json.dumps([{"name": "caddy@not-an-ip", "revoked": False, "last_pull": fresh}])
        passed, detail = _check_bouncers(output)
        assert not passed
        assert "no bouncers" in detail

    @pytest.mark.parametrize("address_field", ["ip", "address", "ip_address", "ipAddress"])
    def test_bouncer_accepts_current_separate_name_and_address_schema(self, address_field):
        from toolkit.services.crowdsec.plugin import _check_bouncers

        row = {
            "name": "caddy",
            address_field: "10.10.10.10",
            "valid": True,
            "last_api_pull": datetime.now(UTC).isoformat(),
        }
        passed, detail = _check_bouncers(json.dumps([row]))
        assert passed, detail

    def test_bouncer_requires_explicit_validity_and_address(self):
        from toolkit.services.crowdsec.plugin import _check_bouncers

        fresh = datetime.now(UTC).isoformat()
        missing_validity = [{"name": "caddy", "ip": "10.10.10.10", "last_pull": fresh}]
        missing_address = [{"name": "caddy", "revoked": False, "last_pull": fresh}]
        assert not _check_bouncers(json.dumps(missing_validity))[0]
        assert not _check_bouncers(json.dumps(missing_address))[0]

    def test_bouncer_rejects_invalid_or_conflicting_identity_and_auth_fields(self):
        from toolkit.services.crowdsec.plugin import _check_bouncers

        fresh = datetime.now(UTC).isoformat()
        rows = [
            {"name": "caddy", "ip": "not-an-ip", "valid": True, "last_pull": fresh},
            {"name": "caddy", "ip": "10.10.10.10", "revoked": False, "valid": False, "last_pull": fresh},
            {
                "name": "caddy@10.10.10.10",
                "ip": "10.10.10.11",
                "valid": True,
                "last_pull": fresh,
            },
        ]
        for row in rows:
            assert not _check_bouncers(json.dumps([row]))[0]

    def test_compose_registers_only_the_caddy_bouncer_key(self):
        compose = yaml.safe_load((Path(__file__).parents[3] / "toolkit/services/crowdsec/compose.yaml").read_text())
        environment = compose["services"]["crowdsec"]["environment"]
        assert "BOUNCER_KEY_caddy" in environment
        assert "CROWDSEC_CADDY_BOUNCER_KEY" not in environment

    def test_revoked_bouncer_fails(self):
        from toolkit.services.crowdsec.plugin import _check_bouncers

        output = json.dumps(
            [{"name": "caddy@10.10.10.10", "revoked": True, "last_pull": datetime.now(UTC).isoformat()}]
        )
        passed, detail = _check_bouncers(output)
        assert not passed
        assert "invalid" in detail
