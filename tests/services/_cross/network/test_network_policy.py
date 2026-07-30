from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import yaml
from toolkit.core.config.config import Config
from toolkit.core.infra.network_policy import build_guest_firewall_policy, declared_service_ports
from toolkit.core.ops.preflight import run_preflight


def test_production_requires_a_trusted_proxmox_ca(tmp_path: Path):
    cfg = Config(
        domain="example.com",
        email="admin@example.com",
        proxmox={"api_url": "https://pve.example.test:8006"},
    )
    with patch(
        "toolkit.core.infra.proxmox_tls.ensure_proxmox_ca_bundle",
        side_effect=RuntimeError("CA discovery failed"),
    ):
        items = run_preflight(tmp_path, cfg, bootstrap=True)
    item = next(item for item in items if item.id == "proxmox_tls")
    assert item.ok is False
    assert item.detail == "CA discovery failed"


def test_firewall_policy_allows_public_ingress_only_on_infra():
    policy = build_guest_firewall_policy(
        Path.cwd(),
        {"services": {"email": False}},
    )

    public = [rule for rule in policy if rule["from_ip"] == "any"]
    assert {(rule["port"], tuple(rule["roles"])) for rule in public} == {
        (53, ("infra",)),
        (80, ("infra",)),
        (443, ("infra",)),
    }


def test_firewall_policy_compiles_public_mail_from_service_manifest() -> None:
    policy = build_guest_firewall_policy(Path.cwd(), {})

    public_mail = {
        (rule["port"], rule["proto"]) for rule in policy if rule["from_ip"] == "any" and "mailserver" in rule["comment"]
    }

    assert public_mail == {(25, "tcp"), (465, "tcp"), (587, "tcp"), (993, "tcp")}
    assert not any(rule["from_ip"] == "any" and rule["port"] == 4190 for rule in policy)


def test_firewall_policy_has_no_blanket_private_subnet_rule(tmp_path: Path):
    policy = build_guest_firewall_policy(tmp_path, {})

    assert not any(rule["from_ip"] == "10.10.10.0/24" and rule["port"] == "any" for rule in policy)
    assert any(rule["comment"] == "SSH from Proxmox gateway" for rule in policy)
    assert any(rule["comment"] == "SSH from enrolled mesh" for rule in policy)
    mesh_router_ssh = next(rule for rule in policy if rule["comment"] == "SSH through mesh subnet router")
    assert mesh_router_ssh["from_ip"] == "10.10.10.10"
    assert set(mesh_router_ssh["roles"]) == {"media", "apps"}


def test_firewall_policy_closes_public_ingress_and_dns_when_disabled() -> None:
    policy = build_guest_firewall_policy(
        Path.cwd(),
        {"network": {"expose_via_internet": False, "dns_public_access": False}},
    )

    assert not any(rule["from_ip"] == "any" for rule in policy)
    assert {(rule["from_ip"], rule["proto"]) for rule in policy if rule["port"] == 53} == {
        ("10.10.10.0/24", "tcp"),
        ("10.10.10.0/24", "udp"),
        ("100.64.0.0/10", "tcp"),
        ("100.64.0.0/10", "udp"),
    }
    assert {(rule["from_ip"], rule["port"]) for rule in policy if rule["port"] in {80, 443}} == {
        ("10.10.10.0/24", 80),
        ("10.10.10.0/24", 443),
        ("100.64.0.0/10", 80),
        ("100.64.0.0/10", 443),
    }


def test_firewall_policy_uses_typed_mesh_pool_and_machine_network() -> None:
    raw = Config().model_dump(mode="python")
    raw["network"].update(
        {
            "expose_via_internet": False,
            "dns_public_access": False,
            "mesh_ipv4_cidr": "100.100.0.0/16",
        }
    )
    for machine in raw["machines"].values():
        suffix = machine["address"].rsplit(".", 1)[1]
        machine.update({"address": f"10.20.16.{suffix}", "gateway": "10.20.16.1", "cidr": 20})

    policy = build_guest_firewall_policy(Path.cwd(), raw)

    internal_web_sources = {
        rule["from_ip"] for rule in policy if rule["port"] in {80, 443} and rule["from_ip"] != "any"
    }
    assert internal_web_sources == {"10.20.16.0/20", "100.100.0.0/16"}
    assert any(rule["comment"] == "SSH from Proxmox gateway" and rule["from_ip"] == "10.20.16.1" for rule in policy)


