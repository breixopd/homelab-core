from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from toolkit.core.config.config import Config
from toolkit.services import get_service_plugin


def _plugin(name: str):
    plugin = get_service_plugin(name)
    assert plugin is not None
    return plugin


@pytest.mark.parametrize(
    "service,function,module",
    [
        ("wazuh-indexer", "ensure_wazuh_indexer_healthy", "toolkit.services.wazuh-indexer.bootstrap"),
    ],
)
def test_pre_start_recovery_is_owned_by_plugin(service: str, function: str, module: str) -> None:
    context = MagicMock()

    services = _plugin(service).before_runtime_start(context, (service,))

    assert services == (service,)
    context.run_recovery.assert_called_once_with(function, module)


@pytest.mark.parametrize(
    "service,function,module,kwargs",
    [
        (
            "postgres",
            "ensure_postgres_healthy",
            "toolkit.services.sdk.postgres",
            {"node": "infra", "service": "postgres"},
        ),
        (
            "immich-postgres",
            "ensure_postgres_healthy",
            "toolkit.services.sdk.postgres",
            {"node": "infra", "service": "immich-postgres"},
        ),
        ("lldap", "sync_ldap_bind_only", "toolkit.services.lldap.bootstrap", {}),
    ],
)
def test_post_start_reconciliation_is_owned_by_plugin(
    service: str,
    function: str,
    module: str,
    kwargs: dict[str, str],
) -> None:
    context = MagicMock(node="infra")

    _plugin(service).after_runtime_start(context, (service,))

    context.run_recovery.assert_called_once_with(function, module, **kwargs)


def test_gluetun_and_qbittorrent_share_only_bounded_lifecycle_state() -> None:
    context = MagicMock()
    context.services_healthy.return_value = True
    _plugin("gluetun").after_runtime_start(context, ("gluetun",))
    context.set_state.assert_called_once_with("gluetun_healthy", True)

    context.state.return_value = True
    services = _plugin("qbittorrent").before_runtime_start(context, ("qbittorrent-vpn",))
    assert services == ("qbittorrent-vpn",)
    context.run_host.assert_any_call(["docker", "rm", "-f", "qbittorrent-vpn"])
    context.run_host.assert_any_call(["docker", "rm", "-f", "qbittorrent"])
    context.add_compose_up_option.assert_called_once_with("--no-recreate")

    _plugin("qbittorrent").after_runtime_start(context, services)
    context.remove_compose_up_option.assert_called_once_with("--no-recreate")


def test_gluetun_owns_its_deployment_credential_gate(tmp_path: Path) -> None:
    context = MagicMock(root=tmp_path)

    with pytest.raises(RuntimeError, match="credentials are incomplete"):
        _plugin("gluetun").prepare_runtime_deployment(context, ("gluetun",))

    vpn_env = tmp_path / "generated" / ".env.vpn"
    vpn_env.parent.mkdir()
    vpn_env.write_text("VPN_SERVICE_PROVIDER=nordvpn\nWIREGUARD_PRIVATE_KEY=key\n", encoding="utf-8")
    context.services_healthy.return_value = True

    _plugin("gluetun").prepare_runtime_deployment(context, ("gluetun",))

    context.set_state.assert_called_once_with("gluetun_healthy", True)


def test_gluetun_repairs_credentials_from_node_environment(tmp_path: Path) -> None:
    vpn_env = tmp_path / "generated" / ".env.vpn"
    vpn_env.parent.mkdir()
    vpn_env.write_text("VPN_SERVICE_PROVIDER=nordvpn\nWIREGUARD_PRIVATE_KEY=\n", encoding="utf-8")
    node_env = tmp_path / "generated" / "media" / ".env"
    node_env.parent.mkdir()
    node_env.write_text(
        "VPN_PROVIDER=nordvpn\nVPN_TYPE=wireguard\nWIREGUARD_PRIVATE_KEY=derived-key\n",
        encoding="utf-8",
    )
    context = MagicMock(root=tmp_path, node="media")
    context.services_healthy.return_value = True

    _plugin("gluetun").prepare_runtime_deployment(context, ("gluetun",))

    assert "WIREGUARD_PRIVATE_KEY=derived-key\n" in vpn_env.read_text(encoding="utf-8")
    assert vpn_env.stat().st_mode & 0o777 == 0o600
    context.log.assert_called_once()
    context.set_state.assert_called_once_with("gluetun_healthy", True)


def test_komodo_retries_without_destructive_recovery() -> None:
    context = MagicMock()
    context.services_healthy.return_value = False
    context.retry_services.return_value = True
    context.wait_until_healthy.return_value = True

    _plugin("komodo-core").after_runtime_start(context, ("komodo-core",))

    context.retry_services.assert_called_once_with(("komodo-core",))
    context.run_recovery.assert_not_called()
    context.resolve_failure.assert_called_once_with()


