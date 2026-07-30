from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import MagicMock

from tests.helpers.machines import single_control_machines
from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.infra.hosts import HostIntegrationStatus
from toolkit.core.ops.monitoring_verify import verify_grafana_alerting
from toolkit.services._arr import verify_arr_downloadclient_test, verify_prowlarr_flaresolverr
from toolkit.services.gitea.plugin import _check_gitea_forward_auth
from toolkit.services.headscale.plugin import check_nodes
from toolkit.services.seaweedfs.plugin import _check_seaweedfs_s3

KOMODO_PLUGIN = importlib.import_module("toolkit.services.komodo-core.plugin").KomodoPlugin


def test_check_gitea_forward_auth_302(monkeypatch):
    cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))

    def fake_ssh(_cfg, _ip, _cmd, *, root=None, timeout=20):
        return 0, "HTTP/1.1 302 Found\nLocation: https://auth.example.com/?rd=https%3A%2F%2Fgit.example.com\n", ""

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)
    check = _check_gitea_forward_auth(cfg, "10.0.0.1", Path("."))
    assert check.passed
    assert check.check == "forward_auth"
    assert "auth.example.com" in check.detail


def test_check_gitea_forward_auth_missing_redirect(monkeypatch):
    cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))

    def fake_ssh(_cfg, _ip, _cmd, *, root=None, timeout=20):
        return 0, "HTTP/1.1 200 OK\n", ""

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)
    check = _check_gitea_forward_auth(cfg, "127.0.0.1", Path("."))
    assert not check.passed


def test_check_flaresolverr_prowlarr_wired(monkeypatch):
    class Resp:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    # Single-host cfg so _servarr_get takes its local httpx branch.
    cfg = _single_vm_cfg()
    def fake_get(url, **_kwargs):
        if url.endswith("/indexer"):
            return Resp([{"name": "1337x", "definitionName": "1337x", "tags": [9]}])
        if url.endswith("/indexerproxy"):
            return Resp([{"name": "FlareSolverr", "implementation": "FlareSolverr", "tags": [9]}])
        if url.endswith("/tag"):
            return Resp([{"id": 9, "label": "flaresolverr"}])
        raise AssertionError(url)

    monkeypatch.setattr("httpx.get", fake_get)
    check = verify_prowlarr_flaresolverr(cfg, "http://prowlarr:9696", "prowlarr", "10.0.0.1", Path("."), "key")
    assert check.passed
    assert check.check == "flaresolverr"


def test_check_flaresolverr_prowlarr_skips_without_cf_indexers(monkeypatch):
    class Resp:
        status_code = 200

        def json(self):
            return [{"name": "yts", "definitionName": "yts", "fields": []}]

    # Single-host cfg so _servarr_get takes its local httpx branch.
    cfg = _single_vm_cfg()
    monkeypatch.setattr("httpx.get", lambda *_a, **_k: Resp())
    check = verify_prowlarr_flaresolverr(cfg, "http://prowlarr:9696", "prowlarr", "10.0.0.1", Path("."), "key")
    assert check.passed
    assert "skipped" in check.detail


def test_check_flaresolverr_prowlarr_accepts_ready_proxy_without_protected_indexer(monkeypatch):
    class Resp:
        status_code = 200

        def __init__(self, payload):
            self.payload = payload

        def json(self):
            return self.payload

    def fake_get(url, **_kwargs):
        if url.endswith("/indexer"):
            return Resp([{"name": "yts", "definitionName": "yts", "fields": []}])
        if url.endswith("/indexerproxy"):
            return Resp([{"name": "FlareSolverr", "implementation": "FlareSolverr", "tags": [9]}])
        if url.endswith("/tag"):
            return Resp([{"id": 9, "label": "flaresolverr"}])
        raise AssertionError(url)

    cfg = _single_vm_cfg(media=True)
    monkeypatch.setattr("httpx.get", fake_get)
    check = verify_prowlarr_flaresolverr(cfg, "http://prowlarr:9696", "prowlarr", "10.0.0.1", Path("."), "key")
    assert check.passed
    assert check.detail == "proxy ready; no protected indexer active"


def test_arr_download_client_probe_uses_secure_request_transport(monkeypatch) -> None:
    cfg = _single_vm_cfg(media=True)
    calls = []

    def fake_request(_cfg, _vm_ip, _container, url, **kwargs):
        calls.append((url, kwargs))
        if url.endswith("/downloadclient"):
            return 0, '[{"implementation":"QBittorrent","name":"qbit"}]'
        if url.endswith("/downloadclient/test"):
            return 0, ""
        return 1, ""

    monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_request)
    monkeypatch.setattr(
        "toolkit.services.sdk.docker_exec_on_vm",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("raw authenticated curl is forbidden")),
    )

    check = verify_arr_downloadclient_test(
        cfg,
        "sonarr",
        "sonarr",
        8989,
        "10.10.10.11",
        Path("."),
        "test-api-key",
    )

    assert check.passed
    assert {call[1]["headers"]["X-Api-Key"] for call in calls} == {"test-api-key"}
    assert calls[-1][1]["method"] == "POST"
    assert calls[-1][1]["body"] == '{"implementation": "QBittorrent", "name": "qbit"}'


