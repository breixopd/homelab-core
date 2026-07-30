from __future__ import annotations

import re
import stat
from pathlib import Path

import pytest
import yaml
from toolkit.core.config.config import Config
from toolkit.core.infra.iac_sync import render_ansible_generated_yml, render_generated_tfvars
from toolkit.core.manifest.catalog import load_service_catalog

ROOT = Path(__file__).resolve().parents[4]


def _link_service_catalog(root: Path) -> None:
    toolkit_dir = root / "toolkit"
    toolkit_dir.mkdir(parents=True, exist_ok=True)
    (toolkit_dir / "services").symlink_to(ROOT / "toolkit/services", target_is_directory=True)
    (toolkit_dir / "Dockerfile").symlink_to(ROOT / "toolkit/Dockerfile")


def test_tfvars_uses_arbitrary_machines_from_config():
    raw = {
        "domain": "example.com",
        "machines": {
            "control-east": {
                "kind": "lxc",
                "hostname": "control-01",
                "address": "10.0.0.20",
                "gateway": "10.0.0.1",
                "vmid": 820,
                "labels": ["control"],
            },
            "compute-west": {
                "kind": "vm",
                "hostname": "compute-07",
                "address": "10.0.0.27",
                "gateway": "10.0.0.1",
                "vmid": 827,
                "admin_user": "ubuntu",
                "cloud_image_datastore": "local",
                "cloud_image_format": "qcow2",
                "cloud_image_url": "https://cloud-images.ubuntu.com/noble/current/noble-server-cloudimg-amd64.img",
                "cloud_image_sha256": "b" * 64,
            },
        },
        "proxmox": {
            "ssh_public_key": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA test-key",
        },
    }
    hcl = render_generated_tfvars(raw)
    assert '"control-east" = {' in hcl
    assert re.search(r'address\s+= "10\.0\.0\.20"', hcl)
    assert '"compute-west" = {' in hcl
    assert re.search(r'kind\s+= "vm"', hcl)
    assert "enabled_lxcs" not in hcl
    assert "infra_config" not in hcl


def test_generated_iac_follows_renamed_machine_ids_and_additional_vm() -> None:
    raw = Config().model_dump(mode="python")
    original = raw.pop("machines")
    renamed = {
        "control-plane": {**original["infra"], "hostname": "control-plane-01"},
        "media-tier": {**original["media"], "hostname": "media-tier-01"},
        "apps-tier": {**original["apps"], "hostname": "apps-tier-01"},
        "compute-east": {
            **original["apps"],
            "kind": "vm",
            "hostname": "compute-east-01",
            "address": "10.10.10.25",
            "vmid": 825,
            "labels": ["compute"],
            "admin_user": "debian",
            "cloud_image_datastore": "local",
            "cloud_image_format": "qcow2",
            "cloud_image_url": "https://cloud-images.example.test/debian.qcow2",
            "cloud_image_sha256": "c" * 64,
            "data_disks": [],
        },
    }
    raw["machines"] = renamed
    raw["proxmox"]["ssh_public_key"] = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA test-key"

    tfvars = render_generated_tfvars(raw)
    generated = yaml.safe_load(render_ansible_generated_yml(raw))

    assert '"compute-east" = {' in tfvars
    assert re.search(r'kind\s+= "vm"', tfvars)
    assert generated["machine_ids"] == {
        "control-plane": 800,
        "media-tier": 801,
        "apps-tier": 802,
        "compute-east": 825,
    }
    assert generated["service_nodes"]["romm"] == "apps-tier"
    assert generated["service_nodes"]["music-sync"] == "media-tier"
    assert generated["service_nodes"]["postgres"] == "control-plane"


def test_tfvars_uses_authoritative_config_values():
    raw: dict = {
        "domain": "x.test",
        "proxmox": {
            "api_url": "https://custom:8006/api2/json",
            "ssh_public_key": "ssh-ed25519 AAA",
            "lxc_template_url": "https://images.example.test/debian.tar.zst",
            "lxc_template_checksum": "a" * 64,
            "lxc_template_datastore": "templates",
            "lxc_storage": "guest-disks",
        },
    }
    hcl = render_generated_tfvars(raw)
    # The /api2/json suffix is normalized to the BPG provider base URL.
    assert "https://custom:8006/" in hcl
    assert "ssh-ed25519 AAA" in hcl
    assert 'lxc_template_url       = "https://images.example.test/debian.tar.zst"' in hcl
    assert f'lxc_template_checksum  = "{"a" * 64}"' in hcl
    assert 'lxc_template_datastore = "templates"' in hcl
    assert re.search(r'default_datastore\s+= "guest-disks"', hcl)


