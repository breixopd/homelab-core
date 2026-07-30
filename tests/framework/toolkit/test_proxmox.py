from __future__ import annotations

from toolkit.core.infra.proxmox import (
    _api_get_with_retry,
    choose_proxmox_node,
    detect_lxc_ip,
    inspect_proxmox_host,
    list_proxmox_vms,
    lxc_features_config,
    lxc_network_config,
    normalize_proxmox_token,
    suggest_zfs_disk_devices,
    validate_proxmox_url,
)


def test_normalize_proxmox_token_full_value():
    token_id, token_secret = normalize_proxmox_token("root@pam!terraform=secret")
    assert token_id == "root@pam!terraform"
    assert token_secret == "secret"


def test_validate_proxmox_url_adds_api_suffix():
    assert validate_proxmox_url("https://10.0.0.1:8006") == "https://10.0.0.1:8006/api2/json"


def test_list_proxmox_vms_uses_authoritative_qemu_inventory(monkeypatch) -> None:
    requested: list[str] = []

    def request(_base_url, path, _auth_value, *, verify_ssl, ca_file=None):
        requested.append(path)
        return {"data": [{"name": "worker-vm-01", "vmid": 820}]}

    monkeypatch.setattr("toolkit.core.infra.proxmox._api_get_with_retry", request)

    inventory = list_proxmox_vms(
        "https://10.0.0.1:8006",
        "root@pam!terraform",
        "secret",
        "pve",
        ca_file="/tmp/ca.pem",
    )

    assert inventory == [{"name": "worker-vm-01", "vmid": 820}]
    assert requested == ["nodes/pve/qemu"]


def test_inspect_proxmox_host_recommends_existing(monkeypatch):
    def fake_api_get(base_url, path, auth_value, *, verify_ssl):
        if path == "nodes/pve/status":
            return {"data": {"status": "online"}}
        if path == "nodes/pve/qemu":
            return {
                "data": [
                    {
                        "vmid": 9000,
                        "name": "debian-12-cloudinit-template",
                        "status": "stopped",
                        "template": 1,
                    },
                    {"vmid": 800, "name": "infra-01", "status": "running"},
                    {"vmid": 801, "name": "media-01", "status": "running"},
                    {"vmid": 802, "name": "apps-01", "status": "running"},
                ]
            }
        if path == "nodes/pve/lxc":
            return {"data": []}
        raise AssertionError(path)

    monkeypatch.setattr("toolkit.core.infra.proxmox._api_get", fake_api_get)
    monkeypatch.setattr(
        "toolkit.core.infra.proxmox._detect_vm_ip",
        lambda *args, **kwargs: {
            800: "10.10.10.10",
            801: "10.10.10.11",
            802: "10.10.10.12",
        }[args[2]],
    )

    report = inspect_proxmox_host(
        "https://10.0.0.1:8006",
        "root@pam!terraform",
        "secret",
    )

    assert report.ok is True
    assert report.recommendation == "existing"
    assert report.missing_machines == []
    assert set(report.existing_machines) == {"infra", "media", "apps"}
    assert report.existing_machines["infra"].ip == "10.10.10.10"


def test_inspect_proxmox_host_recommends_provision_when_no_role_vms(monkeypatch):
    def fake_api_get(base_url, path, auth_value, *, verify_ssl):
        if path == "nodes/pve/status":
            return {"data": {"status": "online"}}
        if path == "nodes/pve/qemu":
            return {
                "data": [
                    {
                        "vmid": 9000,
                        "name": "debian-12-cloudinit-template",
                        "status": "stopped",
                        "template": 1,
                    }
                ]
            }
        if path == "nodes/pve/lxc":
            return {"data": []}
        raise AssertionError(path)

    monkeypatch.setattr("toolkit.core.infra.proxmox._api_get", fake_api_get)

    report = inspect_proxmox_host(
        "https://10.0.0.1:8006",
        "root@pam!terraform",
        "secret",
    )

    assert report.ok is True
    assert report.recommendation == "provision"
    assert report.missing_machines == ["infra", "media", "apps"]
    assert report.message == "No desired machines exist on node pve; OpenTofu can provision them."