def test_stateful_repair_actions_are_declared_and_implemented() -> None:
    expected = {
        "wazuh-indexer": "reconcile-security",
        "komodo-core": "reconcile-credentials",
    }

    for service, action in expected.items():
        plugin = _plugin(service)
        assert {candidate.id for candidate in plugin.management().actions} == {action}
        assert plugin.supported_actions() == frozenset({action})


def test_wazuh_repair_action_uses_non_destructive_controller_reconciliation(tmp_path: Path, monkeypatch) -> None:
    reconcile = MagicMock(return_value=["Wazuh: credentials reconciled; indexer state preserved"])
    wazuh_bootstrap = importlib.import_module("toolkit.services.wazuh-indexer.bootstrap")
    monkeypatch.setattr(wazuh_bootstrap, "reconcile_wazuh_security", reconcile)

    logs = _plugin("wazuh-indexer").execute_action("reconcile-security", Config(), {}, tmp_path)

    assert logs == ["Wazuh: credentials reconciled; indexer state preserved"]
    reconcile.assert_called_once()


def test_komodo_repair_action_fails_when_runtime_reconciliation_is_incomplete(tmp_path: Path, monkeypatch) -> None:
    komodo_plugin = importlib.import_module("toolkit.services.komodo-core.plugin").KomodoPlugin
    monkeypatch.setattr(
        komodo_plugin,
        "reconcile_runtime_credentials",
        lambda *_args: ["Hook error: Komodo runtime credential reconciliation failed (exit 1)"],
    )

    with pytest.raises(RuntimeError, match="did not converge"):
        _plugin("komodo-core").execute_action("reconcile-credentials", Config(), {}, tmp_path)


def test_runtime_credential_reconciliation_dispatches_only_matching_plugins(tmp_path: Path, monkeypatch) -> None:
    from toolkit.core.deploy.deploy_workflow import reconcile_runtime_credentials

    matching = MagicMock()
    matching.runtime_node.return_value = "infra"
    matching.reconcile_runtime_credentials.return_value = ["matching credentials reconciled"]
    other = MagicMock()
    other.runtime_node.return_value = "apps"

    monkeypatch.setattr(
        "toolkit.services.enabled_service_plugins",
        lambda _cfg: [("management", matching), ("media", other)],
    )

    logs = reconcile_runtime_credentials(Config(), tmp_path, "infra")

    assert logs == ["matching credentials reconciled"]
    matching.reconcile_runtime_credentials.assert_called_once()
    other.reconcile_runtime_credentials.assert_not_called()


def test_runtime_placement_dispatch_includes_secondary_agent_runtimes() -> None:
    from tests.helpers.machines import renamed_default_machines
    from toolkit.services import enabled_plugin_runtimes

    cfg = Config(domain="example.com", machines=renamed_default_machines())

    stream = {plugin.service: runtimes for _category, plugin, runtimes in enabled_plugin_runtimes(cfg, "stream")}
    core = {plugin.service: runtimes for _category, plugin, runtimes in enabled_plugin_runtimes(cfg, "core")}

    assert "alloy-agent" in stream["alloy"]
    assert "alloy-agent-docker-proxy" in stream["alloy"]
    assert "cadvisor-agent" in stream["cadvisor"]
    assert "node-exporter-agent" in stream["node-exporter"]
    assert "alloy" not in stream["alloy"]
    assert core["alloy"] == ("alloy",)


def test_wazuh_controller_reconciliation_validates_bounded_remote_response(tmp_path: Path, monkeypatch) -> None:
    reconcile_wazuh_security = importlib.import_module(
        "toolkit.services.wazuh-indexer.bootstrap"
    ).reconcile_wazuh_security

    cfg = Config()
    response = {
        "ok": True,
        "logs": ["Wazuh: credentials reconciled; indexer state preserved"],
    }
    ssh = MagicMock(return_value=(0, json.dumps(response), ""))
    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", ssh)

    assert reconcile_wazuh_security(cfg, tmp_path) == response["logs"]
    assert "--runtime-reconcile" in ssh.call_args.args[2]


def test_registry_mirror_healthy_probe_needs_no_recovery() -> None:
    context = MagicMock()
    context.environment.return_value = "10.10.10.10"
    context.run_host.return_value.returncode = 0

    _plugin("registry-mirror").after_runtime_start(context, ("registry-mirror",))

    context.retry_services.assert_not_called()
    context.record_failure.assert_not_called()


def test_caddy_reloads_and_accepts_healthy_auth_edge() -> None:
    context = MagicMock()
    context.compose.return_value.returncode = 0
    context.wait_until_healthy.return_value = True

    _plugin("caddy").after_runtime_start(context, ("caddy",))

    assert context.compose.call_args.args[:4] == ("exec", "-T", "caddy", "caddy")
    context.wait_until_healthy.assert_any_call("authelia", ("authelia",))
    context.wait_until_healthy.assert_any_call("caddy-admin", ("caddy",))
    context.record_failure.assert_not_called()