def test_tfvars_are_emitted_in_canonical_assignment_layout():
    hcl = render_generated_tfvars(
        {
            "domain": "x.test",
            "proxmox": {"ssh_public_key": "ssh-ed25519 AAA"},
        }
    )

    assert 'lxc_template_url       = "http://download.proxmox.com/' in hcl
    assert 'lxc_template_checksum  = "ff5c55cba730fc1e93bc7de3e0ea4aecb05c692094009cfcf2999973a56f15e5"' in hcl
    assert 'lxc_template_datastore = "local"' in hcl
    assert re.search(r'default_datastore\s+= "local"', hcl)
    assert '    kind        = "lxc"' in hcl
    assert "    labels = [" in hcl
    assert re.search(r'template_file_id\s+= ""', hcl)
    assert re.search(r'admin_user\s+= ""', hcl)
    assert re.search(r"ssh_port\s+= 22", hcl)
    assert re.search(r'cloud_image_datastore\s+= ""', hcl)


def test_ansible_generated_includes_vpn_enabled():
    raw = {
        "domain": "lab.local",
        "service_settings": {"gluetun": {"enabled": True}},
        "proxmox": {"api_url": "https://192.168.5.1:8006/api2/json"},
    }
    yml = render_ansible_generated_yml(raw)
    assert "vpn_enabled: true" in yml
    assert "machine_ids:" in yml


def test_ansible_generated_declares_manifest_owned_guest_hooks() -> None:
    generated = yaml.safe_load(render_ansible_generated_yml({}, repo_root=ROOT))

    assert generated["service_guest_task_files"] == [
        "toolkit/services/wazuh-indexer/ansible/guest.yml",
        "toolkit/services/wazuh-dashboard/ansible/guest.yml",
        "toolkit/services/lldap/ansible/guest.yml",
        "toolkit/services/headscale/ansible/guest.yml",
    ]
    assert generated["service_guest_final_task_files"] == [
        "toolkit/services/adguard/ansible/guest-final.yml",
    ]
    assert generated["service_manager_task_files"] == [
        "toolkit/services/wazuh-indexer/ansible/manager.yml",
    ]
    assert generated["service_security_task_files"] == [
        "toolkit/services/wazuh-dashboard/ansible/security.yml",
    ]
    assert generated["service_sync_task_files"] == [
        "toolkit/services/lldap/ansible/sync.yml",
    ]
    assert generated["service_recovery_task_files"] == [
        "toolkit/services/lldap/ansible/recovery.yml",
        "toolkit/services/wazuh-dashboard/ansible/recovery.yml",
        "toolkit/services/wazuh-indexer/ansible/recovery.yml",
        "toolkit/services/headscale/ansible/recovery.yml",
        "toolkit/services/komodo-core/ansible/recovery.yml",
    ]
    assert generated["dns_service"] == "adguard"
    assert generated["dns_service_ip"] == generated["service_ips"]["adguard"]
    assert generated["registry_mirror_service"] == "registry-mirror"
    assert generated["registry_mirror_node"] == generated["service_nodes"]["registry-mirror"]
    assert generated["registry_mirror_ip"] == generated["service_ips"]["registry-mirror"]
    assert generated["registry_mirror_port"] == 3128
    assert generated["registry_mirror_enabled"] is True


def test_ansible_generated_uses_infrastructure_capabilities_without_service_aliases() -> None:
    generated = yaml.safe_load(render_ansible_generated_yml({}))

    assert generated["mesh_router_node"] == generated["service_nodes"]["headscale"]
    assert generated["vpn_node"] == generated["service_nodes"]["gluetun"]
    for legacy_alias in (
        "postgres_host",
        "redis_host",
        "lldap_host",
        "loki_host",
        "dns_server",
        "wazuh_manager_ip",
        "crowdsec_host",
        "mail_host",
        "registry_mirror_host",
    ):
        assert legacy_alias not in generated


