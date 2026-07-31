from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
import yaml
from click.testing import CliRunner
from toolkit.cli import main
from toolkit.core.config.config import Config, ExternalHost, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.infra.fleet import (
    add_node,
    get_node,
    list_nodes,
    node_status,
    onboard_node,
    remove_node,
)
from toolkit.core.infra.hosts import HostIntegrationReconcileResult, HostIntegrationStatus
from toolkit.services import FleetOnboardingContribution

KOMODO_BOOTSTRAP = importlib.import_module("toolkit.services.komodo-core.bootstrap")
HEADSCALE_MESH = importlib.import_module("toolkit.services.headscale.mesh")


def _setup_config(root: Path) -> None:
    cfg = Config(domain="example.com", email="admin@example.com")
    cfg.machines["infra"] = cfg.machines["infra"].model_copy(update={"address": "10.10.10.10"})
    save_config(cfg, config_path(root))


def test_fleet_host_preserves_service_owned_integration_settings():
    host = ExternalHost(
        name="nas-01",
        ip="10.0.0.5",
        kind="fleet",
        services=["media-cache"],
        integrations={"media-cache": {"path": "/srv/library"}},
    )
    assert host.integration_value("media-cache", "path") == "/srv/library"
    assert "media-cache" in host.services


def test_sync_external_host_merges_without_clobbering(tmp_path: Path):
    _setup_config(tmp_path)
    from toolkit.core.config.config import load_config
    from toolkit.core.infra.hosts import add_host

    add_host(
        tmp_path,
        "nas-01",
        "10.0.0.5",
        services=["media-cache"],
        integrations={"media-cache": {"path": "/srv/library"}},
    )

    add_node(tmp_path, "nas-01", "10.0.0.5")

    cfg = load_config(config_path(tmp_path))
    host = next(h for h in cfg.external_hosts if h.name == "nas-01")
    assert "media-cache" in host.services
    assert "monitoring-agent" in host.services
    assert "wazuh-agent" in host.services
    assert host.integration_value("media-cache", "path") == "/srv/library"


def test_remove_node_removes_external_host(tmp_path: Path, monkeypatch):
    _setup_config(tmp_path)
    monkeypatch.setattr("toolkit.core.ops.dns.remove_external_host_dns", lambda *a, **k: {})
    add_node(tmp_path, "vps-01", "203.0.113.5")

    from toolkit.core.config.config import load_config

    assert load_config(config_path(tmp_path)).external_hosts
    assert remove_node(tmp_path, "vps-01") is True
    assert list_nodes(tmp_path) == []
    assert load_config(config_path(tmp_path)).external_hosts == []


def test_add_list_remove_persistence(tmp_path: Path):
    _setup_config(tmp_path)
    node = add_node(
        tmp_path,
        "vps-01",
        "203.0.113.5",
        cluster_group="edge",
        lldap_email="ops@example.com",
    )
    assert node.name == "vps-01"
    from toolkit.core.config.config import load_config

    stored = load_config(config_path(tmp_path)).external_hosts[0]
    assert stored.kind == "fleet"
    assert stored.cluster_group == "edge"

    nodes = list_nodes(tmp_path)
    assert len(nodes) == 1
    assert nodes[0].lldap_email == "ops@example.com"

    assert remove_node(tmp_path, "vps-01")
    assert list_nodes(tmp_path) == []


def test_add_syncs_external_hosts(tmp_path: Path):
    _setup_config(tmp_path)
    add_node(tmp_path, "vps-01", "203.0.113.5")

    from toolkit.core.config.config import load_config

    cfg = load_config(config_path(tmp_path))
    assert len(cfg.external_hosts) == 1
    assert cfg.external_hosts[0].name == "vps-01"
    assert cfg.external_hosts[0].kind == "fleet"
    assert "monitoring-agent" in cfg.external_hosts[0].services


def test_explicit_empty_service_selection_is_preserved(tmp_path: Path):
    _setup_config(tmp_path)

    add_node(tmp_path, "vps-01", "203.0.113.5", services=[])

    assert get_node(tmp_path, "vps-01").services == []


def test_add_duplicate_raises(tmp_path: Path):
    _setup_config(tmp_path)
    add_node(tmp_path, "vps-01", "203.0.113.5")
    with pytest.raises(ValueError, match="already exists"):
        add_node(tmp_path, "vps-01", "203.0.113.6")


