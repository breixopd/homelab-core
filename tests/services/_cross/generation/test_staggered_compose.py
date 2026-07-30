"""Tests for staggered compose wave ordering and runner behavior."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from toolkit.core.deploy.staggered_compose import (
    StaggeredComposeRunner,
    _compose_ps_args,
    _service_health_from_ps_json,
    run_staggered_compose,
    unplanned_active_services,
)


def test_wave_coverage_reports_active_unplanned_services(tmp_path: Path) -> None:
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        "services:\n"
        "  planned:\n    profiles: [cloud]\n"
        "  missing:\n    profiles: [cloud]\n"
        "  inactive:\n    profiles: [dev]\n",
        encoding="utf-8",
    )

    assert unplanned_active_services(compose, frozenset({"cloud"}), {"planned"}) == {"missing"}


def test_manifest_planner_supports_arbitrary_node_and_service_plugins(tmp_path: Path) -> None:
    from toolkit.core.config.config import Config
    from toolkit.core.machines import MachineSpec
    from toolkit.core.registry.stagger_planner import compose_stagger_waves

    services_root = tmp_path / "toolkit/services"
    for name, priority, dependencies in (("database", 5, []), ("api", 20, ["database"])):
        service_root = services_root / name
        service_root.mkdir(parents=True)
        service_root.joinpath("service.yaml").write_text(
            f"name: {name}\nlabel: {name.title()}\ndescription: Test service\nicon: test\n"
            f"category: management\nplacement: worker-west\npriority: {priority}\n"
            f"depends_on: {dependencies!r}\n"
        )
        service_root.joinpath("compose.yaml").write_text(
            f"services:\n  {name}:\n    image: example/{name}:1@sha256:{'a' * 64}\n"
            "    logging:\n      driver: json-file\n      options:\n        max-size: 10m\n        max-file: '3'\n"
        )
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        f"services:\n  api:\n    image: example/api:1@sha256:{'a' * 64}\n"
        f"  database:\n    image: example/database:1@sha256:{'b' * 64}\n"
    )
    cfg = Config(
        machines={
            "worker-west": MachineSpec(
                hostname="worker-07",
                address="10.10.10.27",
                gateway="10.10.10.1",
                vmid=827,
                labels=("control",),
            )
        }
    )

    waves = compose_stagger_waves(tmp_path, cfg, "worker-west", compose_path=compose)

    assert [(wave.name, wave.services) for wave in waves] == [
        ("database", ("database",)),
        ("api", ("api",)),
    ]


def test_builtin_planner_keeps_multi_container_plugin_together(tmp_path: Path) -> None:
    import yaml
    from toolkit.core.config.config import Config
    from toolkit.core.generate.compose_assemble import assemble_role_compose_text
    from toolkit.core.registry.stagger_planner import compose_stagger_waves

    root = Path.cwd()
    cfg = Config(services={"security": True})
    compose = tmp_path / "compose.yaml"
    compose.write_text(assemble_role_compose_text(root, cfg, "infra"))
    model = yaml.safe_load(compose.read_text())
    profiles = frozenset(profile for service in model["services"].values() for profile in service.get("profiles", []))

    waves = compose_stagger_waves(root, cfg, "infra", compose_path=compose, profiles=profiles)
    by_name = {wave.name: wave.services for wave in waves}

    assert by_name["wazuh-indexer"] == ("wazuh-indexer-certs-init", "wazuh-indexer")
    assert waves[0].name == "registry-mirror"
    assert [wave.name for wave in waves].index("wazuh-indexer") < [wave.name for wave in waves].index("wazuh-dashboard")


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("", "missing"),
        ('{"State":"running","Health":"healthy"}', "ok"),
        ('[{"State":"running","Health":""}]', "ok"),
        ('{"Status":"Up 2 minutes"}', "ok"),
        ('{"State":"running","Health":"starting"}', "starting"),
        ('{"State":"exited","Health":""}', "exited"),
        ("not-json", "error"),
    ],
)
def test_service_health_from_ps_json(raw: str, expected: str) -> None:
    assert _service_health_from_ps_json(raw) == expected


def test_service_health_accepts_successful_one_shot_only_when_declared() -> None:
    completed = '{"State":"exited","ExitCode":0,"Health":""}'

    assert _service_health_from_ps_json(completed) == "exited"
    assert _service_health_from_ps_json(completed, allow_completed=True) == "ok"


def test_wave_records_health_timeout_as_deployment_failure(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "infra")
    runner = StaggeredComposeRunner(root=root, node="infra")
    runner.up_wave = MagicMock(return_value=True)  # type: ignore[method-assign]
    runner.wait_for_wave = MagicMock(return_value=False)  # type: ignore[method-assign]
    runner.wave_sleep = MagicMock()  # type: ignore[method-assign]

    assert runner.wave("api", "api") is False
    assert runner.wave_failures == 1
    runner.wave_sleep.assert_not_called()


def test_wave_health_batches_compose_ps_and_fails_on_missing_service(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "infra")
    runner = StaggeredComposeRunner(root=root, node="infra")
    rows = "\n".join(
        [
            '{"Service":"postgres","State":"running","Health":"healthy"}',
            '{"Service":"redis","State":"running","Health":"healthy"}',
        ]
    )
    runner._run_compose = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(returncode=0, stdout=rows, stderr="")
    )

    assert runner._services_healthy(("postgres", "redis")) is True
    assert runner._run_compose.call_args.args == (
        "ps",
        "--all",
        "--format",
        "json",
        "postgres",
        "redis",
    )
    assert runner._services_healthy(("postgres", "redis", "missing")) is False
    completed = frozenset({"wazuh-indexer-certs-init"})
    assert _compose_ps_args("wazuh-indexer-certs-init", completed) == (
        "ps",
        "--all",
        "--format",
        "json",
        "wazuh-indexer-certs-init",
    )
    assert "--all" not in _compose_ps_args("postgres", completed)
    assert "wazuh-indexer-certs-init" in runner._completed_services


def test_local_ip_bindable_loopback():
    from toolkit.core.deploy.staggered_compose import _local_ip_bindable

    assert _local_ip_bindable("127.0.0.1")
    assert not _local_ip_bindable("192.0.2.1")  # TEST-NET-1, not local


def _setup_guest_root(tmp_path: Path, node: str) -> Path:
    gen = tmp_path / "generated" / node
    gen.mkdir(parents=True)
    (gen / ".env").write_text("COMPOSE_PROFILES=\nPRIVATE_IP=127.0.0.1\n")
    (gen / "compose.yaml").write_text("services: {}\n")
    (tmp_path / "config.yaml").write_text("domain: test.local\nproxmox:\n  provision_machines: true\n")
    return tmp_path


def test_remote_database_waits_follow_service_binding_environment(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "apps")
    (root / "generated/apps/.env").write_text(
        "COMPOSE_PROFILES=\nPRIVATE_IP=127.0.0.1\nGITEA_DB_HOST=10.10.10.10\nGITEA_DB_PORT=5544\n"
    )
    runner = StaggeredComposeRunner(root=root, node="apps")
    runner.wait_for_remote_tcp = MagicMock(return_value=True)  # type: ignore[method-assign]

    runner._wait_for_remote_dependencies()

    runner.wait_for_remote_tcp.assert_called_once_with(
        "10.10.10.10",
        5544,
        "PostgreSQL dependency for Gitea",
    )


def test_remote_service_waits_follow_dependency_endpoint_environment(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "apps")
    (root / "generated/apps/.env").write_text(
        "COMPOSE_PROFILES=\nPRIVATE_IP=127.0.0.1\nNEXTCLOUD_REDIS_HOST=10.10.10.10\nNEXTCLOUD_REDIS_PORT=6380\n"
    )
    runner = StaggeredComposeRunner(root=root, node="apps")
    runner.wait_for_remote_tcp = MagicMock(return_value=True)  # type: ignore[method-assign]

    runner._wait_for_remote_dependencies()

    runner.wait_for_remote_tcp.assert_called_once_with(
        "10.10.10.10",
        6380,
        "Redis dependency for Nextcloud",
    )


def test_remote_runtime_waits_follow_dependency_endpoint_environment(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "media")
    (root / "generated/media/.env").write_text(
        "COMPOSE_PROFILES=\nPRIVATE_IP=127.0.0.1\nALLOY_LOKI_HOST=10.10.10.10\nALLOY_LOKI_PORT=3100\n"
    )
    runner = StaggeredComposeRunner(root=root, node="media")
    runner.wait_for_remote_tcp = MagicMock(return_value=True)  # type: ignore[method-assign]

    runner._wait_for_remote_dependencies()

    runner.wait_for_remote_tcp.assert_called_once_with(
        "10.10.10.10",
        3100,
        "Loki dependency for Alloy",
    )


def test_run_staggered_compose_blocks_workstation_multi_vm(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path
    (root / "docker-compose.yml").write_text("services: {}\n")
    (root / "config.yaml").write_text(
        "domain: test.local\nemail: a@test.local\nproxmox:\n  provision_machines: true\n"
        "services:\n  management: true\n  media: true\n"
    )
    monkeypatch.delenv("HOMELAB_NODE", raising=False)
    monkeypatch.delenv("HOMELAB_DEPLOY_CONTROLLER", raising=False)
    rc = run_staggered_compose(root, "media")
    assert rc == 2


def test_run_staggered_compose_missing_env(tmp_path: Path) -> None:
    root = tmp_path
    (root / "docker-compose.yml").write_text("services: {}\n")
    (root / "config.yaml").write_text(
        "domain: test.local\nemail: a@test.local\nservices:\n"
        "  management: true\n  media: false\n  cloud: false\n"
        "  notifications: false\n  email: false\n  security: false\n"
        "proxmox:\n  provision_machines: false\n"
    )
    rc = run_staggered_compose(root, "infra")
    assert rc == 1


def test_run_staggered_compose_writes_status(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "infra")
    ok_proc = MagicMock(returncode=0, stdout='{"State":"running","Health":"healthy"}', stderr="")

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "network", "ls"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if cmd[:3] == ["docker", "compose", "up"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        if "pull" in cmd:
            return MagicMock(returncode=0, stdout="", stderr="")
        if cmd[:2] == ["curl"]:
            return MagicMock(returncode=0, stdout="", stderr="")
        return ok_proc

    runner = StaggeredComposeRunner(root=root, node="infra", subprocess_run=fake_run)
    runner.sleep = lambda _: None  # type: ignore[method-assign]

    with patch.object(runner, "_run_node", return_value=None):
        with patch.object(runner, "load_gate_strict"):
            rc = runner.run()

    assert rc == 0
    assert (root / ".compose-up.infra.status").read_text().strip() == "ok"


def test_up_wave_records_compose_up_services(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "media")
    recorded: list[tuple[str, ...]] = []

    def fake_run(cmd, **kwargs):
        if "pull" in cmd:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout='{"State":"exited","Health":""}', stderr="")

    def fake_streamed(*args: str):
        if args and args[0] == "up":
            recorded.append(tuple(args[2:]))  # after up -d
        return MagicMock(returncode=0, stdout="", stderr="")

    runner = StaggeredComposeRunner(root=root, node="media", subprocess_run=fake_run)
    runner._run_compose_streamed = fake_streamed  # type: ignore[method-assign]
    runner.sleep = lambda _: None  # type: ignore[method-assign]
    runner._profiles = frozenset()
    runner._capacity = None
    runner._compose_cmd = runner._build_compose_cmd()

    with patch.object(runner, "wait_for_wave", return_value=True):
        runner.up_wave("prowlarr", "flaresolverr")
        runner.up_wave("qbittorrent")

    assert ("--remove-orphans", "--no-deps", "prowlarr", "flaresolverr") in recorded
    assert ("--remove-orphans", "--no-deps", "qbittorrent") in recorded


def test_up_wave_reconciles_when_running_without_healthcheck(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "media")
    calls: list[tuple[str, ...]] = []

    def fake_run(cmd, **kwargs):
        if "pull" in cmd:
            return MagicMock(returncode=0, stdout="", stderr="")
        return MagicMock(returncode=0, stdout='{"State":"running","Health":""}', stderr="")

    def fake_streamed(*args: str):
        calls.append(args)
        return MagicMock(returncode=0, stdout="", stderr="")

    runner = StaggeredComposeRunner(root=root, node="media", subprocess_run=fake_run)
    runner._run_compose_streamed = fake_streamed  # type: ignore[method-assign]
    runner.sleep = lambda _: None  # type: ignore[method-assign]
    runner._profiles = frozenset()
    runner._compose_cmd = runner._build_compose_cmd()

    assert runner.up_wave("node-exporter-agent", "cadvisor-agent") is True
    assert calls == [
        (
            "up",
            "-d",
            "--remove-orphans",
            "--no-deps",
            "node-exporter-agent",
            "cadvisor-agent",
        )
    ]


def test_up_wave_does_not_force_recreate_healthy_edge(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "infra")
    recorded: list[tuple[str, ...]] = []

    runner = StaggeredComposeRunner(root=root, node="infra")
    runner._profiles = frozenset()
    runner._compose_cmd = runner._build_compose_cmd()
    runner.compose_pull_retry = lambda services=(): True  # type: ignore[method-assign]
    runner._run_compose_streamed = lambda *args: (  # type: ignore[method-assign]
        recorded.append(args) or MagicMock(returncode=0, stdout="", stderr="")
    )

    assert runner.up_wave("authelia", "caddy") is True
    assert "--force-recreate" not in recorded[0]


def test_up_wave_force_recreates_an_unhealthy_unchanged_service(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "media")
    recorded: list[tuple[str, ...]] = []

    runner = StaggeredComposeRunner(root=root, node="media")
    runner._profiles = frozenset()
    runner._compose_cmd = runner._build_compose_cmd()
    runner.compose_pull_retry = lambda services=(): True  # type: ignore[method-assign]
    runner._run_compose_streamed = lambda *args: (  # type: ignore[method-assign]
        recorded.append(args) or MagicMock(returncode=0, stdout="", stderr="")
    )

    assert runner.up_wave("recyclarr", force_recreate=True) is True
    assert recorded == [
        (
            "up",
            "-d",
            "--remove-orphans",
            "--no-deps",
            "--force-recreate",
            "recyclarr",
        )
    ]


def test_up_wave_honors_plugin_no_recreate_over_generic_force(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "media")
    recorded: list[tuple[str, ...]] = []

    runner = StaggeredComposeRunner(root=root, node="media")
    runner._profiles = frozenset()
    runner._compose_cmd = runner._build_compose_cmd()
    runner.compose_pull_retry = lambda services=(): True  # type: ignore[method-assign]
    runner._run_compose_streamed = lambda *args: (  # type: ignore[method-assign]
        recorded.append(args) or MagicMock(returncode=0, stdout="", stderr="")
    )
    runner.add_compose_up_option("--no-recreate")

    assert runner.up_wave("qbittorrent-vpn", force_recreate=True) is True
    assert "--no-recreate" in recorded[0]
    assert "--force-recreate" not in recorded[0]


def test_recovery_planner_requires_healthy_no_change_wave(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "infra")
    runner = StaggeredComposeRunner(root=root, node="infra")
    runner._profiles = frozenset()
    runner._compose_cmd = runner._build_compose_cmd()
    runner._recovery_mode = True
    runner._services_healthy = lambda _services: True  # type: ignore[method-assign]
    runner._run_compose = lambda *_args, **_kwargs: MagicMock(  # type: ignore[method-assign]
        returncode=0,
        stdout="Container postgres Running\nContainer redis Running\n",
        stderr="",
    )
    assert runner._can_skip_recovery_wave(("postgres", "redis")) is True

    runner._run_compose = lambda *_args, **_kwargs: MagicMock(  # type: ignore[method-assign]
        returncode=0,
        stdout="Container postgres Recreate\n",
        stderr="",
    )
    assert runner._can_skip_recovery_wave(("postgres",)) is False


def test_recovery_planner_excludes_successful_one_shot_services(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "infra")
    runner = StaggeredComposeRunner(root=root, node="infra")
    runner._run_compose = MagicMock(  # type: ignore[method-assign]
        return_value=MagicMock(returncode=0, stdout="Container wazuh-indexer Running\n", stderr="")
    )

    assert runner._compose_up_would_change(("wazuh-indexer-certs-init", "wazuh-indexer")) is False
    args = runner._run_compose.call_args.args
    assert "wazuh-indexer" in args
    assert "wazuh-indexer-certs-init" not in args


def test_recovery_imports_hyphenated_service_modules_dynamically(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "infra")
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return MagicMock(returncode=0, stdout="", stderr="")

    runner = StaggeredComposeRunner(root=root, node="infra", subprocess_run=fake_run)
    runner._run_heal_lines("ensure_wazuh_indexer_healthy", "toolkit.services.wazuh-indexer.bootstrap")

    script = calls[0][2]
    assert "importlib.import_module('toolkit.services.wazuh-indexer.bootstrap')" in script
    assert "from toolkit.services.wazuh-indexer.bootstrap import" not in script


def test_before_wave_fails_when_runtime_host_paths_are_missing(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "media")
    runner = StaggeredComposeRunner(root=root, node="media")
    required = tmp_path / "missing-device"
    runner._runtime_host_paths = {"custom-accelerator": (str(required),)}

    with pytest.raises(RuntimeError, match=r"custom-accelerator.*missing-device"):
        runner._before_wave("custom", ("base", "custom-accelerator"))


def test_wave_lifecycle_is_dispatched_to_owning_service_plugin(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "media")
    runner = StaggeredComposeRunner(root=root, node="media")
    plugin = MagicMock()
    plugin.before_runtime_start.return_value = ("adjusted",)

    with patch("toolkit.services.get_service_plugin", return_value=plugin):
        assert runner._before_wave("example", ("example", "helper")) == ("adjusted",)
        runner._after_wave("example", ("adjusted",))

    plugin.before_runtime_start.assert_called_once_with(runner, ("example", "helper"))
    plugin.after_runtime_start.assert_called_once_with(runner, ("adjusted",))


def test_controller_reconciliation_does_not_restart_its_own_runtime(tmp_path: Path, monkeypatch) -> None:
    root = _setup_guest_root(tmp_path, "infra")
    runner = StaggeredComposeRunner(root=root, node="infra")
    monkeypatch.setenv("HOMELAB_PRESERVE_CONTROLLER", "1")

    with patch("toolkit.services.get_service_plugin", return_value=None):
        services = runner._before_wave("homelab-ui", ("homelab-controller", "homelab-ui"))

    assert services == ("homelab-ui",)


def test_pre_deployment_lifecycle_is_dispatched_to_active_service_plugin(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "media")
    (root / "generated/media/compose.yaml").write_text("services:\n  gluetun: {}\n", encoding="utf-8")
    runner = StaggeredComposeRunner(root=root, node="media")
    plugin = MagicMock(has_compose_application=True)
    plugin.compose_application.return_value = {"services": {"gluetun": {}}}

    with patch(
        "toolkit.services.get_service_plugin",
        side_effect=lambda name: plugin if name == "gluetun" else None,
    ):
        runner._prepare_runtime_deployment()

    plugin.prepare_runtime_deployment.assert_called_once_with(runner, ("gluetun",))


def test_changed_network_subnet_drains_only_managed_attached_containers(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "infra")
    (root / "generated/infra/compose.yaml").write_text(
        "name: homelab\n"
        "services:\n  api:\n    image: example/api:1\n    networks: [plugin-api]\n"
        "networks:\n"
        "  plugin-api:\n"
        "    driver: bridge\n"
        "    ipam:\n      config:\n        - subnet: 172.31.10.0/28\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["docker", "network", "inspect"]:
            return MagicMock(
                returncode=0,
                stdout=(
                    '[{"IPAM":{"Config":[{"Subnet":"172.20.0.0/16"}]},"Containers":{"container-id":{"Name":"api"}}}]'
                ),
                stderr="",
            )
        if cmd[:3] == ["docker", "container", "inspect"]:
            return MagicMock(
                returncode=0,
                stdout='[{"Config":{"Labels":{"com.docker.compose.project":"homelab"}}}]',
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    runner = StaggeredComposeRunner(root=root, node="infra", subprocess_run=fake_run)
    runner._compose_file = root / "generated/infra/compose.yaml"

    runner._reconcile_changed_networks()

    assert ["docker", "rm", "--force", "container-id"] in calls
    assert ["docker", "network", "rm", "homelab_plugin-api"] in calls


def test_changed_network_subnet_refuses_to_remove_unmanaged_containers(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "infra")
    (root / "generated/infra/compose.yaml").write_text(
        "name: homelab\n"
        "services:\n  api:\n    image: example/api:1\n    networks: [plugin-api]\n"
        "networks:\n"
        "  plugin-api:\n"
        "    ipam:\n      config:\n        - subnet: 172.31.10.0/28\n",
        encoding="utf-8",
    )

    def fake_run(cmd, **kwargs):
        if cmd[:3] == ["docker", "network", "inspect"]:
            return MagicMock(
                returncode=0,
                stdout=(
                    '[{"IPAM":{"Config":[{"Subnet":"172.20.0.0/16"}]},'
                    '"Containers":{"external-id":{"Name":"external"}}}]'
                ),
                stderr="",
            )
        if cmd[:3] == ["docker", "container", "inspect"]:
            return MagicMock(
                returncode=0,
                stdout='[{"Config":{"Labels":{"com.docker.compose.project":"another-project"}}}]',
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    runner = StaggeredComposeRunner(root=root, node="infra", subprocess_run=fake_run)
    runner._compose_file = root / "generated/infra/compose.yaml"

    with pytest.raises(RuntimeError, match="unmanaged container"):
        runner._reconcile_changed_networks()


def test_changed_network_subnet_drains_declared_service_with_missing_endpoint(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "infra")
    (root / "generated/infra/compose.yaml").write_text(
        "name: homelab\n"
        "services:\n  database:\n    image: example/database:1\n    networks: [shared]\n"
        "networks:\n"
        "  shared:\n"
        "    ipam:\n      config:\n        - subnet: 172.31.10.0/28\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["docker", "network", "inspect"]:
            return MagicMock(
                returncode=0,
                stdout='[{"IPAM":{"Config":[{"Subnet":"172.20.0.0/16"}]},"Containers":{}}]',
                stderr="",
            )
        if cmd[:3] == ["docker", "ps", "--all"]:
            return MagicMock(returncode=0, stdout="database-id\n", stderr="")
        if cmd[:3] == ["docker", "container", "inspect"]:
            return MagicMock(
                returncode=0,
                stdout='[{"Config":{"Labels":{"com.docker.compose.project":"homelab"}}}]',
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    runner = StaggeredComposeRunner(root=root, node="infra", subprocess_run=fake_run)
    runner._compose_file = root / "generated/infra/compose.yaml"

    runner._reconcile_changed_networks()

    assert ["docker", "rm", "--force", "database-id"] in calls


def test_network_reconciliation_removes_obsolete_managed_networks(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "infra")
    (root / "generated/infra/compose.yaml").write_text(
        "name: homelab\nservices: {}\nnetworks:\n  current:\n    driver: bridge\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        if cmd[:3] == ["docker", "network", "ls"]:
            return MagicMock(returncode=0, stdout="homelab_current\nhomelab_obsolete\n", stderr="")
        if cmd[:3] == ["docker", "network", "inspect"]:
            return MagicMock(
                returncode=0,
                stdout='[{"IPAM":{"Config":[]},"Containers":{"old-id":{"Name":"old"}}}]',
                stderr="",
            )
        if cmd[:3] == ["docker", "container", "inspect"]:
            return MagicMock(
                returncode=0,
                stdout='[{"Config":{"Labels":{"com.docker.compose.project":"homelab"}}}]',
                stderr="",
            )
        return MagicMock(returncode=0, stdout="", stderr="")

    runner = StaggeredComposeRunner(root=root, node="infra", subprocess_run=fake_run)
    runner._compose_file = root / "generated/infra/compose.yaml"

    runner._reconcile_changed_networks()

    assert ["docker", "rm", "--force", "old-id"] in calls
    assert ["docker", "network", "rm", "homelab_obsolete"] in calls
    assert ["docker", "network", "rm", "homelab_current"] not in calls


def test_network_reconciliation_retries_transient_endpoint_cleanup(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "infra")
    (root / "generated/infra/compose.yaml").write_text(
        "name: homelab\nservices: {}\nnetworks:\n  current:\n    driver: bridge\n",
        encoding="utf-8",
    )
    calls: list[list[str]] = []
    sleeps: list[float] = []
    remove_attempts = 0

    def fake_run(cmd, **kwargs):
        nonlocal remove_attempts
        calls.append(cmd)
        if cmd[:3] == ["docker", "network", "ls"]:
            return MagicMock(returncode=0, stdout="homelab_obsolete\n", stderr="")
        if cmd[:3] == ["docker", "network", "inspect"]:
            return MagicMock(
                returncode=0,
                stdout='[{"IPAM":{"Config":[]},"Containers":{}}]',
                stderr="",
            )
        if cmd[:3] == ["docker", "network", "rm"]:
            remove_attempts += 1
            if remove_attempts == 1:
                return MagicMock(returncode=1, stdout="", stderr="network has active endpoints")
        return MagicMock(returncode=0, stdout="", stderr="")

    runner = StaggeredComposeRunner(
        root=root,
        node="infra",
        subprocess_run=fake_run,
        sleep=sleeps.append,
    )
    runner._compose_file = root / "generated/infra/compose.yaml"

    runner._reconcile_changed_networks()

    assert calls.count(["docker", "network", "rm", "homelab_obsolete"]) == 2
    assert sleeps == [1.0]


def test_recovery_skips_only_unchanged_non_buildable_wave(tmp_path: Path) -> None:
    from toolkit.core.registry.stagger_planner import StaggerWave

    root = _setup_guest_root(tmp_path, "infra")
    runner = StaggeredComposeRunner(root=root, node="infra")
    runner._profiles = frozenset()
    runner._recovery_mode = True
    runner._services_healthy = lambda _services: True  # type: ignore[method-assign]
    runner._compose_up_would_change = lambda _services: False  # type: ignore[method-assign]
    runner.up_wave = MagicMock(return_value=True)  # type: ignore[method-assign]
    runner.wait_for_wave = MagicMock(return_value=True)  # type: ignore[method-assign]
    runner.wave_sleep = MagicMock()  # type: ignore[method-assign]

    with (
        patch(
            "toolkit.core.registry.stagger_planner.compose_stagger_waves",
            return_value=[StaggerWave("databases", ("postgres", "redis"))],
        ),
        patch("toolkit.core.deploy.staggered_compose.unplanned_active_services", return_value=set()),
    ):
        runner._run_role_waves()

    runner.up_wave.assert_not_called()
    runner.wait_for_wave.assert_not_called()
    runner.wave_sleep.assert_not_called()

    assert runner._can_skip_recovery_wave(("postgres",)) is True


def test_role_wave_records_non_running_health_failure(tmp_path: Path) -> None:
    from toolkit.core.registry.stagger_planner import StaggerWave

    root = _setup_guest_root(tmp_path, "infra")
    runner = StaggeredComposeRunner(root=root, node="infra")
    runner._profiles = frozenset()
    runner.up_wave = MagicMock(return_value=True)  # type: ignore[method-assign]
    runner.wait_for_wave = MagicMock(return_value=False)  # type: ignore[method-assign]
    runner.wave_sleep = MagicMock()  # type: ignore[method-assign]

    with (
        patch(
            "toolkit.core.registry.stagger_planner.compose_stagger_waves",
            return_value=[StaggerWave("databases", ("postgres",))],
        ),
        patch("toolkit.core.deploy.staggered_compose.unplanned_active_services", return_value=set()),
    ):
        runner._run_role_waves()

    assert runner.wave_failures == 1


def test_role_wave_requests_recreate_when_current_container_is_unhealthy(tmp_path: Path) -> None:
    from toolkit.core.registry.stagger_planner import StaggerWave

    root = _setup_guest_root(tmp_path, "media")
    runner = StaggeredComposeRunner(root=root, node="media")
    runner._profiles = frozenset()
    runner._services_healthy = lambda _services: False  # type: ignore[method-assign]
    runner.up_wave = MagicMock(return_value=True)  # type: ignore[method-assign]
    runner.wait_for_wave = MagicMock(return_value=True)  # type: ignore[method-assign]
    runner.wave_sleep = MagicMock()  # type: ignore[method-assign]

    with (
        patch(
            "toolkit.core.registry.stagger_planner.compose_stagger_waves",
            return_value=[StaggerWave("recyclarr", ("recyclarr",))],
        ),
        patch("toolkit.core.deploy.staggered_compose.unplanned_active_services", return_value=set()),
    ):
        runner._run_role_waves()

    runner.up_wave.assert_called_once_with("recyclarr", force_recreate=True)


def test_role_wave_skips_health_wait_when_compose_up_fails(tmp_path: Path) -> None:
    from toolkit.core.registry.stagger_planner import StaggerWave

    root = _setup_guest_root(tmp_path, "media")
    runner = StaggeredComposeRunner(root=root, node="media")
    runner._profiles = frozenset()
    runner._services_healthy = lambda _services: False  # type: ignore[method-assign]
    runner.up_wave = MagicMock(return_value=False)  # type: ignore[method-assign]
    runner.wait_for_wave = MagicMock(return_value=True)  # type: ignore[method-assign]
    runner.wave_sleep = MagicMock()  # type: ignore[method-assign]

    with (
        patch(
            "toolkit.core.registry.stagger_planner.compose_stagger_waves",
            return_value=[StaggerWave("qbittorrent", ("qbittorrent-vpn",))],
        ),
        patch("toolkit.core.deploy.staggered_compose.unplanned_active_services", return_value=set()),
    ):
        runner._run_role_waves()

    assert runner.wave_failures == 1
    runner.wait_for_wave.assert_not_called()


def test_role_wave_fails_closed_when_health_never_settles(tmp_path: Path) -> None:
    from toolkit.core.registry.stagger_planner import StaggerWave

    root = _setup_guest_root(tmp_path, "infra")
    runner = StaggeredComposeRunner(root=root, node="infra")
    runner._profiles = frozenset()
    runner.up_wave = MagicMock(return_value=True)  # type: ignore[method-assign]
    runner.wait_for_wave = MagicMock(return_value=False)  # type: ignore[method-assign]
    runner.wave_sleep = MagicMock()  # type: ignore[method-assign]

    with (
        patch(
            "toolkit.core.registry.stagger_planner.compose_stagger_waves",
            return_value=[StaggerWave("databases", ("postgres",))],
        ),
        patch("toolkit.core.deploy.staggered_compose.unplanned_active_services", return_value=set()),
    ):
        runner._run_role_waves()

    assert runner.wave_failures == 1


def test_tdarr_runs_on_cpu_only_hosts(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "media")
    runner = StaggeredComposeRunner(root=root, node="media")

    assert runner._before_wave("tdarr", ("tdarr",)) == ("tdarr",)


def test_run_reports_failure_on_wave_failure(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "infra")

    runner = StaggeredComposeRunner(root=root, node="infra")
    runner.sleep = lambda _: None  # type: ignore[method-assign]

    with patch.object(runner, "_acquire_lock", return_value=True):
        with patch.object(runner, "load_gate_strict"):
            with patch.object(runner, "_run_node", side_effect=lambda: runner._record_failure()):
                rc = runner.run()

    assert rc == 1
    assert (root / ".compose-up.infra.status").read_text().strip() == "failed"


def test_node_generates_recyclarr_artifact_when_plugin_is_active(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "worker")
    (root / "generated/worker/compose.yaml").write_text("services:\n  recyclarr:\n    image: example/recyclarr:1\n")
    runner = StaggeredComposeRunner(root=root, node="worker")
    runner._profiles = frozenset()

    runner._ensure_compose_artifacts()

    assert (root / "generated/recyclarr/recyclarr.yml").is_file()


def test_node_does_not_generate_artifact_for_absent_plugin(tmp_path: Path) -> None:
    root = _setup_guest_root(tmp_path, "worker")
    runner = StaggeredComposeRunner(root=root, node="worker")
    runner._profiles = frozenset()

    runner._ensure_compose_artifacts()

    assert not (root / "generated/recyclarr").exists()


# ── StaggeredComposeRunner.run — state-file integrity on non-local exits ────


class _BaseExceptionSignal(BaseException):
    """Test double for SIGTERM/SIGKILL delivered to the runner (not Exception)."""


def test_run_writes_failed_status_on_baseexception(tmp_path: Path) -> None:
    """A SIGTERM/SIGKILL (BaseException, not Exception) must leave status 'failed'.

    Regression: the old `except Exception` skipped `_write_status('failed')` on
    a SIGTERM'd runner, leaving the `.compose-up.<node>.status` marker stuck on
    'running'. That marker is what the controller's compose_wait gate treats as
    'already up' next time, silently no-op'ing the next deploy.
    """
    root = _setup_guest_root(tmp_path, "infra")
    runner = StaggeredComposeRunner(root=root, node="infra")
    runner.sleep = lambda _: None  # type: ignore[method-assign]

    with patch.object(runner, "_acquire_lock", return_value=True):
        with patch.object(runner, "load_gate_strict"):
            with patch.object(runner, "_run_node", side_effect=_BaseExceptionSignal("SIGTERM")):
                with pytest.raises(_BaseExceptionSignal):
                    runner.run()

    assert (root / ".compose-up.infra.status").read_text().strip() == "failed"


def test_run_does_not_clobber_status_when_lock_not_acquired(tmp_path: Path) -> None:
    """A contender that loses the lock must NOT rewrite the legitimate owner's status.

    Regression: `_acquire_lock` called `_write_status('failed')` on a transient
    BlockingIOError, clobbering a 'running'/'ok' held by the other runner.
    """
    root = _setup_guest_root(tmp_path, "infra")
    status = root / ".compose-up.infra.status"
    status.write_text("running\n")  # existing legitimate owner mid-run

    runner = StaggeredComposeRunner(root=root, node="infra")

    with patch.object(runner, "_acquire_lock", return_value=False):
        rc = runner.run()

    assert rc == 1
    # status file must be UNCHANGED — the loser of the lock race doesn't own it
    assert status.read_text().strip() == "running"


def test_acquire_lock_does_not_clobber_status_on_contention(tmp_path: Path) -> None:
    """A real lock-contention loss (BlockingIOError on fcntl) must not write status.

    Drives the actual _acquire_lock path (not the mock) so we cover the line
    that historically called _write_status('failed') and clobbered the owner.
    """
    import fcntl

    root = _setup_guest_root(tmp_path, "infra")
    status = root / ".compose-up.infra.status"
    status.write_text("running\n")  # existing legitimate owner mid-run

    # Pre-acquire the lock with a held fd so our runner hits BlockingIOError.
    holder_fd = os.open(root / ".compose-deploy.infra.lock", os.O_CREAT | os.O_RDWR, 0o644)
    fcntl.flock(holder_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        runner = StaggeredComposeRunner(root=root, node="infra")
        acquired = runner._acquire_lock()
        assert acquired is False
        # The contender must NOT rewrite the legitimate owner's status.
        assert status.read_text().strip() == "running"
    finally:
        fcntl.flock(holder_fd, fcntl.LOCK_UN)
        os.close(holder_fd)