def test_ansible_generated_storage_values_follow_configured_proxmox_storage() -> None:
    generated = yaml.safe_load(
        render_ansible_generated_yml(
            {
                "proxmox": {"lxc_storage": "fast-ssd"},
            }
        )
    )

    assert generated["zfs_proxmox_id"] == "fast-ssd"
    assert generated["machine_data_mounts"]["media"] == ["/data"]
    assert generated["service_host_sources"]["gitea"]["GITEA_DATA_SOURCE"].endswith("/data/gitea")
    assert generated["service_config_sources"] == []
    assert generated["service_host_sources"]["kopia"]["KOPIA_CONFIG_SOURCE"].endswith("/data/kopia/config")


def test_infrastructure_capabilities_have_manifest_owned_providers() -> None:
    catalog = load_service_catalog(ROOT)

    assert catalog.require_provider("mesh-router").name == "headscale"
    assert catalog.require_provider("tunnel-device").name == "gluetun"


def test_ansible_generated_network_values_follow_desired_topology() -> None:
    raw = Config().model_dump(mode="python")
    raw["network"]["mesh_ipv4_cidr"] = "100.100.0.0/16"
    for machine in raw["machines"].values():
        suffix = machine["address"].rsplit(".", 1)[1]
        machine.update({"address": f"10.20.16.{suffix}", "gateway": "10.20.16.1", "cidr": 20})

    generated = yaml.safe_load(render_ansible_generated_yml(raw))

    assert generated["proxmox_private_network"] == "10.20.16.0/20"
    assert generated["private_network"] == "10.20.16.0/20"
    assert generated["proxmox_private_gateway"] == "10.20.16.1"
    assert generated["proxmox_private_bridge_name"] == "vmbr1"
    assert generated["proxmox_private_bridge_cidr"] == "10.20.16.1/20"
    assert generated["proxmox_public_bridge_name"] == "vmbr0"
    assert generated["mesh_cidr"] == "100.100.0.0/16"


def test_ansible_public_dnat_respects_global_internet_exposure() -> None:
    generated = yaml.safe_load(
        render_ansible_generated_yml(
            {
                "network": {
                    "expose_via_internet": False,
                    "mail_public_access": True,
                    "dns_public_access": True,
                }
            }
        )
    )

    assert generated["internet_ingress_enabled"] is False
    assert generated["public_listener_forwards"] == []


def test_ansible_public_dnat_is_compiled_from_service_listener_manifests() -> None:
    generated = yaml.safe_load(render_ansible_generated_yml({}))

    assert generated["public_listener_forwards"] == [
        {
            "id": "dns-tcp-public",
            "port": 53,
            "protocol": "tcp",
            "service": "adguard",
            "target_ip": "10.10.10.10",
        },
        {
            "id": "dns-udp-public",
            "port": 53,
            "protocol": "udp",
            "service": "adguard",
            "target_ip": "10.10.10.10",
        },
        {
            "id": "smtp-public",
            "port": 25,
            "protocol": "tcp",
            "service": "mailserver",
            "target_ip": "10.10.10.10",
        },
        {
            "id": "submissions-public",
            "port": 465,
            "protocol": "tcp",
            "service": "mailserver",
            "target_ip": "10.10.10.10",
        },
        {
            "id": "submission-public",
            "port": 587,
            "protocol": "tcp",
            "service": "mailserver",
            "target_ip": "10.10.10.10",
        },
        {
            "id": "imaps-public",
            "port": 993,
            "protocol": "tcp",
            "service": "mailserver",
            "target_ip": "10.10.10.10",
        },
    ]


def test_host_setup_reconciles_public_dnat_through_a_managed_chain() -> None:
    playbook = (ROOT / "automation/ansible/host-setup.yml").read_text(encoding="utf-8")

    assert "Create managed homelab DNAT chain" in playbook
    assert "Flush managed homelab DNAT chain" in playbook
    assert "chain_management: true" in playbook
    assert "chain: HOMELAB_DNAT" in playbook
    assert "jump: HOMELAB_DNAT" in playbook


