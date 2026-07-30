from __future__ import annotations

from unittest.mock import MagicMock, patch

from tests.helpers.machines import renamed_default_machines
from toolkit.core.compose.docker import ContainerStatus
from toolkit.core.compose.registry import get_category, load_all
from toolkit.core.config.config import Config
from toolkit.core.deploy.deploy_workflow import run_post_start_hooks, wait_for_healthy


def _category(name: str):
    load_all()
    return get_category(name)


def test_wait_for_healthy_timeout():
    docker = MagicMock()
    docker.ps.return_value = []
    assert wait_for_healthy(docker, "test-service", timeout=1) is False


def test_wait_for_healthy_success():
    docker = MagicMock()
    docker.ps.return_value = [
        ContainerStatus(
            name="c1",
            service="test-service",
            state="running",
            health="healthy",
            image="test-service:latest",
        ),
    ]
    assert wait_for_healthy(docker, "test-service", timeout=5) is True


def test_run_post_start_hooks_no_compose(tmp_path):
    results = run_post_start_hooks(Config(domain="test.local"), tmp_path)
    assert results == {"local": ["No generated Compose application - skipped service setup."]}


def test_run_post_start_hooks_with_running_service(tmp_path):
    cfg = Config(domain="test.local")
    role_compose = tmp_path / "generated" / "infra" / "compose.yaml"
    role_compose.parent.mkdir(parents=True)
    role_compose.write_text("name: homelab\n")
    running = [
        ContainerStatus(
            name="c1",
            service="example-service",
            state="running",
            health="healthy",
            image="example-service:latest",
        ),
    ]

    plugin = MagicMock()
    plugin._yaml_data = {"runtime": "container"}
    plugin.is_enabled.return_value = True
    plugin.post_start.return_value = ["test log"]
    with (
        patch(
            "toolkit.core.deploy.deploy_workflow.compose_for_root",
            return_value=MagicMock(ps=MagicMock(return_value=running)),
        ),
        patch("toolkit.services.load_service_plugins", return_value={"example-service": plugin}),
        patch("toolkit.core.secrets.secrets.load_runtime_secrets", return_value={}),
    ):
        results = run_post_start_hooks(cfg, tmp_path)

    assert results["management::example-service"] == ["test log"]


def test_run_post_start_hooks_loads_custom_control_node_secrets(tmp_path):
    machines = renamed_default_machines()
    cfg = Config(domain="test.local", machines={"core": machines["core"]})
    category = _category("management")
    running = [
        ContainerStatus(
            name="example-service",
            service="example-service",
            state="running",
            health="healthy",
            image="example-service:latest",
        )
    ]
    plugin = MagicMock()
    plugin._yaml_data = {"runtime": "container"}
    plugin.is_enabled.return_value = True
    plugin.post_start.return_value = []

    with (
        patch("toolkit.core.deploy.deploy_workflow.enabled_categories", return_value=[category]),
        patch(
            "toolkit.core.deploy.deploy_workflow.compose_for_root",
            return_value=MagicMock(ps=MagicMock(return_value=running)),
        ),
        patch("toolkit.services.load_service_plugins", return_value={"example-service": plugin}),
        patch("toolkit.core.secrets.secrets.load_runtime_secrets", return_value={}) as load_secrets,
    ):
        run_post_start_hooks(cfg, tmp_path)

    load_secrets.assert_called_once_with(tmp_path, role="core")


def test_run_post_start_hooks_runs_embedded_plugins_without_a_container(tmp_path):
    cfg = Config(domain="test.local")
    category = _category("management")
    plugin = MagicMock()
    plugin._yaml_data = {"runtime": "embedded"}
    plugin.is_enabled.return_value = True
    plugin.post_start.return_value = ["embedded setup complete"]
    running = [
        ContainerStatus(
            name="example-service",
            service="example-service",
            state="running",
            health="healthy",
            image="example-service:latest",
        )
    ]

    with (
        patch("toolkit.core.deploy.deploy_workflow.enabled_categories", return_value=[category]),
        patch(
            "toolkit.core.deploy.deploy_workflow.compose_for_root",
            return_value=MagicMock(ps=MagicMock(return_value=running)),
        ),
        patch("toolkit.services.load_service_plugins", return_value={"embedded-tool": plugin}),
        patch("toolkit.core.secrets.secrets.load_runtime_secrets", return_value={}),
    ):
        results = run_post_start_hooks(cfg, tmp_path)

    plugin.post_start.assert_called_once()
    assert results["management::embedded-tool"] == ["embedded setup complete"]