def test_firewall_policy_compiles_declared_cross_node_route(tmp_path: Path):
    service = tmp_path / "toolkit" / "services" / "example"
    service.mkdir(parents=True)
    (service / "service.yaml").write_text(
        """name: example
label: Example
description: Example service
icon: box
category: cloud
placement: apps
priority: 50
routes:
- upstream: example:8080
  published_port: 4567
  exposure: private
  auth:
    mode: forward_auth
"""
    )
    (service / "compose.yaml").write_text(
        f"services:\n  example:\n    image: example/service:1@sha256:{'a' * 64}\n"
        "    ports:\n      - ${PRIVATE_IP:-127.0.0.1}:4567:8080\n"
        "    logging:\n      driver: json-file\n      options:\n        max-size: 10m\n        max-file: '3'\n"
    )
    policy = build_guest_firewall_policy(
        tmp_path,
        {},
    )

    assert any(
        rule["roles"] == ["apps"]
        and rule["from_ip"] == "10.10.10.10"
        and rule["port"] == 4567
        and "example" in rule["comment"]
        for rule in policy
    )


def test_firewall_policy_limits_fmd_api_and_metrics_to_infra() -> None:
    policy = build_guest_firewall_policy(
        Path.cwd(),
        {},
    )

    fmd = [rule for rule in policy if rule["roles"] == ["apps"] and "fmd-server" in rule["comment"]]
    assert {(rule["from_ip"], rule["port"], rule["proto"]) for rule in fmd} == {
        ("10.10.10.10", 8084, "tcp"),
        ("10.10.10.10", 9101, "tcp"),
    }


def test_firewall_policy_allows_only_declared_route_ports_from_ingress() -> None:
    policy = build_guest_firewall_policy(Path.cwd(), {})

    apps_ports = {rule["port"] for rule in policy if rule["roles"] == ["apps"] and rule["from_ip"] == "10.10.10.10"}
    media_ports = {rule["port"] for rule in policy if rule["roles"] == ["media"] and rule["from_ip"] == "10.10.10.10"}

    assert {2283, 3000, 8082, 8083, 8084, 8333, 8888, 9101} <= apps_ports
    assert {4533, 5055, 6767, 7878, 8080, 8096, 8265, 8845, 8989, 9696} <= media_ports


def test_firewall_policy_limits_databases_to_actual_cross_node_consumers() -> None:
    policy = build_guest_firewall_policy(Path.cwd(), {})

    database_rules = [rule for rule in policy if rule["port"] in {5432, 6379}]

    assert database_rules
    assert {rule["from_ip"] for rule in database_rules} == {"10.10.10.12"}
    assert {tuple(rule["roles"]) for rule in database_rules} == {("infra",)}


def test_firewall_policy_keeps_unrouted_admin_ports_node_local() -> None:
    policy = build_guest_firewall_policy(Path.cwd(), {})

    assert not any(
        rule["from_ip"] in {"10.10.10.11", "10.10.10.12"} and rule["port"] in {3001, 9200} for rule in policy
    )
    assert {
        "roles": ["infra"],
        "from_ip": "10.10.10.11",
        "port": 8090,
        "proto": "tcp",
        "comment": "ntfy servarr-api",
    } in policy


def test_firewall_policy_allows_observability_only_to_agent_metrics() -> None:
    policy = build_guest_firewall_policy(Path.cwd(), {})

    agent_rules = [rule for rule in policy if rule["port"] in {8088, 9100}]

    assert {(tuple(rule["roles"]), rule["from_ip"], rule["port"]) for rule in agent_rules} == {
        (("infra",), "172.31.249.2", 9100),
        (("infra",), "172.31.250.2", 8088),
        (("apps",), "10.10.10.10", 8088),
        (("media",), "10.10.10.10", 8088),
        (("apps",), "10.10.10.10", 9100),
        (("media",), "10.10.10.10", 9100),
    }


