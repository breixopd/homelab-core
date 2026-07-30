"""Tests for toolkit/core/hooks.py"""

from unittest.mock import MagicMock, patch

from toolkit.categories import Category, Service
from toolkit.core.compose.docker import ContainerStatus, DockerCompose
from toolkit.core.config.config import Config


class DummyCategory(Category):
    def __init__(self):
        super().__init__(
            name="test",
            label="Test Category",
            compose_file="test.yml",
            placement="infra",
            description="Test category",
            priority=100,
        )

    def services(self, config: Config) -> list[Service]:
        return [Service(name="svc1", label="Service 1", description="Test svc")]


def _running_compose() -> MagicMock:
    return MagicMock(
        ps=MagicMock(
            return_value=[
                ContainerStatus(
                    name="container",
                    service="container",
                    state="running",
                    health="healthy",
                    image="image",
                )
            ]
        )
    )


class TestWaitForHealthy:
    def test_returns_true_when_healthy(self):
        from toolkit.core.deploy.deploy_workflow import wait_for_healthy

        dc = MagicMock(spec=DockerCompose)
        dc.ps.return_value = [
            ContainerStatus(
                name="svc1",
                service="svc1",
                state="running",
                health="healthy",
                image="img",
            ),
        ]

        result = wait_for_healthy(dc, "svc1", timeout=5)

        assert result is True
        dc.ps.assert_called()

    def test_returns_false_on_timeout(self):
        from toolkit.core.deploy.deploy_workflow import wait_for_healthy

        dc = MagicMock(spec=DockerCompose)
        dc.ps.return_value = [
            ContainerStatus(name="svc1", service="svc1", state="running", health=None, image="img"),
        ]

        with patch("time.sleep"):
            result = wait_for_healthy(dc, "svc1", timeout=2)

        assert result is False

    def test_returns_false_when_service_not_found(self):
        from toolkit.core.deploy.deploy_workflow import wait_for_healthy

        dc = MagicMock(spec=DockerCompose)
        dc.ps.return_value = [
            ContainerStatus(name="other", service="other", state="running", health=None, image="img"),
        ]

        with patch("time.sleep"):
            result = wait_for_healthy(dc, "missing", timeout=2)

        assert result is False

    def test_becomes_healthy_after_delay(self):
        from toolkit.core.deploy.deploy_workflow import wait_for_healthy

        dc = MagicMock(spec=DockerCompose)
        unhealthy = [
            ContainerStatus(name="svc1", service="svc1", state="running", health=None, image="img"),
        ]
        healthy = [
            ContainerStatus(
                name="svc1",
                service="svc1",
                state="running",
                health="healthy",
                image="img",
            ),
        ]
        dc.ps.side_effect = [unhealthy, healthy]

        with patch("time.sleep"):
            result = wait_for_healthy(dc, "svc1", timeout=10)

        assert result is True
        assert dc.ps.call_count == 2

    def test_multiple_containers_same_service(self):
        from toolkit.core.deploy.deploy_workflow import wait_for_healthy

        dc = MagicMock(spec=DockerCompose)
        dc.ps.return_value = [
            ContainerStatus(name="svc1-1", service="svc1", state="running", health=None, image="img"),
            ContainerStatus(
                name="svc1-2",
                service="svc1",
                state="running",
                health="healthy",
                image="img",
            ),
        ]

        result = wait_for_healthy(dc, "svc1", timeout=5)

        assert result is True

    def test_unhealthy_container_not_ignored(self):
        from toolkit.core.deploy.deploy_workflow import wait_for_healthy

        dc = MagicMock(spec=DockerCompose)
        dc.ps.return_value = [
            ContainerStatus(
                name="svc1",
                service="svc1",
                state="running",
                health="unhealthy",
                image="img",
            ),
        ]

        with patch("time.sleep"):
            result = wait_for_healthy(dc, "svc1", timeout=2)

        assert result is False