def test_onboard_node_runs_playbook(tmp_path: Path, monkeypatch):
    _setup_config(tmp_path)
    (tmp_path / "secrets.enc.yaml").write_text(
        yaml.safe_dump({"CROWDSEC_AGENT_REGISTRATION_TOKEN": "crowdsec-agent-token-for-tests-000000"}),
        encoding="utf-8",
    )
    add_node(tmp_path, "vps-01", "203.0.113.5", lldap_email="ops@example.com")

    playbook = tmp_path / "automation" / "ansible" / "playbooks" / "onboard-fleet-node.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("---\n- hosts: localhost\n  tasks: []\n")

    inv = tmp_path / "automation" / "ansible" / "inventory" / "hosts.yml"
    inv.parent.mkdir(parents=True, exist_ok=True)
    inv.write_text("all:\n  hosts: {}\n")

    monkeypatch.setattr("toolkit.core.infra.fleet.trust_host_key", lambda *a, **k: ["trusted"])
    monkeypatch.setattr("toolkit.services.lldap.bootstrap.ensure_fleet_user", lambda *a, **k: ["LLDAP: ok"])
    monkeypatch.setattr(
        "toolkit.services.headscale.bootstrap.headscale_preauth_key_for_deploy",
        lambda *_args, **_kwargs: "hskey-test",
    )
    monkeypatch.setattr(KOMODO_BOOTSTRAP, "komodo_onboarding_key", lambda root: "O_1234567890123456789012345678_O")
    monkeypatch.setattr("toolkit.services.headscale.mesh.fleet_node_online", lambda *a, **k: True)
    monkeypatch.setattr(
        "toolkit.core.identity.ldap_automation.ensure_directory_and_sssd",
        lambda *a, **k: ["LDAP: sssd ok"],
    )

    proc = MagicMock(returncode=0, pid=1234)
    proc.communicate.return_value = ("PLAY RECAP ok", "")
    captured_vars: dict[str, str] = {}

    def run_ansible(command, **_kwargs):
        var_file = next((Path(str(part)[1:]) for part in command if str(part).startswith("@")), None)
        if var_file is not None:
            captured_vars.update(yaml.safe_load(var_file.read_text(encoding="utf-8")))
        return proc

    with patch("toolkit.core.ansible.ansible_runner.subprocess.Popen", side_effect=run_ansible) as run:
        with patch("toolkit.core.infra.fleet.write_inventory", return_value=inv):
            result = onboard_node(tmp_path, "vps-01")

    assert result.success
    playbook_calls = [c[0][0] for c in run.call_args_list if c and c[0] and "onboard-fleet-node.yml" in str(c[0][0])]
    assert playbook_calls, "expected an onboard-fleet-node.yml ansible run"
    cmd = playbook_calls[0]
    assert "--limit" in cmd
    assert "vps-01" in cmd
    assert "O_1234567890123456789012345678_O" not in " ".join(str(part) for part in cmd)
    assert "hskey-test" not in " ".join(str(part) for part in cmd)
    assert captured_vars["komodo_onboarding_key"] == "O_1234567890123456789012345678_O"
    assert captured_vars["headscale_auth_key"] == "hskey-test"

    node = get_node(tmp_path, "vps-01")
    assert node is not None
    assert node.reconciled is True
    assert node.last_reconcile_at


def test_onboard_rejects_service_variable_collisions(tmp_path: Path, monkeypatch):
    _setup_config(tmp_path)
    add_node(tmp_path, "vps-01", "203.0.113.5", services=[])

    playbook = tmp_path / "automation" / "ansible" / "playbooks" / "onboard-fleet-node.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("---\n- hosts: localhost\n  tasks: []\n")

    first = MagicMock(service="first")
    first.prepare_fleet_onboarding.return_value = FleetOnboardingContribution(variables={"shared": "a"})
    second = MagicMock(service="second")
    second.prepare_fleet_onboarding.return_value = FleetOnboardingContribution(variables={"shared": "b"})
    monkeypatch.setattr("toolkit.core.infra.fleet.trust_host_key", lambda *a, **k: [])
    monkeypatch.setattr(
        "toolkit.core.infra.fleet.reconcile_host_integrations",
        lambda *a, **k: HostIntegrationReconcileResult((), ()),
    )
    monkeypatch.setattr("toolkit.services.enabled_service_plugins", lambda _cfg: [("one", first), ("two", second)])

    result = onboard_node(tmp_path, "vps-01")

    assert not result.success
    assert "variable collision" in result.message


