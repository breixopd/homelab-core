from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.ops.preflight import (
    PreflightItem,
    _check_ansible_security_gate,
    _check_database_mesh,
    preflight_passed,
    run_preflight,
)


def test_preflight_checks_config(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    cfg = Config(domain="example.com", email="a@example.com")
    save_config(cfg, config_path(root))
    items = run_preflight(root, cfg)
    assert any(i.id == "config" and i.ok for i in items)
    assert not preflight_passed(items)  # missing env files


def test_ansible_security_gate_uses_generated_guest_hook_contract(tmp_path: Path) -> None:
    root = tmp_path / "homelab"
    guest_setup = root / "automation" / "ansible" / "guest-setup.yml"
    hooks = root / "toolkit" / "services" / "lldap" / "ansible"
    generated = root / "automation" / "ansible" / "group_vars" / "generated.yml"
    inventory = root / "automation" / "ansible" / "inventory" / "hosts.yml"
    guest_setup.parent.mkdir(parents=True)
    hooks.mkdir(parents=True)
    generated.parent.mkdir(parents=True)
    inventory.parent.mkdir(parents=True)
    inventory.write_text("all:\n  hosts: {}\n")
    guest_setup.write_text(
        "- hosts: guest_hosts\n"
        "  tasks:\n"
        "    - ansible.builtin.include_tasks: '{{ item }}'\n"
        "      loop: '{{ service_guest_task_files }}'\n"
    )
    hook = hooks / "guest.yml"
    hook.write_text("- name: Configure LLDAP client\n  ansible.builtin.include_role:\n    name: ldap_client\n")
    generated.write_text(
        "service_guest_task_files:\n"
        "- toolkit/services/lldap/ansible/guest.yml\n"
        "service_guest_final_task_files: []\n"
        "service_manager_task_files: []\n"
        "service_security_task_files: []\n"
        "service_sync_task_files:\n"
        "- toolkit/services/lldap/ansible/sync.yml\n"
    )
    (hooks / "sync.yml").write_text(
        "- name: Configure LLDAP client\n  ansible.builtin.include_role:\n    name: ldap_client\n"
    )
    cfg = Config(proxmox={"provision_machines": True})

    with (
        patch("toolkit.core.ops.preflight.resolve_tool", return_value="ansible-playbook"),
        patch(
            "toolkit.core.ops.preflight.subprocess.run",
            return_value=MagicMock(returncode=0, stderr="", stdout=""),
        ) as run,
    ):
        item = _check_ansible_security_gate(root, cfg)

    assert item is not None
    assert item.ok is True
    assert item.label == "guest-setup syntax-check"
    command = run.call_args.args[0]
    assert command[:3] == ["ansible-playbook", "-i", str(inventory)]
    assert ["-e", f"@{generated}"] == command[3:5]


def test_sops_age_ok_with_key_file(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    (root / "keys").mkdir()
    (root / "keys" / "age.key").write_text("AGE-SECRET-KEY-1FAKE\n")
    cfg = Config(domain="example.com", email="a@example.com")
    save_config(cfg, config_path(root))

    items = run_preflight(root, cfg, bootstrap=True)
    sops = next(i for i in items if i.id == "sops_age")
    assert sops.ok is True
    assert sops.detail == ""


def test_sops_age_missing_key(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    cfg = Config(domain="example.com", email="a@example.com")
    save_config(cfg, config_path(root))

    with patch(
        "toolkit.core.secrets.secrets._sops_age_key_candidates",
        return_value=[root / "keys" / "age.key"],
    ):
        items = run_preflight(root, cfg, bootstrap=True)
    sops = next(i for i in items if i.id == "sops_age")
    assert sops.ok is False
    assert "init-sops" in sops.detail


def test_preflight_passed_requires_sops_age():
    items = [
        PreflightItem("config", "config.yaml", True),
        PreflightItem("sops_age", "SOPS age decryption key", False),
        PreflightItem("load", "host load", False),
    ]
    assert not preflight_passed(items)


def test_preflight_passed_database_mesh_optional():
    items = [
        PreflightItem("config", "config.yaml", True),
        PreflightItem("sops_age", "SOPS age decryption key", True),
        PreflightItem("database_mesh", "apps → infra PostgreSQL", False),
    ]
    assert preflight_passed(items)


def test_database_mesh_follows_manifest_provider_and_binding(tmp_path: Path) -> None:
    cfg = Config(
        machines={
            "control": {
                "hostname": "control",
                "address": "10.10.10.10",
                "gateway": "10.10.10.1",
                "vmid": 100,
                "labels": ["control"],
            },
            "apps": {
                "hostname": "apps",
                "address": "10.10.10.11",
                "gateway": "10.10.10.1",
                "vmid": 101,
                "labels": ["apps"],
            },
        }
    )
    with patch("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", return_value=(0, "OK\n", "")) as ssh:
        item = _check_database_mesh(cfg, tmp_path)

    assert item is not None
    assert item.id == "database_mesh"
    assert item.ok is True
    assert "PostgreSQL" in item.label
    assert ssh.call_args.args[1] == cfg.node_ip("apps")


def test_vault_cf_waf_proxied_with_rule(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    (root / "keys").mkdir()
    (root / "keys" / "age.key").write_text("AGE-SECRET-KEY-1FAKE\n")
    (root / "secrets.enc.yaml").write_text("CLOUDFLARE_API_TOKEN: x\n")
    cfg = Config(domain="example.com", email="a@example.com", services={"cloud": True, "media": False})
    save_config(cfg, config_path(root))

    from toolkit.core.ops.dns import DNSRecord

    client = MagicMock()
    client._zone_id = "zone"
    client.list_records.return_value = [
        DNSRecord(name="vault.example.com", type="A", content="1.2.3.4", proxied=True, record_id="r1"),
    ]

    with (
        patch("toolkit.core.secrets.secrets.load_secrets_plaintext", return_value={"CLOUDFLARE_API_TOKEN": "tok"}),
        patch("toolkit.core.ops.dns.CloudflareDNS", return_value=client),
        patch("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", return_value=(0, "OK\n", "")),
        patch(
            "urllib.request.urlopen",
            return_value=MagicMock(
                read=lambda: b'{"result":{"rules":[{"description":"rate limit vault"}]}}',
                __enter__=lambda s: s,
                __exit__=lambda *a: None,
            ),
        ),
    ):
        items = run_preflight(root, cfg, bootstrap=True)

    vault = next(i for i in items if i.id == "vault_cf_waf")
    assert vault.ok is True
    assert "WAF" in vault.detail


def test_vault_cf_waf_proxied_api_forbidden(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    (root / "keys").mkdir()
    (root / "keys" / "age.key").write_text("AGE-SECRET-KEY-1FAKE\n")
    (root / "secrets.enc.yaml").write_text("encrypted\n")
    cfg = Config(domain="example.com", email="a@example.com", services={"cloud": True, "media": False})
    save_config(cfg, config_path(root))

    from toolkit.core.ops.dns import DNSRecord

    client = MagicMock()
    client._zone_id = "zone"
    client.list_records.return_value = [
        DNSRecord(name="vault.example.com", type="A", content="1.2.3.4", proxied=True, record_id="r1"),
    ]

    import urllib.error

    def fake_urlopen(req, timeout=15):
        raise urllib.error.HTTPError(req.full_url, 403, "forbidden", hdrs=None, fp=None)

    with (
        patch(
            "toolkit.core.secrets.secrets.load_secrets_plaintext",
            return_value={"CLOUDFLARE_API_TOKEN": "tok"},
        ),
        patch("toolkit.core.ops.dns.CloudflareDNS", return_value=client),
        patch("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", return_value=(0, "OK\n", "")),
        patch("urllib.request.urlopen", side_effect=fake_urlopen),
    ):
        items = run_preflight(root, cfg, bootstrap=True)

    vault = next(i for i in items if i.id == "vault_cf_waf")
    assert vault.ok is True
    assert "proxied" in vault.detail.lower()


def test_vault_cf_waf_manual_attest(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    (root / "keys").mkdir()
    (root / "keys" / "age.key").write_text("AGE-SECRET-KEY-1FAKE\n")
    (root / "secrets.enc.yaml").write_text("encrypted\n")
    cfg = Config(domain="example.com", email="a@example.com", services={"cloud": True, "media": False})
    save_config(cfg, config_path(root))

    with (
        patch(
            "toolkit.core.secrets.secrets.load_secrets_plaintext",
            return_value={"CF_VAULT_WAF_ATTEST": "1"},
        ),
    ):
        items = run_preflight(root, cfg, bootstrap=True)

    vault = next(i for i in items if i.id == "vault_cf_waf")
    assert vault.ok is True
    assert vault.detail == "manual attest"


def _preflight_bootstrap_root(tmp_path: Path) -> tuple[Path, Config]:
    root = tmp_path / "homelab"
    root.mkdir()
    (root / "keys").mkdir()
    (root / "keys" / "age.key").write_text("AGE-SECRET-KEY-1FAKE\n")
    cfg = Config(
        domain="example.com",
        email="a@example.com",
        services={"media": True, "cloud": False},
        service_settings={
            "gluetun": {"enabled": True},
            "music-sync": {"enabled": True},
        },
        dns={"provider": "cloudflare"},
    )
    save_config(cfg, config_path(root))
    return root, cfg


def test_service_credentials_vpn_missing(tmp_path: Path):
    root, cfg = _preflight_bootstrap_root(tmp_path)
    with patch("toolkit.core.ops.preflight._load_secrets_for_preflight", return_value={}):
        items = run_preflight(root, cfg, bootstrap=True)
    vpn = next(i for i in items if i.id == "service_credentials_gluetun")
    assert vpn.ok is False
    assert "NORDVPN_TOKEN" in vpn.detail


def test_service_credentials_vpn_ok(tmp_path: Path):
    root, cfg = _preflight_bootstrap_root(tmp_path)
    secrets = {"NORDVPN_TOKEN": "token"}
    with patch("toolkit.core.ops.preflight._load_secrets_for_preflight", return_value=secrets):
        items = run_preflight(root, cfg, bootstrap=True)
    vpn = next(i for i in items if i.id == "service_credentials_gluetun")
    assert vpn.ok is True


def test_service_credentials_follow_manifest_predicates(tmp_path: Path):
    root, cfg = _preflight_bootstrap_root(tmp_path)
    cfg.service_settings["gluetun"]["provider"] = "protonvpn"
    secrets = {"VPN_USER": "u", "VPN_PASSWORD": "p"}
    with patch("toolkit.core.ops.preflight._load_secrets_for_preflight", return_value=secrets):
        items = run_preflight(root, cfg, bootstrap=True)

    vpn = next(i for i in items if i.id == "service_credentials_gluetun")
    assert vpn.ok is True


def test_service_credentials_spotify_missing(tmp_path: Path):
    root, cfg = _preflight_bootstrap_root(tmp_path)
    secrets = {"NORDVPN_TOKEN": "token"}
    with patch("toolkit.core.ops.preflight._load_secrets_for_preflight", return_value=secrets):
        items = run_preflight(root, cfg, bootstrap=True)
    spotify = next(i for i in items if i.id == "service_credentials_music-sync")
    assert spotify.ok is False
    assert "SPOTIFY_CLIENT_ID" in spotify.detail
    assert "SPOTIFY_CLIENT_SECRET" in spotify.detail


def test_feature_secrets_cloudflare_token_missing(tmp_path: Path):
    root, cfg = _preflight_bootstrap_root(tmp_path)
    with patch("toolkit.core.ops.preflight._load_secrets_for_preflight", return_value={}):
        items = run_preflight(root, cfg, bootstrap=True)
    cf = next(i for i in items if i.id == "feature_cloudflare_dns")
    assert cf.ok is False
    assert cf.detail == "set CLOUDFLARE_API_TOKEN in secrets"


def test_feature_secrets_rejects_caddy_runtime_token_as_user_secret(tmp_path: Path):
    root, cfg = _preflight_bootstrap_root(tmp_path)
    with patch(
        "toolkit.core.ops.preflight._load_secrets_for_preflight",
        return_value={"CF_API_TOKEN": "runtime-alias-is-not-a-user-secret"},
    ):
        items = run_preflight(root, cfg, bootstrap=True)

    cf = next(i for i in items if i.id == "feature_cloudflare_dns")
    assert cf.ok is False


def test_age_key_backup_requires_attest(tmp_path: Path):
    root, cfg = _preflight_bootstrap_root(tmp_path)
    with patch("toolkit.core.ops.preflight._load_secrets_for_preflight", return_value={}):
        items = run_preflight(root, cfg, bootstrap=True)
    backup = next(i for i in items if i.id == "age_key_backup")
    assert backup.ok is False
    assert "AGE_KEY_BACKUP_ATTEST" in backup.detail


def test_age_key_backup_attested(tmp_path: Path):
    root, cfg = _preflight_bootstrap_root(tmp_path)
    with patch(
        "toolkit.core.ops.preflight._load_secrets_for_preflight",
        return_value={"AGE_KEY_BACKUP_ATTEST": "1"},
    ):
        items = run_preflight(root, cfg, bootstrap=True)
    backup = next(i for i in items if i.id == "age_key_backup")
    assert backup.ok is True
    assert backup.detail == ""


def test_preflight_existing_guests_omits_provisioning_only_tools(tmp_path: Path):
    root, cfg = _preflight_bootstrap_root(tmp_path)
    cfg.proxmox.provision_machines = True

    items = run_preflight(root, cfg, bootstrap=True, require_provisioning_tools=False)

    item_ids = {item.id for item in items}
    assert "ansible" in item_ids
    assert "tofu" not in item_ids
    assert "jq" not in item_ids


def test_controller_profile_uses_bootstrap_checks_without_operator_workspace(tmp_path: Path, monkeypatch):
    root, cfg = _preflight_bootstrap_root(tmp_path)
    cfg.proxmox.provision_machines = True
    monkeypatch.setenv("HOMELAB_NODE", cfg.control_node)
    monkeypatch.setattr("toolkit.core.ops.preflight.resolve_ansible_ssh_key", lambda *_a, **_k: root / "ssh.key")
    monkeypatch.setattr(
        "toolkit.core.infra.proxmox_ssh.resolve_proxmox_proxy_key",
        lambda *_a, **_k: root / "proxy.key",
    )
    monkeypatch.setattr(
        "toolkit.core.infra.proxmox_tls.ensure_proxmox_ca_bundle",
        lambda *_a, **_k: root / "ca.pem",
    )

    items = run_preflight(root, cfg, bootstrap=True, profile="controller")
    item_ids = {item.id for item in items}
    assert "venv" not in item_ids
    assert not any(item.id.startswith("env_") for item in items)
    assert "tofu" in item_ids
    assert "jq" in item_ids
    assert "python_runtime" in item_ids
    assert "ansible_security" not in item_ids


def test_operator_profile_remains_strict_without_workspace(tmp_path: Path):
    root, cfg = _preflight_bootstrap_root(tmp_path)
    items = run_preflight(root, cfg)
    item_ids = {item.id for item in items}
    assert "venv" in item_ids
    assert any(item.id.startswith("env_") for item in items)


def test_controller_profile_post_generate_checks_all_node_envs(tmp_path: Path, monkeypatch):
    root, cfg = _preflight_bootstrap_root(tmp_path)
    monkeypatch.setenv("HOMELAB_NODE", cfg.control_node)
    for node in cfg.enabled_nodes:
        env_file = root / "generated" / node / ".env"
        env_file.parent.mkdir(parents=True, exist_ok=True)
        env_file.write_text("generated=true\n")

    items = run_preflight(root, cfg, bootstrap=False, profile="controller")
    env_ids = {item.id for item in items if item.id.startswith("env_")}
    assert env_ids == {f"env_{node}" for node in cfg.enabled_nodes}