class TestRunPostStartHooks:
    def test_runtime_credentials_discovered_by_early_category_reach_later_hooks(self, tmp_path):
        from toolkit.core.deploy.deploy_workflow import run_post_start_hooks

        cfg = Config(domain="example.com")
        first_category = DummyCategory()
        second_category = DummyCategory()
        second_category.name = "later"
        first = MagicMock()
        first._yaml_data = {"runtime": "embedded"}
        first.is_enabled.return_value = True

        def discover(_cfg, secrets, **_kwargs):
            secrets["RUNTIME_API_KEY"] = "discovered"
            return []

        first.post_start.side_effect = discover
        second = MagicMock()
        second._yaml_data = {"runtime": "embedded"}
        second.is_enabled.return_value = True

        def consume(_cfg, secrets, **_kwargs):
            assert secrets["RUNTIME_API_KEY"] == "discovered"
            return []

        second.post_start.side_effect = consume

        with (
            patch("toolkit.core.deploy.deploy_workflow.load_all"),
            patch(
                "toolkit.core.deploy.deploy_workflow.enabled_categories",
                return_value=[first_category, second_category],
            ),
            patch(
                "toolkit.services.load_service_plugins",
                side_effect=[{"first": first}, {"second": second}],
            ),
            patch("toolkit.core.deploy.deploy_workflow.compose_for_root", return_value=_running_compose()),
            patch("toolkit.core.secrets.secrets.load_runtime_secrets", return_value={}),
        ):
            run_post_start_hooks(cfg, tmp_path)

    def test_runs_plugins_in_dependency_order_from_loader(self, tmp_path):
        from toolkit.core.deploy.deploy_workflow import run_post_start_hooks

        cfg = Config(domain="example.com")
        events: list[str] = []
        category = DummyCategory()
        first = MagicMock()
        first._yaml_data = {"runtime": "embedded"}
        first.is_enabled.return_value = True
        first.post_start.side_effect = lambda *_args, **_kwargs: events.append("first") or []
        second = MagicMock()
        second._yaml_data = {"runtime": "embedded"}
        second.is_enabled.return_value = True
        second.post_start.side_effect = lambda *_args, **_kwargs: events.append("second") or []
        progress: list[str] = []

        with (
            patch("toolkit.core.deploy.deploy_workflow.load_all"),
            patch("toolkit.core.deploy.deploy_workflow.enabled_categories", return_value=[category]),
            patch(
                "toolkit.services.load_service_plugins",
                return_value={"first": first, "second": second},
            ),
            patch("toolkit.core.deploy.deploy_workflow.compose_for_root", return_value=_running_compose()),
            patch("toolkit.core.secrets.secrets.load_runtime_secrets", return_value={}),
        ):
            run_post_start_hooks(cfg, tmp_path, on_progress=progress.append)

        assert events == ["first", "second"]
        assert any("first: applying service setup" in line for line in progress)
        assert any("second: setup complete" in line for line in progress)
        assert all("integration complete" not in line for line in progress)

    def test_plugin_failure_does_not_hide_or_skip_later_service(self, tmp_path):
        from toolkit.core.deploy.deploy_workflow import run_post_start_hooks

        cfg = Config(domain="example.com")
        failing = MagicMock()
        failing._yaml_data = {"runtime": "embedded"}
        failing.is_enabled.return_value = True
        failing.post_start.side_effect = RuntimeError("intentional failure")
        healthy = MagicMock()
        healthy._yaml_data = {"runtime": "embedded"}
        healthy.is_enabled.return_value = True
        healthy.post_start.return_value = ["configured"]

        with (
            patch("toolkit.core.deploy.deploy_workflow.load_all"),
            patch("toolkit.core.deploy.deploy_workflow.enabled_categories", return_value=[DummyCategory()]),
            patch(
                "toolkit.services.load_service_plugins",
                return_value={"failing": failing, "healthy": healthy},
            ),
            patch("toolkit.core.deploy.deploy_workflow.compose_for_root", return_value=_running_compose()),
            patch("toolkit.core.secrets.secrets.load_runtime_secrets", return_value={}),
        ):
            results = run_post_start_hooks(cfg, tmp_path)

        assert results["test::failing"] == ["Plugin error: intentional failure"]
        assert results["test::healthy"] == ["configured"]
        healthy.post_start.assert_called_once()

    def test_skips_disabled_plugin(self, tmp_path):
        from toolkit.core.deploy.deploy_workflow import run_post_start_hooks

        cfg = Config(domain="example.com")
        plugin = MagicMock()
        plugin._yaml_data = {"runtime": "embedded"}
        plugin.is_enabled.return_value = False

        with (
            patch("toolkit.core.deploy.deploy_workflow.load_all"),
            patch("toolkit.core.deploy.deploy_workflow.enabled_categories", return_value=[DummyCategory()]),
            patch("toolkit.services.load_service_plugins", return_value={"disabled": plugin}),
            patch("toolkit.core.deploy.deploy_workflow.compose_for_root", return_value=_running_compose()),
            patch("toolkit.core.secrets.secrets.load_runtime_secrets", return_value={}),
        ):
            results = run_post_start_hooks(cfg, tmp_path)

        assert results == {}
        plugin.post_start.assert_not_called()

    def test_skips_plugin_owned_by_another_node(self, tmp_path):
        from toolkit.core.deploy.deploy_workflow import run_post_start_hooks

        cfg = Config(domain="example.com")
        plugin = MagicMock()
        plugin._yaml_data = {"runtime": "embedded"}
        plugin.is_enabled.return_value = True
        plugin.runtime_node.return_value = "apps"

        with (
            patch("toolkit.core.deploy.deploy_workflow.load_all"),
            patch("toolkit.core.deploy.deploy_workflow.enabled_categories", return_value=[DummyCategory()]),
            patch("toolkit.services.load_service_plugins", return_value={"remote": plugin}),
            patch("toolkit.core.deploy.deploy_workflow.compose_for_root", return_value=_running_compose()),
            patch("toolkit.core.secrets.secrets.load_runtime_secrets", return_value={}),
        ):
            results = run_post_start_hooks(cfg, tmp_path, vm="infra")

        assert results == {}
        plugin.post_start.assert_not_called()

    def test_empty_results_when_no_categories(self, tmp_path):
        from toolkit.core.deploy.deploy_workflow import run_post_start_hooks

        cfg = Config(domain="example.com")

        with (
            patch("toolkit.core.deploy.deploy_workflow.load_all"),
            patch("toolkit.core.deploy.deploy_workflow.enabled_categories", return_value=[]),
        ):
            results = run_post_start_hooks(cfg, tmp_path)

        assert results == {}