def test_onboard_missing_node(tmp_path: Path):
    _setup_config(tmp_path)
    result = onboard_node(tmp_path, "missing")
    assert not result.success
    assert "not found" in result.message


def test_komodo_onboarding_key_is_derived_from_seed(tmp_path: Path, monkeypatch):
    sec = tmp_path / "secrets.enc.yaml"
    sec.write_text(yaml.dump({"KOMODO_ONBOARDING_SEED": "seed-material-for-komodo"}))
    monkeypatch.setattr(KOMODO_BOOTSTRAP, "secrets_path", lambda root: sec)

    key = KOMODO_BOOTSTRAP.komodo_onboarding_key(tmp_path)
    assert key.startswith("O_") and key.endswith("_O")
    assert len(key) == 32


def test_fleet_cli_add_and_list(tmp_path: Path):
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--root",
            str(tmp_path),
            "fleet",
            "add",
            "vps-01",
            "203.0.113.5",
            "--cluster-group",
            "edge",
            "--skip-onboard",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Added fleet node" in result.output

    result = runner.invoke(main, ["--root", str(tmp_path), "fleet", "list"])
    assert result.exit_code == 0
    assert "vps-01" in result.output
    assert "edge" in result.output


def test_headscale_node_online_matches_registered_node(tmp_path: Path, monkeypatch):
    _setup_config(tmp_path)
    nodes_json = '[{"name": "vps-01", "online": true}]'
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        lambda *a, **k: (0, nodes_json, ""),
    )
    cfg = Config(domain="example.com")
    assert HEADSCALE_MESH.fleet_node_online(cfg, tmp_path, "vps-01") is True


def test_headscale_node_online_unknown_when_not_registered(tmp_path: Path, monkeypatch):
    _setup_config(tmp_path)
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        lambda *a, **k: (0, '[{"name": "other", "online": true}]', ""),
    )
    cfg = Config(domain="example.com")
    assert HEADSCALE_MESH.fleet_node_online(cfg, tmp_path, "vps-01") is False


def test_headscale_node_online_unknown_on_query_failure(tmp_path: Path, monkeypatch):
    _setup_config(tmp_path)
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        lambda *a, **k: (1, "", "timeout"),
    )
    cfg = Config(domain="example.com")
    assert HEADSCALE_MESH.fleet_node_online(cfg, tmp_path, "vps-01") is None


def test_node_status_uses_manifest_owned_agent_statuses(tmp_path: Path, monkeypatch):
    _setup_config(tmp_path)
    add_node(tmp_path, "vps-01", "203.0.113.5")
    monkeypatch.setattr("toolkit.core.infra.fleet.test_host_connection", lambda *a, **k: True)
    monkeypatch.setattr(
        "toolkit.core.infra.fleet.host_integration_statuses",
        lambda *a, **k: (HostIntegrationStatus("vpn-client", "Headscale mesh VPN", True, "registered and online"),),
    )

    status = node_status(tmp_path, "vps-01")
    assert status is not None
    assert status.agent("vpn-client").active is True


def test_fleet_cli_status(tmp_path: Path, monkeypatch):
    _setup_config(tmp_path)
    add_node(tmp_path, "vps-01", "203.0.113.5")

    from toolkit.core.infra.fleet import FleetNodeStatus

    monkeypatch.setattr(
        "toolkit.core.infra.fleet.node_status",
        lambda root, name: FleetNodeStatus(
            name=name,
            ssh_ok=True,
            agents=(
                HostIntegrationStatus("komodo-periphery", "Komodo Periphery", True, "periphery active"),
                HostIntegrationStatus("wazuh-agent", "Wazuh security agent", False, "Wazuh agent inactive"),
            ),
            reconciled=False,
        ),
    )

    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "fleet", "status", "vps-01"])
    assert result.exit_code == 0
    assert "Komodo Periphery: ok" in result.output
    assert "Wazuh security agent: fail" in result.output
