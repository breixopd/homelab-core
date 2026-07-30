from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

from toolkit.core.ansible.ansible_inventory import write_inventory
from toolkit.core.compose.docker import ContainerStatus, DockerCompose
from toolkit.core.config.config import Config, load_config, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.deploy.deploy import health_gate
from toolkit.core.generate.generate import generate_all
from toolkit.core.infra.hosts import add_host, list_hosts, remove_host


def _setup(root: Path) -> Config:
    cfg = Config(domain="example.com", email="admin@example.com")
    save_config(cfg, config_path(root))
    generate_all(root)
    return cfg


def test_health_gate_all_healthy():
    compose = MagicMock(spec=DockerCompose)
    compose.ps.return_value = [
        ContainerStatus(
            name="c1",
            service="postgres",
            state="running",
            health="healthy",
            image="pg:16",
        ),
        ContainerStatus(
            name="c2",
            service="redis",
            state="running",
            health="healthy",
            image="redis:7",
        ),
    ]
    result = health_gate(compose, ["postgres", "redis"], timeout=5, poll_interval=1)
    assert result["postgres"] is True
    assert result["redis"] is True


def test_health_gate_timeout():
    compose = MagicMock(spec=DockerCompose)
    compose.ps.return_value = [
        ContainerStatus(
            name="c1",
            service="postgres",
            state="running",
            health="unhealthy",
            image="pg:16",
        ),
    ]
    result = health_gate(compose, ["postgres"], timeout=2, poll_interval=1)
    assert result["postgres"] is False


def test_health_gate_no_healthcheck():
    """Services without HEALTHCHECK (health='') are treated as unhealthy.

    A service without a healthcheck (health='') is treated as unhealthy
    since all production services should have explicit healthchecks set
    on the container or in docker-compose.yml.
    """
    compose = MagicMock(spec=DockerCompose)
    compose.ps.return_value = [
        ContainerStatus(
            name="c1",
            service="sonarr",
            state="running",
            health="",
            image="sonarr:latest",
        ),
    ]
    result = health_gate(compose, ["sonarr"], timeout=5, poll_interval=1)
    assert result["sonarr"] is False


def test_add_remove_host(tmp_path: Path):
    _setup(tmp_path)
    add_host(tmp_path, "nas", "192.168.1.100")
    hosts = list_hosts(tmp_path)
    assert len(hosts) == 1
    assert hosts[0].name == "nas"

    removed = remove_host(tmp_path, "nas")
    assert removed is True
    assert len(list_hosts(tmp_path)) == 0


def test_remove_nonexistent_host(tmp_path: Path):
    _setup(tmp_path)
    assert remove_host(tmp_path, "nonexistent") is False


def test_ansible_inventory(tmp_path: Path, monkeypatch):
    _setup(tmp_path)
    add_host(tmp_path, "nas", "192.168.1.100", ssh_user="admin", ssh_port=2222)
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_inventory.resolve_proxmox_host",
        lambda _cfg, _root: "192.0.2.10",
    )
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_inventory.resolve_proxmox_proxy_key",
        lambda _cfg, _root: Path("/tmp/proxmox-key"),
    )
    path = write_inventory(tmp_path, load_config(config_path(tmp_path)))
    text = path.read_text()
    assert "infra-01" in text
    assert "10.10.10.10" in text
    assert "external_hosts" in text
    assert "192.168.1.100" in text


def test_deploy_local_uses_role_compose_and_role_profiles(tmp_path: Path, monkeypatch):
    import toolkit.core.deploy.deploy as deploy_mod
    from toolkit.core.deploy.deploy import deploy_local

    cfg = _setup(tmp_path)
    role_compose = tmp_path / "generated" / "infra" / "compose.yaml"
    role_compose.parent.mkdir(parents=True, exist_ok=True)
    role_compose.write_text("name: homelab\n")

    instances: list = []

    class FakeCompose:
        def __init__(self, compose_file, env_file=None, project_name=None):
            instances.append((compose_file, env_file))

        def pull_retry(self, services=None, profiles=None):
            return True

        def pull(self, services=None):
            return True

        def up(self, services=None, detach=True, profiles=None):
            instances.append(("up", profiles))
            return True

        def ps(self):
            return []

    monkeypatch.setattr("toolkit.core.compose.docker.DockerCompose", FakeCompose)
    monkeypatch.setattr(deploy_mod, "health_gate", lambda *a, **k: {})

    r = deploy_local(tmp_path, "infra", cfg)
    assert r.success
    assert instances[0][0] == role_compose
    _up = next(x for x in instances if isinstance(x, tuple) and x[0] == "up")
    profs = set(_up[1] or [])
    assert "management" in profs
    assert "notifications" in profs
