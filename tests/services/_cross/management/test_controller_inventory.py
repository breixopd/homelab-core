from __future__ import annotations

import json
import subprocess
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
from toolkit.controller.inventory_api import (
    InventoryRequestError,
    read_container_inventory,
    read_services_view,
)
from toolkit.core.config.config import Config, ServicesConfig, save_config
from toolkit.core.config.storage import config_path


def _root(tmp_path: Path, *, media: bool = False, security: bool = False) -> Path:
    save_config(
        Config(
            domain="example.test",
            email="owner@example.test",
            owner_password="owner-password-canary",
            services=ServicesConfig(
                management=True,
                media=media,
                cloud=False,
                notifications=False,
                email=False,
                security=security,
            ),
            proxmox={"provision_machines": False},
        ),
        config_path(tmp_path),
    )
    return tmp_path


def test_services_view_contains_renderable_metadata_without_config_or_secrets(tmp_path: Path) -> None:
    view = read_services_view(_root(tmp_path), family=False, groups=[])
    serialized = view.model_dump_json()

    assert view.domain == "example.test"
    assert any(category.services for category in view.categories)
    assert "owner@example.test" not in serialized
    assert "owner-password-canary" not in serialized
    assert all(service.is_manageable for category in view.categories for service in category.services)
    services = {service.name: service for category in view.categories for service in category.services}
    assert services["grafana"].label == "Grafana"
    assert [(route.exposure, route.auth_mode, route.scope) for route in services["grafana"].routes] == [
        ("private", "oidc", "default")
    ]
    assert {(route.auth_mode, route.scope) for route in services["homelab-ui"].routes} == {
        ("native", "exact: /health"),
        ("native", "prefix: /invite/*"),
        ("forward_auth", "default"),
    }
    assert '"internal"' not in serialized
    assert '"mesh"' not in serialized


def test_family_services_rejects_unknown_access_group(tmp_path: Path) -> None:
    with pytest.raises(InventoryRequestError, match="invalid service access group"):
        read_services_view(_root(tmp_path, media=True), family=True, groups=["lldap_admin"])


def test_operator_services_ignore_non_service_directory_groups(tmp_path: Path) -> None:
    view = read_services_view(_root(tmp_path), family=False, groups=["homelab-users", "lldap_admin"])

    assert any(category.services for category in view.categories)


def test_container_inventory_parses_bounded_docker_json(monkeypatch, tmp_path: Path) -> None:
    root = _root(tmp_path)
    rows = [
        {
            "Names": "grafana",
            "Status": "Up 5 minutes (healthy)",
            "State": "running",
            "Image": "grafana/grafana:12.0.0",
        },
        {
            "Names": "postgres",
            "Status": "Exited (1) 2 minutes ago",
            "State": "exited",
            "Image": "postgres:17",
        },
    ]

    def run(command, **_kwargs):
        assert command == ["docker", "ps", "-a", "--format", "{{json .}}"]
        return subprocess.CompletedProcess(
            command,
            0,
            stdout="\n".join(json.dumps(row) for row in rows),
            stderr="",
        )

    monkeypatch.setattr("toolkit.controller.inventory_api.subprocess.run", run)
    inventory = read_container_inventory(root)

    assert inventory.is_available is True
    assert inventory.unavailable_nodes == []
    assert [(item.name, item.health) for item in inventory.containers] == [
        ("grafana", "healthy"),
        ("postgres", "none"),
    ]


def test_container_inventory_classifies_only_declared_successful_oneshots(monkeypatch, tmp_path: Path) -> None:
    root = _root(tmp_path, security=True)
    rows = [
        {
            "Names": "wazuh-indexer-certs-init",
            "Status": "Exited (0) 2 minutes ago",
            "State": "exited",
            "Image": "wazuh-indexer:latest",
        },
        {
            "Names": "ordinary-daemon",
            "Status": "Exited (0) 2 minutes ago",
            "State": "exited",
            "Image": "example:latest",
        },
    ]
    monkeypatch.setattr(
        "toolkit.controller.inventory_api._docker_ps",
        lambda *_args: (True, "\n".join(json.dumps(row) for row in rows)),
    )

    inventory = read_container_inventory(root)

    assert {item.name: item.completed for item in inventory.containers} == {
        "ordinary-daemon": False,
        "wazuh-indexer-certs-init": True,
    }


def test_container_inventory_reports_node_unavailable_without_error_leak(monkeypatch, tmp_path: Path) -> None:
    root = _root(tmp_path)

    def fail(*_args, **_kwargs):
        raise OSError("private credential path /root/.ssh/id_ed25519")

    monkeypatch.setattr("toolkit.controller.inventory_api.subprocess.run", fail)
    inventory = read_container_inventory(root)

    assert inventory.is_available is False
    assert inventory.unavailable_nodes == ["infra"]
    assert "credential" not in inventory.model_dump_json()


def test_container_inventory_collects_nodes_in_parallel_with_bounded_workers(monkeypatch, tmp_path: Path) -> None:
    root = _root(tmp_path)
    cfg = SimpleNamespace(
        enabled_nodes=["infra", "apps", "media"],
        control_node="infra",
        proxmox=SimpleNamespace(provision_machines=True),
    )
    monkeypatch.setattr("toolkit.controller.inventory_api.load_config", lambda _path: cfg)
    monkeypatch.setattr("toolkit.controller.inventory_api.service_is_enabled", lambda *_args: False)

    active = 0
    max_active = 0
    lock = threading.Lock()
    started = threading.Barrier(3)

    def collect(_cfg, _root, node):
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        # Wait for every submitted worker so this assertion proves overlap,
        # while completion order still differs from configured order.
        started.wait(timeout=2)
        with lock:
            active -= 1
        if node == "apps":
            return False, ""
        row = {"Names": node, "Status": "Up 1 minute", "State": "running", "Image": "test:latest"}
        return True, json.dumps(row)

    monkeypatch.setattr("toolkit.controller.inventory_api._docker_ps", collect)

    inventory = read_container_inventory(root)

    assert max_active == 3
    assert inventory.unavailable_nodes == ["apps"]
    assert [item.node for item in inventory.containers] == ["infra", "media"]