def test_check_headscale_nodes_no_nodes_fails(monkeypatch):
    cfg = Config(domain="example.com", services=ServicesConfig(security=True))
    monkeypatch.setattr(
        "toolkit.services.sdk.docker_exec_on_vm",
        lambda *_a, **_k: (0, "[]"),
    )
    check = check_nodes(cfg, "10.0.0.1", Path("."))
    assert not check.passed
    assert "no mesh nodes" in check.detail


def test_check_headscale_nodes_null_json(monkeypatch):
    cfg = Config(domain="example.com", services=ServicesConfig(security=True))
    monkeypatch.setattr(
        "toolkit.services.sdk.docker_exec_on_vm",
        lambda *_a, **_k: (0, "null"),
    )
    check = check_nodes(cfg, "10.0.0.1", Path("."))
    assert not check.passed
    assert "no mesh nodes" in check.detail


def test_check_komodo_periphery_skips_unreconciled(monkeypatch):
    cfg = Config(domain="example.com", services=ServicesConfig(management=True))
    node = MagicMock()
    node.name = "vps-01"
    node.reconciled = False
    node.services = ["komodo-periphery"]
    monkeypatch.setattr("toolkit.core.infra.fleet.list_nodes", lambda _root: [node])
    check = KOMODO_PLUGIN().controller_access_checks(cfg, Path("."))[0]
    assert check.passed
    assert "skipped" in check.detail


def _single_vm_cfg(**services_kwargs) -> Config:
    defaults = {
        "management": True,
        "media": False,
        "cloud": False,
        "notifications": False,
        "email": False,
        "security": False,
    }
    defaults.update(services_kwargs)
    return Config(
        domain="example.com",
        services=ServicesConfig(**defaults),
        machines=single_control_machines(),
    )


def test_check_seaweedfs_s3_local(monkeypatch):
    cfg = _single_vm_cfg()

    class Resp:
        status_code = 200
        text = ""

    monkeypatch.setattr("httpx.get", lambda *_a, **_k: Resp())
    check = _check_seaweedfs_s3(cfg, "10.0.0.3", Path("."))
    assert check.passed
    assert check.check == "s3_status"


def test_check_komodo_periphery_skips_without_fleet(monkeypatch):
    cfg = Config(domain="example.com", services=ServicesConfig(management=True))
    monkeypatch.setattr("toolkit.core.infra.fleet.list_nodes", lambda _root: [])
    check = KOMODO_PLUGIN().controller_access_checks(cfg, Path("."))[0]
    assert check.passed
    assert "skipped" in check.detail


def test_check_komodo_periphery_counts_active(monkeypatch):
    cfg = Config(domain="example.com", services=ServicesConfig(management=True))
    node = MagicMock()
    node.name = "vps1"
    node.reconciled = True
    node.services = ["komodo-periphery"]
    monkeypatch.setattr("toolkit.core.infra.fleet.list_nodes", lambda _root: [node])

    class Status:
        def __init__(self, active: bool):
            self._agent = HostIntegrationStatus("komodo-periphery", "Komodo Periphery", active, "test")

        def agent(self, integration: str):
            return self._agent if integration == "komodo-periphery" else None

    monkeypatch.setattr(
        "toolkit.core.infra.fleet.all_node_statuses",
        lambda _root: [Status(True), Status(False)],
    )
    check = KOMODO_PLUGIN().controller_access_checks(cfg, Path("."))[0]
    assert check.passed
    assert "1/2" in check.detail


def test_verify_grafana_alerting_loaded(monkeypatch, tmp_path):
    cfg = Config()
    auth_hdr = {"Authorization": "Basic x"}

    contact_points = '[{"uid":"homelab-ntfy","name":"homelab","type":"webhook"}]'
    alert_rules = '[{"uid":"homelab-instance-down","ruleGroup":"homelab-core","title":"Instance down"}]'

    def fake_curl(_cfg, _ip, container, url, **_kwargs):
        if container != "grafana":
            return 255, ""
        if "contact-points" in url:
            return 0, contact_points
        if "alert-rules" in url:
            return 0, alert_rules
        return 255, ""

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.docker_exec_curl", fake_curl)
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        lambda *_a, **_k: (0, "ok", ""),
    )
    checks = verify_grafana_alerting(cfg, tmp_path, grafana_address="10.0.0.1", auth_hdr=auth_hdr)

    by_key = {(c.service, c.check): c for c in checks}
    assert by_key[("grafana", "alerting_contact_point")].passed
    assert by_key[("grafana", "alerting_rules")].passed
    assert by_key[("grafana", "alerting_ntfy_delivery")].passed


def test_verify_grafana_alerting_missing_receiver(monkeypatch, tmp_path):
    cfg = Config()
    auth_hdr = {"Authorization": "Basic x"}

    def fake_curl(_cfg, _ip, container, url, **_kwargs):
        if container == "grafana" and "contact-points" in url:
            return 0, '[{"uid":"other","name":"x"}]'
        if container == "grafana" and "alert-rules" in url:
            return 0, "[]"
        return 255, ""

    # Mock ssh_run_on_vm to avoid real SSH calls during the ntfy delivery test.
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        lambda *a, **kw: (1, "", ""),
    )
    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.docker_exec_curl", fake_curl)
    checks = verify_grafana_alerting(cfg, tmp_path, grafana_address="10.0.0.1", auth_hdr=auth_hdr)
    by_key = {(c.service, c.check): c for c in checks}
    assert not by_key[("grafana", "alerting_contact_point")].passed
    assert not by_key[("grafana", "alerting_rules")].passed