def test_firewall_policy_allows_node_local_alloy_runtimes_to_loki() -> None:
    policy = build_guest_firewall_policy(Path.cwd(), {})

    loki_rules = [rule for rule in policy if rule["port"] == 3100]

    assert {(tuple(rule["roles"]), rule["from_ip"], rule["proto"]) for rule in loki_rules} == {
        (("infra",), "10.10.10.11", "tcp"),
        (("infra",), "10.10.10.12", "tcp"),
    }


def test_firewall_policy_follows_machine_capabilities_after_renaming() -> None:
    raw = Config().model_dump(mode="python")
    raw["machines"] = {
        "compute": raw["machines"]["apps"],
        "gateway": raw["machines"]["infra"],
        "stream": raw["machines"]["media"],
    }

    policy = build_guest_firewall_policy(Path.cwd(), raw)

    assert {
        "roles": ["compute"],
        "from_ip": "10.10.10.10",
        "port": 2283,
        "proto": "tcp",
        "comment": "gateway ingress to immich-server",
    } in policy
    assert {
        "roles": ["gateway"],
        "from_ip": "10.10.10.12",
        "port": 5432,
        "proto": "tcp",
        "comment": "compute services to integration postgres",
    } in policy
    assert not any(role in {"apps", "infra", "media"} for rule in policy for role in rule["roles"])


def test_firewall_policy_resolves_selected_fleet_integrations_to_mesh() -> None:
    policy = build_guest_firewall_policy(
        Path.cwd(),
        {
            "external_hosts": [
                {
                    "name": "edge-01",
                    "ip": "203.0.113.10",
                    "kind": "fleet",
                    "services": ["crowdsec-agent", "ldap-client", "wazuh-agent"],
                }
            ]
        },
    )

    mesh_rules = {(rule["port"], tuple(rule["roles"])) for rule in policy if rule["from_ip"] == "100.64.0.0/10"}
    assert {(8080, ("infra",)), (3890, ("infra",)), (1514, ("infra",)), (1515, ("infra",))} <= mesh_rules


def test_declared_ports_include_enabled_variants_and_cross_vm_storage() -> None:
    ports = declared_service_ports(Path.cwd(), Config())

    assert ("jellyfin-nvidia", 8096, "tcp") in ports["media"]
    assert not any(service.startswith("plex") for service, _port, _protocol in ports["media"])
    assert ("gluetun", 8080, "tcp") in ports["media"]
    assert ("seaweedfs", 8333, "tcp") in ports["apps"]
    assert ("seaweedfs", 8888, "tcp") in ports["apps"]


def test_firewall_policy_allows_project_port_only_from_infra(tmp_path: Path):
    policy = build_guest_firewall_policy(
        tmp_path,
        {
            "projects": {
                "entries": [
                    {
                        "subdomain": "status",
                        "auth_mode": "forward_auth",
                        "exposure": "private",
                        "docker_image": "example/status:1@sha256:" + "a" * 64,
                        "container_port": 45678,
                        "placement": "apps",
                    }
                ]
            },
        },
    )

    matching = [rule for rule in policy if rule["port"] == 45678]
    assert matching == [
        {
            "roles": ["apps"],
            "from_ip": "10.10.10.10",
            "port": 45678,
            "proto": "tcp",
            "comment": "infra to project status",
        }
    ]


def test_firewall_policy_allows_project_database_only_from_project_node() -> None:
    policy = build_guest_firewall_policy(
        Path.cwd(),
        {
            "projects": {
                "entries": [
                    {
                        "name": "Worker",
                        "subdomain": "worker",
                        "auth_mode": "forward_auth",
                        "exposure": "private",
                        "docker_image": "example/worker:1@sha256:" + "a" * 64,
                        "container_port": 8080,
                        "placement": "media",
                        "database_service": "dev-postgres",
                    }
                ]
            }
        },
    )

    assert {
        "roles": ["apps"],
        "from_ip": "10.10.10.11",
        "port": 5433,
        "proto": "tcp",
        "comment": "media projects to database dev-postgres",
    } in policy


def test_security_role_uses_generated_least_privilege_rules():
    tasks = Path("automation/ansible/roles/security_agent/tasks/main.yml").read_text()
    reconciler = Path("automation/ansible/roles/security_agent/templates/reconcile-ufw-policy.sh.j2").read_text()

    assert "homelab_firewall_rules" in reconciler
    assert "community.general.ufw" not in tasks
    assert "ufw show added" in reconciler
    assert "ufw --force reset" in reconciler
    assert "ufw --force enable" in reconciler
    assert "ufw allow out 1514" not in tasks