def test_ansible_generated_projects_maintenance_timer_desired_state() -> None:
    raw = {
        "domain": "lab.local",
        "proxmox": {"api_url": "https://192.168.5.1:8006/api2/json"},
        "maintenance": {"enabled": False, "daily_at": "04:30"},
    }

    generated = yaml.safe_load(render_ansible_generated_yml(raw))

    assert generated["homelab_maintenance_enabled"] is False
    assert generated["homelab_maintenance_calendar"] == "*-*-* 04:30:00"


def test_ansible_generated_projects_manifest_owned_rightsize_schedule() -> None:
    raw = {
        "domain": "lab.local",
        "proxmox": {"api_url": "https://192.168.5.1:8006/api2/json"},
        "service_settings": {
            "homelab-ui": {
                "rightsize-enabled": False,
                "rightsize-interval-hours": 48,
            }
        },
    }

    generated = yaml.safe_load(render_ansible_generated_yml(raw))

    assert generated["homelab_rightsize_enabled"] is False
    assert generated["homelab_rightsize_interval_hours"] == 48


def test_rightsize_timer_settings_require_host_reconciliation() -> None:
    manifest = yaml.safe_load((ROOT / "toolkit/services/homelab-ui/service.yaml").read_text())
    settings = {item["key"]: item for item in manifest["management"]["settings"]}

    assert settings["rightsize-enabled"]["requires_redeploy"] is True
    assert settings["rightsize-interval-hours"]["requires_redeploy"] is True


def test_ansible_generated_includes_base_domain():
    raw = {
        "domain": "lab.local",
        "proxmox": {"api_url": "https://192.168.5.1:8006/api2/json"},
    }
    yml = render_ansible_generated_yml(raw)
    assert "base_domain: lab.local" in yml
    assert "proxmox_control_host: 192.168.5.1" in yml


def test_ansible_generated_uses_explicit_typed_control_host():
    raw = {
        "domain": "lab.local",
        "proxmox": {
            "api_url": "https://192.168.5.1:8006/api2/json",
            "control_host": "pve-admin.example.test",
        },
    }

    generated = yaml.safe_load(render_ansible_generated_yml(raw))

    assert generated["proxmox_control_host"] == "pve-admin.example.test"


def test_open_tofu_owns_checksum_pinned_lxc_template_download():
    main = (ROOT / "infrastructure/main.tf").read_text(encoding="utf-8")
    variables = (ROOT / "infrastructure/variables.tf").read_text(encoding="utf-8")
    host_setup = (ROOT / "automation/ansible/host-setup.yml").read_text(encoding="utf-8")

    assert 'resource "proxmox_download_file" "lxc_template"' in main
    assert re.search(r'content_type\s+= "vztmpl"', main)
    assert "count = local.default_lxc_template_required ? 1 : 0" in main
    assert re.search(r"checksum\s+= var.lxc_template_checksum", main)
    assert 'template_file_id = each.value.template_file_id != ""' in main
    assert "proxmox_download_file.lxc_template[0].id" in main
    assert re.search(r"checksum\s+= each.value.cloud_image_sha256", main)
    assert 'content_type       = "import"' in main
    assert "datastore_id       = each.value.cloud_image_datastore" in main
    assert 'file_name          = "homelab-${each.key}-cloud.${each.value.cloud_image_format}"' in main
    assert re.search(r"import_from\s+= proxmox_download_file.vm_image\[each.key\].id", main)
    assert "serial_device" in main
    assert "username = each.value.admin_user" in main
    assert 'variable "lxc_template_url"' in variables
    assert 'variable "lxc_template_checksum"' in variables
    assert 'variable "lxc_template_datastore"' in variables
    assert 'variable "lxc_template_id"' not in variables
    assert "pveam" not in host_setup


def test_ansible_generated_includes_only_public_remote_backup_identity(tmp_path: Path) -> None:
    _link_service_catalog(tmp_path)
    raw = {
        "domain": "lab.local",
        "proxmox": {"api_url": "https://192.168.5.1:8006/api2/json"},
        "backups": {"enabled": True, "target": "remote", "storage_host": "nas"},
        "external_hosts": [
            {
                "name": "nas",
                "ip": "192.0.2.10",
                "services": ["backup-storage"],
                "integrations": {"backup-storage": {"path": "/srv/backups"}},
            }
        ],
    }

    yml = render_ansible_generated_yml(raw, repo_root=tmp_path)

    assert "kopia_backup_public_key: ssh-ed25519" in yml
    assert "PRIVATE KEY" not in yml