def test_inspect_proxmox_host_honors_an_explicit_empty_machine_set(monkeypatch):
    def fake_api_get(base_url, path, auth_value, *, verify_ssl):
        if path == "nodes/pve/status":
            return {"data": {"status": "online"}}
        if path in {"nodes/pve/qemu", "nodes/pve/lxc"}:
            return {"data": []}
        raise AssertionError(path)

    monkeypatch.setattr("toolkit.core.infra.proxmox._api_get", fake_api_get)

    report = inspect_proxmox_host(
        "https://10.0.0.1:8006",
        "root@pam!terraform",
        "secret",
        machines={},
    )

    assert report.ok is True
    assert report.missing_machines == []
    assert report.existing_machines == {}
    assert report.message == "No desired machines exist on node pve; OpenTofu can provision them."


def test_inspect_proxmox_host_reports_mixed_state(monkeypatch):
    def fake_api_get(base_url, path, auth_value, *, verify_ssl):
        if path == "nodes/pve/status":
            return {"data": {"status": "online"}}
        if path == "nodes/pve/qemu":
            return {
                "data": [
                    {
                        "vmid": 9000,
                        "name": "debian-12-cloudinit-template",
                        "status": "stopped",
                        "template": 1,
                    },
                    {"vmid": 800, "name": "infra-01", "status": "running"},
                ]
            }
        if path == "nodes/pve/lxc":
            return {"data": []}
        raise AssertionError(path)

    monkeypatch.setattr("toolkit.core.infra.proxmox._api_get", fake_api_get)
    monkeypatch.setattr("toolkit.core.infra.proxmox._detect_vm_ip", lambda *args, **kwargs: "10.10.10.10")

    report = inspect_proxmox_host("https://10.0.0.1:8006", "root@pam!terraform", "secret")

    assert report.ok is True
    assert report.recommendation == "mixed"
    assert report.missing_machines == ["media", "apps"]