def test_docker_ingress_role_enforces_generated_policy_before_published_ports():
    tasks = Path("automation/ansible/roles/docker_firewall/tasks/main.yml").read_text()
    script = Path("automation/ansible/roles/docker_firewall/templates/apply-policy.sh.j2").read_text()
    unit = Path("automation/ansible/roles/docker_firewall/templates/homelab-docker-firewall.service.j2").read_text()

    assert "Render Docker published-port policy" in tasks
    assert "homelab_firewall_rules" in script
    assert "DOCKER-USER" in script
    assert "HOMELAB-INGRESS" in script
    assert "homelab_published_ports" in script
    assert "--ctorigdstport" in script
    assert '--ctorigdst "$ORIGIN_IP"' in script
    assert "--ctstate ESTABLISHED,RELATED" in script
    assert "-i docker0 -j RETURN" not in script
    assert "-i 'br+' -j RETURN" not in script
    assert "-j DROP" in script
    assert "After=docker.service" in unit
    assert "WantedBy=multi-user.target" in unit


def test_machine_driven_iac_attaches_public_interface_only_when_declared():
    source = Path("infrastructure/main.tf").read_text()
    assert 'for_each = each.value.public_bridge == ""' in source
    assert "each.value.public_bridge" in source
    assert "each.value.private_bridge" in source
    assert 'resource "proxmox_virtual_environment_container" "machine"' in source
    assert 'resource "proxmox_virtual_environment_vm" "machine"' in source
    assert 'resource "proxmox_virtual_environment_container" "infra"' not in source


def test_production_python_never_uses_shell_true():
    root = Path(__file__).resolve().parents[4]
    offenders = []
    for source_root in (root / "toolkit", root / "images", root / "scripts"):
        for path in source_root.rglob("*.py"):
            if "shell=True" in path.read_text(encoding="utf-8"):
                offenders.append(path.relative_to(root).as_posix())

    assert offenders == []


def test_repository_contains_no_ssh_private_keys():
    root = Path(__file__).resolve().parents[4]
    offenders = []
    for relative in ("automation", "config", "docs", "images", "infrastructure", "scripts", "toolkit"):
        for path in (root / relative).rglob("*"):
            if path.is_file() and path.stat().st_size <= 2 * 1024 * 1024:
                try:
                    content = path.read_text(encoding="utf-8")
                except UnicodeDecodeError:
                    continue
                if "BEGIN OPENSSH PRIVATE KEY" in content:
                    offenders.append(path.relative_to(root).as_posix())

    assert offenders == []


def test_vpn_container_uses_only_required_tunnel_capability():
    compose = yaml.safe_load(Path("toolkit/services/gluetun/compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["gluetun"]

    assert "privileged" not in service
    assert service["cap_add"] == ["NET_ADMIN"]
    assert service["devices"] == ["/dev/net/tun:/dev/net/tun"]
    assert service["security_opt"] == ["no-new-privileges:true"]


def test_browser_proxy_does_not_disable_container_security_profiles():
    compose = yaml.safe_load(Path("toolkit/services/flaresolverr/compose.yaml").read_text(encoding="utf-8"))
    service = compose["services"]["flaresolverr"]

    assert service["cap_drop"] == ["ALL"]
    assert "cap_add" not in service
    assert service["security_opt"] == ["no-new-privileges:true"]


def test_fresh_deploy_driver_gates_remote_wipe_behind_restore_drill():
    script_path = Path("scripts/validate-fresh-deploy.sh")
    script = script_path.read_text()

    assert script_path.stat().st_mode & 0o111
    assert "--confirm-remote-wipe" in script
    assert script.index("pre-wipe-restore-drill") < script.index("remote-redeploy")
    assert script.index("controller-start") < script.index("remote-redeploy")
    assert "HOMELAB_CONTROLLER_SOCKET" in script
    assert "cleanup_controller" in script
    assert "write_report" in script
    assert '--extra-vars "@$ROOT/automation/ansible/group_vars/generated.yml"' in script