def test_sync_writes_files(tmp_path: Path):
    from toolkit.core.infra.iac_sync import sync_from_repo_root

    _link_service_catalog(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "domain: t.example\nproxmox:\n  ssh_public_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA test-key\n"
    )
    tf, ans = sync_from_repo_root(tmp_path)
    assert tf.exists()
    assert ans.exists()
    assert "10.10.10.10" in tf.read_text()
    assert "base_domain: t.example" in ans.read_text()
    assert stat.S_IMODE(tf.stat().st_mode) == 0o600
    assert stat.S_IMODE(ans.stat().st_mode) == 0o600
    routes = tmp_path / "automation/ansible/group_vars/generated-routes.yml"
    assert routes.exists()
    assert stat.S_IMODE(routes.stat().st_mode) == 0o600


def test_sync_fails_closed_before_writing_when_routes_do_not_compile(tmp_path: Path, monkeypatch) -> None:
    from toolkit.core.infra.iac_sync import sync_from_repo_root

    _link_service_catalog(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "domain: t.example\nproxmox:\n  ssh_public_key: ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAA test-key\n"
    )
    routes = tmp_path / "automation/ansible/group_vars/generated-routes.yml"
    routes.parent.mkdir(parents=True)
    routes.write_text("stale-routes\n")
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_routes.render_ansible_routes_yml",
        lambda _cfg: (_ for _ in ()).throw(RuntimeError("route compilation failed")),
    )

    with pytest.raises(RuntimeError, match="route compilation failed"):
        sync_from_repo_root(tmp_path)

    assert routes.read_text() == "stale-routes\n"
    assert not (tmp_path / "infrastructure/generated.auto.tfvars").exists()


def test_ansible_generated_vars_never_persist_secret_store_values(tmp_path: Path, monkeypatch) -> None:
    _link_service_catalog(tmp_path)
    canaries = {
        "ADGUARD_ADMIN_PASSWORD": "adguard-generated-vars-canary",
        "LLDAP_ADMIN_PASSWORD": "lldap-admin-generated-vars-canary",
        "LLDAP_BIND_PASSWORD": "lldap-bind-generated-vars-canary",
        "CLOUDFLARE_API_TOKEN": "cloudflare-generated-vars-canary",
    }
    monkeypatch.setattr(
        "toolkit.core.secrets.secrets.load_secrets_plaintext",
        lambda _path: canaries,
    )

    rendered = render_ansible_generated_yml(Config().model_dump(mode="python"), repo_root=tmp_path)

    for value in canaries.values():
        assert value not in rendered
    generated = yaml.safe_load(rendered)
    assert "adguard_admin_password" not in generated
    assert "lldap_admin_password" not in generated
    assert "lldap_bind_password" not in generated
    assert "cloudflare_api_token" not in generated


def test_ansible_generated_declares_node_scoped_service_artifacts(tmp_path: Path) -> None:
    _link_service_catalog(tmp_path)

    generated = yaml.safe_load(render_ansible_generated_yml(Config().model_dump(mode="python"), repo_root=tmp_path))
    artifacts = {item["path"]: item for item in generated["service_generated_artifacts"]}

    assert artifacts["authelia/configuration.yml"] == {
        "path": "authelia/configuration.yml",
        "service": "authelia",
        "nodes": ["infra"],
        "enabled": True,
        "kind": "file",
        "mode": "0600",
        "sensitive": True,
        "host_uid": 0,
        "host_gid": 0,
    }
    assert artifacts["recyclarr/recyclarr.yml"]["nodes"] == ["media"]
    assert artifacts["seaweedfs-s3.json"]["nodes"] == ["apps"]
    assert artifacts["kopia/tls/server.crt"]["nodes"] == ["infra"]
    assert artifacts["kopia/tls/server.crt"]["mode"] == "0644"
    assert artifacts["kopia/tls/server.key"]["nodes"] == ["infra"]
    assert artifacts["kopia/tls/server.key"]["mode"] == "0600"
    assert artifacts["kopia/tls/server.key"]["sensitive"] is True