def test_inspect_proxmox_host_detects_lxc_containers(monkeypatch):
    def fake_api_get(base_url, path, auth_value, *, verify_ssl):
        if path == "nodes/pve/status":
            return {"data": {"status": "online"}}
        if path == "nodes/pve/qemu":
            return {
                "data": [
                    {
                        "vmid": 9000,
                        "name": "debian-12-cloudinit-template",
                        "status": "stopped",
                        "template": 1,
                    }
                ]
            }
        if path == "nodes/pve/lxc":
            return {
                "data": [
                    {"vmid": 100, "name": "infra-01", "status": "running"},
                    {"vmid": 101, "name": "media-01", "status": "running"},
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr("toolkit.core.infra.proxmox._api_get", fake_api_get)
    monkeypatch.setattr("toolkit.core.infra.proxmox._detect_vm_ip", lambda *args, **kwargs: "")
    monkeypatch.setattr("toolkit.core.infra.proxmox.detect_lxc_ip", lambda *args, **kwargs: "10.10.10.10")

    report = inspect_proxmox_host("https://10.0.0.1:8006", "root@pam!terraform", "secret")

    assert report.ok is True
    assert report.recommendation == "mixed"
    assert report.missing_machines == ["apps"]
    assert report.existing_machines["infra"].type == "lxc"
    assert report.existing_machines["infra"].vmid == 100
    assert report.existing_machines["media"].type == "lxc"
    assert report.existing_machines["media"].vmid == 101


def test_choose_proxmox_node_prefers_single_online_node(monkeypatch):
    def fake_api_get(base_url, path, auth_value, *, verify_ssl):
        if path == "nodes":
            return {"data": [{"node": "lab-pve", "status": "online"}]}
        raise AssertionError(path)

    monkeypatch.setattr("toolkit.core.infra.proxmox._api_get", fake_api_get)

    node, source = choose_proxmox_node("https://10.0.0.1:8006", "root@pam!terraform", "secret", "")

    assert node == "lab-pve"
    assert source == "auto-single-online"


def test_choose_proxmox_node_falls_back_when_preferred_not_found(monkeypatch):
    def fake_api_get(base_url, path, auth_value, *, verify_ssl):
        if path == "nodes":
            return {"data": [{"node": "actual-node", "status": "online"}]}
        raise AssertionError(path)

    monkeypatch.setattr("toolkit.core.infra.proxmox._api_get", fake_api_get)

    node, source = choose_proxmox_node(
        "https://10.0.0.1:8006",
        "root@pam!terraform",
        "secret",
        preferred_node="nonexistent",
    )

    assert node == "actual-node"
    assert source == "auto-single-online"


def test_choose_proxmox_node_returns_first_online_when_preferred_missing(monkeypatch):
    def fake_api_get(base_url, path, auth_value, *, verify_ssl):
        if path == "nodes":
            return {
                "data": [
                    {"node": "node-a", "status": "online"},
                    {"node": "node-b", "status": "online"},
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr("toolkit.core.infra.proxmox._api_get", fake_api_get)

    node, source = choose_proxmox_node(
        "https://10.0.0.1:8006",
        "root@pam!terraform",
        "secret",
        preferred_node="nonexistent",
    )

    assert node in ("node-a", "node-b")
    assert source == "auto-first-online"


def test_suggest_zfs_disk_devices_filters_used_and_system_disks(monkeypatch):
    def fake_api_get(base_url, path, auth_value, *, verify_ssl):
        if path == "nodes/pve/disks/list":
            return {
                "data": [
                    {"devpath": "/dev/sda", "used": "LVM", "partitions": 3},
                    {"devpath": "/dev/sdb", "used": "", "partitions": []},
                    {"devpath": "/dev/nvme0n1", "used": "unused", "filesystem": ""},
                    {"devpath": "/dev/loop0", "used": ""},
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr("toolkit.core.infra.proxmox._api_get", fake_api_get)

    disks = suggest_zfs_disk_devices("https://10.0.0.1:8006", "root@pam!terraform", "secret")

    assert disks == ["sdb", "nvme0n1"]


def test_suggest_zfs_disk_devices_filters_osdid_zero(monkeypatch):
    """osdid=0 should be treated as used (not falsy)."""

    def fake_api_get(base_url, path, auth_value, *, verify_ssl):
        if path == "nodes/pve/disks/list":
            return {
                "data": [
                    {"devpath": "/dev/sda", "used": "", "partitions": [], "osdid": 0},
                    {"devpath": "/dev/sdb", "used": "", "partitions": []},
                ]
            }
        raise AssertionError(path)

    monkeypatch.setattr("toolkit.core.infra.proxmox._api_get", fake_api_get)

    disks = suggest_zfs_disk_devices("https://10.0.0.1:8006", "root@pam!terraform", "secret")

    assert disks == ["sdb"]


def test_api_get_with_retry_returns_error_dict_on_failure(monkeypatch):
    import urllib.error

    call_count = 0

    def fake_api_get(base_url, path, auth_value, *, verify_ssl):
        nonlocal call_count
        call_count += 1
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr("toolkit.core.infra.proxmox._api_get", fake_api_get)

    result = _api_get_with_retry("https://10.0.0.1:8006/api2/json", "nodes", "PVEAPIToken=token", verify_ssl=True)
    assert result.get("error") is True
    assert "failed after" in result.get("message", "")
    assert call_count == 3


def test_detect_lxc_ip_parses_net_config(monkeypatch):
    def fake_api_get(base_url, path, auth_value, *, verify_ssl):
        if path == "nodes/pve/lxc/100/config":
            return {"data": {"net0": "name=eth0,bridge=vmbr1,ip=10.10.10.10/24,gw=10.10.10.1"}}
        raise AssertionError(path)

    monkeypatch.setattr("toolkit.core.infra.proxmox._api_get", fake_api_get)

    ip = detect_lxc_ip(
        "https://10.0.0.1:8006/api2/json",
        "pve",
        100,
        "PVEAPIToken=token",
        verify_ssl=True,
    )
    assert ip == "10.10.10.10"


def test_detect_lxc_ip_returns_empty_on_no_match(monkeypatch):
    def fake_api_get(base_url, path, auth_value, *, verify_ssl):
        if path == "nodes/pve/lxc/100/config":
            return {"data": {"net0": "name=eth0,bridge=vmbr1"}}
        raise AssertionError(path)

    monkeypatch.setattr("toolkit.core.infra.proxmox._api_get", fake_api_get)

    ip = detect_lxc_ip(
        "https://10.0.0.1:8006/api2/json",
        "pve",
        100,
        "PVEAPIToken=token",
        verify_ssl=True,
    )
    assert ip == ""


def test_lxc_network_config():
    config = lxc_network_config("infra", "10.10.10.10", "10.10.10.1", cidr=24, bridge="vmbr1")
    assert config == {
        "name": "infra",
        "bridge": "vmbr1",
        "ip": "10.10.10.10/24",
        "gw": "10.10.10.1",
    }


def test_lxc_network_config_defaults():
    config = lxc_network_config("media", "10.10.10.11", "10.10.10.1")
    assert config["bridge"] == "vmbr1"
    assert config["ip"] == "10.10.10.11/24"


def test_lxc_features_config():
    assert lxc_features_config(for_tofu=True) == {"nesting": True}
    assert lxc_features_config() == {"nesting": True, "keyctl": True}
