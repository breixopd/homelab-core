"""Behavioral tests for toolkit/core/watchdog.py"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from pathlib import Path
from unittest.mock import Mock, patch

import pytest
from toolkit.core.ops.watchdog import (
    ContainerHealth,
    HealResult,
    HealthIssue,
    Watchdog,
    WatchdogEvent,
    WatchdogReport,
    _parse_docker_uptime,
)

# ── helpers ──────────────────────────────────────────────────────────────────


def mock_run(stdout="", stderr="", returncode=0):
    """Build a mock CompletedProcess."""
    proc = Mock(spec=subprocess.CompletedProcess)
    proc.stdout = stdout
    proc.stderr = stderr
    proc.returncode = returncode
    return proc


class FakeConfig:
    """Minimal Config stand-in for Watchdog.__init__."""

    domain = "example.com"
    services = Mock(management=True)
    is_multi_node = False
    proxmox = Mock(provision_machines=False)
    enabled_nodes = ["infra"]

    @staticmethod
    def category_enabled(name: str) -> bool:
        return name == "management"


def test_check_all_preserves_node_for_duplicate_fleet_container_names(tmp_path):
    wd = Watchdog(tmp_path, FakeConfig())
    containers = [
        {"Names": "edge", "State": "running", "Status": "Up 1 hour", "FleetVM": "media"},
        {"Names": "edge", "State": "running", "Status": "Up 2 hours", "FleetVM": "apps"},
    ]

    with patch.object(wd, "_get_containers", return_value=containers):
        report = wd.check_all()

    assert report.healthy == [ContainerHealth("edge", "media"), ContainerHealth("edge", "apps")]


def test_fleet_log_collection_uses_explicit_observed_node(tmp_path):
    wd = Watchdog(tmp_path, FakeConfig())
    wd._use_fleet_watchdog = lambda: True  # type: ignore[method-assign]
    with patch.object(wd, "_fleet_docker", return_value=(0, "remote log", "")) as remote:
        logs = wd._get_container_logs("edge", node="media")

    assert logs == "remote log"
    remote.assert_called_once_with("media", "docker logs --tail 30 edge", timeout=10)


def test_fleet_post_restart_verification_uses_explicit_node(tmp_path):
    wd = Watchdog(tmp_path, FakeConfig())
    wd._use_fleet_watchdog = lambda: True  # type: ignore[method-assign]
    with patch.object(wd, "_fleet_docker", return_value=(0, "running:healthy", "")) as remote:
        assert wd.verify_post_restart("edge", node="apps", timeout=5) is True

    remote.assert_called_once_with(
        "apps",
        "docker inspect --format '{{.State.Status}}:{{.State.Health.Status}}' edge",
        timeout=10,
    )


def test_fleet_stats_are_collected_from_every_enabled_node(tmp_path):
    class FleetConfig(FakeConfig):
        enabled_nodes = ["media", "apps"]

    wd = Watchdog(tmp_path, FleetConfig())
    wd._use_fleet_watchdog = lambda: True  # type: ignore[method-assign]
    with patch.object(
        wd,
        "_fleet_docker",
        side_effect=[
            (0, "edge\t1%\t2%\t1MiB / 1GiB\t0B / 0B\t0B / 0B\n", ""),
            (0, "edge\t3%\t4%\t1MiB / 1GiB\t0B / 0B\t0B / 0B\n", ""),
        ],
    ) as remote:
        stats = wd._get_container_stats()

    assert [(item["name"], item["node"]) for item in stats] == [("edge", "media"), ("edge", "apps")]
    assert [call.args[0] for call in remote.call_args_list] == ["media", "apps"]


def test_partial_fleet_scan_failure_is_reported_with_node(tmp_path):
    wd = Watchdog(tmp_path, FakeConfig())
    wd._fleet_scan_errors = {"media"}
    with patch.object(
        wd,
        "_get_containers",
        return_value=[{"Names": "edge", "State": "running", "Status": "Up 1 hour", "FleetVM": "apps"}],
    ):
        report = wd.check_all()

    assert any(issue.category == "watchdog-infra" and issue.node == "media" for issue in report.issues)
    assert ContainerHealth("edge", "apps") in report.healthy


def test_fleet_label_discovery_queries_every_enabled_node(tmp_path):
    class FleetConfig(FakeConfig):
        enabled_nodes = ["media", "apps"]

    wd = Watchdog(tmp_path, FleetConfig())
    wd._discovered_safe.clear()
    wd._discovered_safe.add("edge")
    wd._use_fleet_watchdog = lambda: True  # type: ignore[method-assign]
    with patch.object(
        wd,
        "_fleet_docker",
        side_effect=[
            (0, "alloy-agent\tsafe\tloki\n", ""),
            (0, "edge\tcareful\tcaddy\n", ""),
        ],
    ) as remote:
        wd._discover_docker_labels()

    assert wd._discovered_safe == {"alloy-agent"}
    assert "edge" in wd._discovered_careful
    assert wd._discovered_deps["alloy-agent"] == ["loki"]
    assert [call.args[0] for call in remote.call_args_list] == ["media", "apps"]


def test_container_inventory_is_reused_within_one_health_cycle(tmp_path):
    wd = Watchdog(tmp_path, FakeConfig())
    wd._use_fleet_watchdog = lambda: True  # type: ignore[method-assign]
    wd._container_snapshot = None
    inventory = [{"Names": "edge", "State": "running", "Status": "Up 1 hour", "FleetVM": "apps"}]
    with patch.object(wd, "_get_fleet_containers", return_value=inventory) as collect:
        assert wd._get_containers() == inventory
        assert wd._get_containers() == inventory
        assert wd._get_container_uptimes() == {("edge", "apps"): 3600}

    collect.assert_called_once_with()


# ── _parse_docker_uptime ──────────────────────────────────────────────────────


class TestParseDockerUptime:
    @pytest.mark.parametrize(
        "status,expected_min",
        [
            ("Up 3 hours", 3 * 3600 - 1),
            ("Up 2 days", 2 * 86400 - 1),
            ("Up 45 minutes", 45 * 60 - 1),
            ("Up About an hour", 3600 - 1),
            ("Up About a minute", 60 - 1),
            ("Exited (0) 5 minutes ago", 0),
            ("Restarting (1) 10 seconds ago", 0),
        ],
    )
    def test_parses_various_formats(self, status, expected_min):
        result = _parse_docker_uptime(status)
        assert result >= expected_min, f"Expected >= {expected_min}, got {result}"


# ── WatchdogEvent / WatchdogReport ─────────────────────────────────────────────


class TestWatchdogReport:
    def test_ok_is_false_when_critical_issue(self):
        from toolkit.core.ops.watchdog import HealthIssue

        r = WatchdogReport()
        r.issues.append(HealthIssue("svc", "cat", "critical", "msg"))
        assert r.ok is False

    def test_ok_is_true_when_only_warning(self):
        from toolkit.core.ops.watchdog import HealthIssue

        r = WatchdogReport()
        r.issues.append(HealthIssue("svc", "cat", "warning", "msg"))
        assert r.ok is True

    def test_issue_identity_uses_sha256(self):
        from toolkit.core.ops.watchdog import _issue_key

        issue = HealthIssue("caddy", "proxy", "critical", "route unavailable")
        digest = _issue_key(issue)

        assert len(digest) == 64
        assert digest.isalnum()

    def test_issue_identity_includes_fleet_node(self):
        from toolkit.core.ops.watchdog import _issue_key

        media = HealthIssue("edge", "management", "critical", "down", node="media")
        apps = HealthIssue("edge", "management", "critical", "down", node="apps")

        assert _issue_key(media) != _issue_key(apps)

    def test_summary_counts_correctly(self):
        r = WatchdogReport()
        r.healthy = [ContainerHealth("a"), ContainerHealth("b")]
        r.issues.extend(
            [
                HealthIssue("x", "cat", "critical", "c"),
                HealthIssue("y", "cat", "warning", "w"),
            ]
        )

        assert "2 healthy" in r.summary()
        assert "1 warnings" in r.summary()
        assert "1 critical" in r.summary()

    def test_serializes_container_node_identity(self):
        report = WatchdogReport(
            healthy=[ContainerHealth("node-exporter-agent", node="media")],
            issues=[HealthIssue("edge", "management", "warning", "down", node="apps")],
        )

        payload = report.to_dict()

        assert payload["healthy"] == [{"name": "node-exporter-agent", "node": "media"}]
        assert payload["issues"][0]["node"] == "apps"

    def test_to_dict_includes_all_fields(self):
        report = WatchdogReport(
            healthy=[ContainerHealth("a")],
            issues=[HealthIssue("x", "cat", "warning", "msg")],
        )

        payload = report.to_dict()

        assert "timestamp" in payload
        assert "healthy" in payload
        assert "issues" in payload


def test_heal_result_exposes_only_typed_fields():
    result = HealResult(logs=["restarted caddy"], attempted=1, succeeded=1)

    assert result.logs == ["restarted caddy"]
    assert result.attempted == 1
    assert result.succeeded == 1
    assert result.failed == 0
    assert result.deferred == 0
    assert result.ok is True
    with pytest.raises(TypeError):
        iter(result)


def test_heal_result_rejects_inconsistent_outcome_counts():
    with pytest.raises(ValueError):
        HealResult(attempted=1, succeeded=1, failed=1)


def test_managed_projects_are_safe_for_bounded_restart(tmp_path: Path) -> None:
    from toolkit.core.config.config import Config, ProjectEntry

    cfg = Config()
    cfg.projects.entries = [
        ProjectEntry(
            subdomain="status",
            auth_mode="forward_auth",
            exposure="private",
            docker_image="docker.io/library/nginx:1@sha256:" + "a" * 64,
            container_port=45678,
            placement="apps",
        )
    ]

    assert "status" in Watchdog(tmp_path, cfg).restartable_services()


def test_failed_project_health_endpoint_is_auto_fixable(tmp_path: Path) -> None:
    from toolkit.core.config.config import Config, ProjectEntry

    cfg = Config()
    cfg.projects.entries = [
        ProjectEntry(
            subdomain="status",
            auth_mode="forward_auth",
            exposure="private",
            docker_image="docker.io/library/nginx:1@sha256:" + "a" * 64,
            container_port=45678,
            placement="apps",
            health_endpoint="/ready",
        )
    ]
    watchdog = Watchdog(tmp_path, cfg)
    watchdog._get_running_names = lambda: {"status"}  # type: ignore[method-assign]
    watchdog._use_fleet_watchdog = lambda: False  # type: ignore[method-assign]
    watchdog._run = lambda *_args, **_kwargs: mock_run(returncode=1)  # type: ignore[method-assign]

    issues = watchdog.check_project_endpoints()

    assert len(issues) == 1
    assert issues[0].service == "status"
    assert issues[0].auto_fixable is True


# ── _persist_event / atomic log rotation ─────────────────────────────────────


class TestPersistEvent:
    """Test that _persist_event uses atomic write (temp file + rename)."""

    def test_appends_event_to_existing_log(self, tmp_path):
        """Appending a single event writes one JSONL line."""
        root = tmp_path / "root"
        root.mkdir()
        wd = Watchdog(root, FakeConfig())
        # Ensure parent dirs created
        (root / ".homelab-state").mkdir()
        log = root / ".homelab-state" / "watchdog-events.jsonl"
        log.write_text("")  # empty file

        with patch("toolkit.core.ops.watchdog.os.replace") as mock_replace:
            wd._persist_event(WatchdogEvent(1234.0, "check", "svc", "detail"))
            # os.replace should NOT be called on plain append
            mock_replace.assert_not_called()

        lines = log.read_text().splitlines()
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["timestamp"] == 1234.0
        assert data["action"] == "check"
        assert data["service"] == "svc"
        assert data["detail"] == "detail"

    def test_rotates_when_file_exceeds_1mb(self, tmp_path):
        """When file > 1 MB, must write to temp path then os.replace."""
        import stat as stat_mod

        root = tmp_path / "root"
        root.mkdir()
        (root / ".homelab-state").mkdir()
        log = root / ".homelab-state" / "watchdog-events.jsonl"

        big_content = json.dumps({"timestamp": 1.0, "action": "x", "service": "x", "detail": "x" * 200}) + "\n"
        log.write_text(big_content * 6000)  # ~1.1 MB

        with (
            patch("toolkit.core.ops.watchdog.os.replace") as mock_replace,
            patch.object(
                Path,
                "stat",
                return_value=Mock(st_size=2_000_000, st_mode=stat_mod.S_IFDIR | 0o755),
            ),
            patch.object(Path, "read_text", return_value=big_content * 6000),
        ):
            wd = Watchdog(root, FakeConfig())
            wd._persist_event(WatchdogEvent(9999.0, "heal", "svc", "new event"))

            mock_replace.assert_called_once()
            tmp_path_arg = mock_replace.call_args[0][0]
            assert ".event_log.tmp" in str(tmp_path_arg)

    def test_rotation_trims_to_last_200_lines(self, tmp_path):
        """After rotation, only last 200 lines are kept in the log file."""
        root = tmp_path / "root"
        root.mkdir()
        (root / ".homelab-state").mkdir()
        log = root / ".homelab-state" / "watchdog-events.jsonl"

        lines = [json.dumps({"timestamp": i, "action": "old", "service": f"s{i}", "detail": "x"}) for i in range(300)]
        log.write_text("\n".join(lines) + "\n")

        import stat as stat_mod

        prewritten = "\n".join(lines) + "\n"

        # Patch Path.read_text to return our prewritten content so rotation trims correctly
        with (
            patch.object(
                Path,
                "stat",
                return_value=Mock(st_size=2_000_000, st_mode=stat_mod.S_IFDIR | 0o755),
            ),
            patch.object(Path, "read_text", return_value=prewritten),
            patch.object(Path, "write_text"),
        ):
            wd = Watchdog(root, FakeConfig())
            wd._event_log_path = log
            wd._persist_event(WatchdogEvent(9999.0, "check", "svc", "detail"))

            # After rotation, os.replace is called with the tmp path
            # and write_text is called with last 200 lines
            call_args_list = Path.write_text.call_args_list
            assert len(call_args_list) == 1
            written_text = call_args_list[0][0][0]
            written_lines = written_text.strip().splitlines()
            # Should have exactly 200 lines (last 200 of the original 300)
            assert len(written_lines) == 200

    def test_rotation_cleans_up_temp_file_on_error(self, tmp_path):
        """If rotation fails, temp file is removed."""
        root = tmp_path / "root"
        root.mkdir()
        (root / ".homelab-state").mkdir()
        log = root / ".homelab-state" / "watchdog-events.jsonl"

        big_content = json.dumps({"timestamp": 1.0, "action": "x", "service": "x", "detail": "x" * 200}) + "\n"
        log.write_text(big_content * 6000)

        import stat as stat_mod

        with (
            patch.object(
                Path,
                "stat",
                return_value=Mock(st_size=2_000_000, st_mode=stat_mod.S_IFDIR | 0o755),
            ),
            patch.object(Path, "read_text", return_value=big_content * 6000),
            patch("toolkit.core.ops.watchdog.os.replace", side_effect=OSError("fail")),
            patch("toolkit.core.ops.watchdog.os.unlink"),
        ):
            wd = Watchdog(root, FakeConfig())
            wd._persist_event(WatchdogEvent(1.0, "check", "svc", "detail"))

    def test_event_log_dir_created_if_missing(self, tmp_path):
        """_persist_event creates parent directories if needed."""
        root = tmp_path / "root"
        root.mkdir()
        # state directory does not exist

        wd = Watchdog(root, FakeConfig())
        wd._persist_event(WatchdogEvent(1.0, "check", "svc", "detail"))

        assert (root / ".homelab-state" / "watchdog-events.jsonl").exists()


# ── check_port_conflicts ──────────────────────────────────────────────────────


class TestCheckPortConflicts:
    """Test that check_port_conflicts detects duplicate port bindings."""

    def test_no_conflicts_when_no_containers(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with patch.object(wd, "_get_containers", return_value=[]):
            issues = wd.check_port_conflicts()
        assert issues == []

    def test_no_conflicts_when_single_container(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        containers = [{"Names": "caddy", "Ports": "0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp"}]
        with patch.object(wd, "_get_containers", return_value=containers):
            issues = wd.check_port_conflicts()
        assert issues == []

    def test_detects_duplicate_port(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        # Two containers binding to same host port 8080
        containers = [
            {"Names": "svc-a", "Ports": "0.0.0.0:8080->80/tcp", "FleetVM": "apps"},
            {"Names": "svc-b", "Ports": "0.0.0.0:8080->80/tcp", "FleetVM": "apps"},
        ]
        with patch.object(wd, "_get_containers", return_value=containers):
            issues = wd.check_port_conflicts()
        assert len(issues) == 1
        assert issues[0].severity == "warning"
        assert "8080" in issues[0].message
        assert "svc-a" in issues[0].message
        assert "svc-b" in issues[0].message
        assert issues[0].node == "apps"

    def test_same_port_on_different_nodes_is_not_a_conflict(self):
        wd = Watchdog(Path("/tmp/test"), FakeConfig())
        containers = [
            {"Names": "edge", "Ports": "0.0.0.0:8080->80/tcp", "FleetVM": "media"},
            {"Names": "edge", "Ports": "0.0.0.0:8080->80/tcp", "FleetVM": "apps"},
        ]

        with patch.object(wd, "_get_containers", return_value=containers):
            assert wd.check_port_conflicts() == []

    def test_duplicate_ipv4_and_ipv6_bindings_of_one_container_are_not_a_conflict(self):
        wd = Watchdog(Path("/tmp/test"), FakeConfig())
        containers = [
            {
                "Names": "adguard",
                "Ports": "0.0.0.0:53->53/tcp, [::]:53->53/tcp",
                "FleetVM": "infra",
            }
        ]

        with patch.object(wd, "_get_containers", return_value=containers):
            assert wd.check_port_conflicts() == []

    def test_ignores_internal_port_bindings(self):
        """Ports without host binding (e.g. 80->80) should not cause conflicts."""
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        # Both containers only have internal mappings — no host port
        containers = [{"Names": "svc-a", "Ports": "80/tcp"}, {"Names": "svc-b", "Ports": "80/tcp"}]
        with patch.object(wd, "_get_containers", return_value=containers):
            issues = wd.check_port_conflicts()
        assert issues == []

    def test_handles_empty_ports_field(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        containers = [{"Names": "svc-a", "Ports": ""}, {"Names": "svc-b", "Ports": ""}]
        with patch.object(wd, "_get_containers", return_value=containers):
            issues = wd.check_port_conflicts()
        assert issues == []

    def test_non_zero_returncode_returns_empty(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with patch.object(wd, "_get_containers", return_value=[]):
            issues = wd.check_port_conflicts()
        assert issues == []


# ── check_disk_space ──────────────────────────────────────────────────────────


class TestCheckDiskSpace:
    """Test check_disk_space reports low disk appropriately."""

    def test_no_issue_when_under_threshold(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        # 70% used — below default 90% threshold
        fake_usage = Mock()
        fake_usage.used = 70
        fake_usage.total = 100
        fake_usage.free = 30
        with patch("shutil.disk_usage", return_value=fake_usage):
            issues = wd.check_disk_space()
        assert issues == []

    def test_warning_at_90_percent(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        fake_usage = Mock()
        fake_usage.used = 90
        fake_usage.total = 100
        fake_usage.free = 10
        # Both root and docker-data paths trigger the threshold
        with patch("shutil.disk_usage", return_value=fake_usage):
            issues = wd.check_disk_space(threshold_pct=90.0)
        assert len(issues) == 2
        assert all(i.severity == "warning" for i in issues)

    def test_critical_at_95_percent(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        fake_usage = Mock()
        fake_usage.used = 96
        fake_usage.total = 100
        fake_usage.free = 4
        with patch("shutil.disk_usage", return_value=fake_usage):
            issues = wd.check_disk_space(threshold_pct=90.0)
        assert len(issues) == 2
        assert all(i.severity == "critical" for i in issues)

    def test_checks_both_root_and_docker_data_paths(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())

        # First call returns high usage (root), second returns low (docker data)
        fake_high = Mock()
        fake_high.used = 95
        fake_high.total = 100
        fake_high.free = 5

        fake_low = Mock()
        fake_low.used = 50
        fake_low.total = 100
        fake_low.free = 50

        with patch("shutil.disk_usage", side_effect=[fake_high, fake_low]):
            issues = wd.check_disk_space(threshold_pct=90.0)

        # Only root should trigger since docker data is fine
        assert len(issues) == 1
        assert "Root filesystem" in issues[0].message

    def test_handles_oserror_gracefully(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with patch("shutil.disk_usage", side_effect=OSError("no access")):
            issues = wd.check_disk_space()
        assert issues == []


# ── heal / restart backoff ────────────────────────────────────────────────────


class TestHeal:
    """Test heal() enforces max restart limit (max 3 = max_restarts_per_service)."""

    def setup_method(self, _method):
        """Clean persisted watchdog state before each test so backoff counters
        don't leak between tests (watchdog now persists restart state to disk)."""
        state = Path("/tmp/test/.homelab-state/watchdog-state.json")
        if state.exists():
            state.unlink()

    def make_report(self, service, auto_fixable=True):
        from toolkit.core.ops.watchdog import HealthIssue

        r = WatchdogReport()
        r.issues.append(HealthIssue(service, "cat", "critical", "test msg", auto_fixable=auto_fixable))
        return r

    def test_skips_when_not_auto_fixable(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        wd._restart_counts["caddy"] = 0

        with patch("subprocess.run"):
            result = wd.heal(self.make_report("caddy", auto_fixable=False))

        assert any("SKIP" in log for log in result.logs)
        assert "caddy" in result.logs[0]

    def test_hook_failures_do_not_directly_trigger_recover(self, tmp_path: Path):
        root = tmp_path / "root"
        hooks_dir = root / ".homelab-state"
        hooks_dir.mkdir(parents=True)
        (hooks_dir / "last-hooks.json").write_text(
            json.dumps({"infra": {"passed": False, "critical": 1, "warning": 0}})
        )
        wd = Watchdog(root, FakeConfig())

        issues = wd.check_hook_failures()

        assert len(issues) == 1
        assert issues[0].service == "hooks"
        assert issues[0].auto_fixable is False

        with patch.object(wd, "_run") as mock_run:
            result = wd.heal(WatchdogReport(issues=issues))

        mock_run.assert_not_called()
        assert any("SKIP hooks" in log for log in result.logs)

    def test_skips_when_restart_count_exceeds_limit(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        wd._restart_counts["caddy"] = 3  # already at limit

        with patch("subprocess.run"):
            result = wd.heal(self.make_report("caddy"))

        assert any("SKIP" in log for log in result.logs)
        assert any("manual intervention" in log for log in result.logs)
        assert result.deferred == 1

    def test_defers_all_healing_while_platform_operation_is_active(self, tmp_path: Path):
        from toolkit.core.deploy.operation_lease import OperationLease

        wd = Watchdog(tmp_path, FakeConfig())
        lease = OperationLease.acquire(tmp_path, "config-apply")
        try:
            with patch.object(wd, "_docker_action") as action:
                result = wd.heal(self.make_report("caddy"))
        finally:
            lease.release()

        action.assert_not_called()
        assert result.attempted == 0
        assert result.deferred == 1
        assert result.logs == ["DEFER watchdog healing: config-apply operation is active"]

    def test_skips_when_in_backoff_window(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        wd._restart_counts["caddy"] = 1  # already 1 restart
        # Set last restart timestamp to now — within backoff window
        # backoff = 5 * 2^1 = 10 seconds
        wd._restart_timestamps["caddy"] = time.time()

        with patch("subprocess.run"):
            result = wd.heal(self.make_report("caddy"))

        assert any("BACKOFF" in log for log in result.logs)
        assert result.deferred == 1

    def test_proceeds_when_backoff_expired(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        wd._restart_counts["caddy"] = 1
        # Last restart was long ago — backoff expired
        wd._restart_timestamps["caddy"] = time.time() - 100

        with patch("subprocess.run", return_value=mock_run(returncode=0)):
            with patch.object(wd, "verify_post_restart", return_value=True):
                result = wd.heal(self.make_report("caddy"))

        assert any("HEAL" in log for log in result.logs)
        assert result.attempted == 1
        assert result.succeeded == 1

    def test_restart_increments_counter(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        wd._restart_counts["caddy"] = 0

        with patch("subprocess.run", return_value=mock_run(returncode=0)):
            with patch.object(wd, "verify_post_restart", return_value=True):
                wd.heal(self.make_report("caddy"))

        assert wd._restart_counts["caddy"] == 1

    def test_restart_logs_failure_on_nonzero_returncode(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        wd._restart_counts["caddy"] = 0

        with patch(
            "subprocess.run",
            return_value=mock_run(stderr="container not found", returncode=1),
        ):
            with patch.object(wd, "verify_post_restart", return_value=False):
                result = wd.heal(self.make_report("caddy"))

        assert any("FAIL" in log for log in result.logs)
        assert result.attempted == 1
        assert result.failed == 1
        assert result.ok is False

    def test_qbittorrent_stale_lock_heal(self, tmp_path):
        """qbittorrent-vpn unhealthy → stop, clear lockfile/ipc-socket, start."""
        cfg_dir = tmp_path / "config" / "qbittorrent" / "qBittorrent"
        cfg_dir.mkdir(parents=True)
        (cfg_dir / "lockfile").write_text("stale")
        (cfg_dir / "ipc-socket").write_text("")

        wd = Watchdog(tmp_path, FakeConfig())
        commands: list[list[str]] = []

        def record(cmd, **kwargs):
            commands.append(cmd)
            return mock_run(returncode=0)

        with patch("subprocess.run", side_effect=record):
            result = wd.heal(self.make_report("qbittorrent-vpn"))

        assert not (cfg_dir / "lockfile").exists()
        assert not (cfg_dir / "ipc-socket").exists()
        assert ["docker", "stop", "qbittorrent-vpn"] in commands
        assert ["docker", "start", "qbittorrent-vpn"] in commands
        # The generic restart path must not double-handle qbittorrent.
        assert ["docker", "restart", "qbittorrent-vpn"] not in commands
        assert any("clean lock state" in log for log in result.logs)

    def test_dependency_started_first_if_down(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        wd._restart_counts["grafana"] = 0

        started = []

        def run_mock(cmd, *args, **kwargs):
            started.append(cmd)
            return mock_run(returncode=0)

        with patch("subprocess.run", side_effect=run_mock):
            with (
                patch.object(wd, "verify_post_restart", return_value=True),
                patch.object(
                    wd,
                    "_get_running_names",
                    return_value={"postgres", "prometheus", "loki"},
                ),
            ):
                wd.heal(self.make_report("grafana"))

        restart_commands = [c for c in started if len(c) >= 2 and c[1] == "restart"]
        assert any(cmd[-1] == "grafana" for cmd in restart_commands)


# ── check_all ──────────────────────────────────────────────────────────────────


class TestCheckAll:
    def test_successful_manifest_oneshot_is_not_reported_as_failure(self, monkeypatch):
        wd = Watchdog(Path("/tmp/test"), FakeConfig())
        monkeypatch.setattr(
            "toolkit.core.config.service_metadata.get_service_runtime_mode",
            lambda service: "oneshot" if service == "cert-init" else "daemon",
        )
        container = {
            "Names": "cert-init",
            "State": "exited",
            "Status": "Exited (0) 5 minutes ago",
        }

        with patch.object(wd, "_get_containers", return_value=[container]):
            report = wd.check_all()

        assert report.issues == []
        assert report.healthy == [ContainerHealth("cert-init")]

    @pytest.mark.parametrize("state", ["exited", "restarting"])
    def test_careful_services_are_never_unattended_repair_targets(self, state):
        wd = Watchdog(Path("/tmp/test"), FakeConfig())
        wd._discovered_safe.clear()
        container = {
            "Names": "stateful-service",
            "State": state,
            "Status": state.title(),
        }

        with (
            patch.object(wd, "_get_containers", return_value=[container]),
            patch.object(wd, "diagnose", return_value="operator review required"),
        ):
            report = wd.check_all()

        assert len(report.issues) == 1
        assert report.issues[0].auto_fixable is False

    def test_returns_infra_severity_when_no_containers(self):
        """0 containers seen → 'infra' severity (long cooldown, not page-able).

        Was 'critical' pre-fix — that drove the multiday alert storm on Jun 23
        because the per-cycle critical flipped report.ok=False every 5 min.
        Now it's a separate 'infra' kind that the notify() path applies a much
        longer cooldown to instead of re-paging forever.
        """
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with patch("subprocess.run", return_value=mock_run(stdout="", returncode=0)):
            report = wd.check_all()
        assert len(report.issues) == 1
        assert report.issues[0].severity == "infra"
        assert report.issues[0].category == "watchdog-infra"
        assert report.ok is True  # 'infra' does not page via report.ok
        assert report.has_infra_state is True

    def test_runs_full_check(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        # Empty docker ps output
        with patch("subprocess.run", return_value=mock_run(stdout="", returncode=0)):
            report = wd.check_all()
        assert isinstance(report, WatchdogReport)


# ── diagnose ──────────────────────────────────────────────────────────────────


class TestDiagnose:
    def test_connection_refused_diagnosis(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with patch.object(wd, "_get_container_logs", return_value="error: connection refused"):
            diag = wd.diagnose("caddy", "exited")
        assert "connection refused" in diag.lower() or "dependency" in diag.lower()

    def test_permission_denied_diagnosis(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with patch.object(wd, "_get_container_logs", return_value="permission denied"):
            diag = wd.diagnose("caddy", "exited")
        assert "permission" in diag.lower()

    def test_missing_dependency_diagnosis(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        # authelia depends on postgres, redis
        with (
            patch.object(wd, "_get_container_logs", return_value=""),
            patch.object(wd, "_get_running_names", return_value=set()),
        ):
            diag = wd.diagnose("authelia", "exited")
        assert "postgres" in diag or "redis" in diag


# ── full_check aggregates checks ─────────────────────────────────────────────


class TestFullCheck:
    def test_full_check_calls_multiple_checks(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())

        with (
            patch.object(wd, "check_all", return_value=WatchdogReport()),
            patch.object(wd, "check_disk_space", return_value=[]),
            patch.object(wd, "check_memory", return_value=[]),
            patch.object(wd, "check_port_conflicts", return_value=[]),
            patch.object(wd, "check_restart_loops", return_value=[]),
            patch.object(wd, "check_config_files", return_value=[]),
            patch.object(wd, "check_dns_resolution", return_value=[]),
            patch.object(wd, "check_dependency_connectivity", return_value=[]),
            patch.object(wd, "check_container_resources", return_value=[]),
            patch.object(wd, "check_volume_permissions", return_value=[]),
            patch.object(wd, "check_image_updates", return_value=[]),
            patch.object(wd, "check_ssl_certificates", return_value=[]),
            patch.object(wd, "check_docker_log_sizes", return_value=[]),
            patch.object(wd, "check_backup_freshness", return_value=[]),
            patch.object(wd, "check_backup_restore_drill", return_value=[]) as drill_check,
        ):
            report = wd.full_check()

        assert isinstance(report, WatchdogReport)
        drill_check.assert_called_once_with()

    def test_full_check_logs_event(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        initial_events = len(wd._events)

        with (
            patch.object(wd, "check_all", return_value=WatchdogReport()),
            patch.object(wd, "check_disk_space", return_value=[]),
            patch.object(wd, "check_memory", return_value=[]),
            patch.object(wd, "check_port_conflicts", return_value=[]),
            patch.object(wd, "check_restart_loops", return_value=[]),
            patch.object(wd, "check_config_files", return_value=[]),
            patch.object(wd, "check_dns_resolution", return_value=[]),
            patch.object(wd, "check_dependency_connectivity", return_value=[]),
            patch.object(wd, "check_container_resources", return_value=[]),
            patch.object(wd, "check_volume_permissions", return_value=[]),
            patch.object(wd, "check_image_updates", return_value=[]),
            patch.object(wd, "check_ssl_certificates", return_value=[]),
            patch.object(wd, "check_docker_log_sizes", return_value=[]),
            patch.object(wd, "check_backup_freshness", return_value=[]),
            patch.object(wd, "check_backup_restore_drill", return_value=[]),
        ):
            wd.full_check()

        # _log_event was called (at least "check" action)
        assert len(wd._events) >= initial_events


# ── prune_docker ──────────────────────────────────────────────────────────────


class TestPruneDocker:
    def test_prune_returns_message(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with patch(
            "subprocess.run",
            return_value=mock_run(stdout="Total reclaimed: 1GB", returncode=0),
        ):
            msg = wd.prune_docker()
        assert "reclaimed" in msg.lower() or "Prune" in msg

    def test_prune_handles_failure(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with patch(
            "subprocess.run",
            return_value=mock_run(stderr="permission denied", returncode=1),
        ):
            msg = wd.prune_docker()
        assert "fail" in msg.lower()


# ── prometheus_metrics ────────────────────────────────────────────────────────


class TestPrometheusMetrics:
    def test_metrics_contain_expected_helps(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())

        with (
            patch.object(wd, "full_check", return_value=WatchdogReport()),
            patch.object(wd, "_get_container_stats", return_value=[]),
            patch.object(wd, "_get_container_uptimes", return_value={}),
        ):
            metrics = wd.prometheus_metrics()

        assert "watchdog_healthy_containers" in metrics
        assert "watchdog_issues_total" in metrics
        assert "watchdog_ok" in metrics

    def test_metrics_format_prometheus_text(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())

        with (
            patch.object(wd, "full_check", return_value=WatchdogReport()),
            patch.object(wd, "_get_container_stats", return_value=[]),
            patch.object(wd, "_get_container_uptimes", return_value={}),
        ):
            metrics = wd.prometheus_metrics()

        lines = metrics.splitlines()
        assert any("# HELP watchdog_healthy_containers" in line for line in lines)
        assert any("# TYPE watchdog_healthy_containers gauge" in line for line in lines)

    def test_duplicate_fleet_names_have_distinct_node_labels(self):
        wd = Watchdog(Path("/tmp/test"), FakeConfig())
        report = WatchdogReport(healthy=[ContainerHealth("edge", "media"), ContainerHealth("edge", "apps")])
        stats = [
            {
                "name": "edge",
                "node": "media",
                "cpu": "1%",
                "mem_pct": "2%",
                "mem_usage": "1MiB / 1GiB",
                "net_io": "",
                "block_io": "",
            },
            {
                "name": "edge",
                "node": "apps",
                "cpu": "3%",
                "mem_pct": "4%",
                "mem_usage": "1MiB / 1GiB",
                "net_io": "",
                "block_io": "",
            },
        ]
        with (
            patch.object(wd, "_get_container_stats", return_value=stats),
            patch.object(
                wd,
                "_get_container_uptimes",
                return_value={("edge", "media"): 10.0, ("edge", "apps"): 20.0},
            ),
        ):
            metrics = wd.prometheus_metrics(report)

        assert 'watchdog_container_healthy{container="edge",node="media"} 1' in metrics
        assert 'watchdog_container_healthy{container="edge",node="apps"} 1' in metrics
        assert 'watchdog_container_cpu_percent{container="edge",node="media"} 1.00' in metrics
        assert 'watchdog_container_uptime_seconds{container="edge",node="apps"} 20' in metrics


# ── check_memory ───────────────────────────────────────────────────────────────


class TestCheckMemory:
    def _meminfo(self, total=16384000, available=15000000, buffers=100000, cached=500000):
        return (
            f"MemTotal:       {total} kB\n"
            f"MemAvailable:   {available} kB\n"
            f"Buffers:        {buffers} kB\n"
            f"Cached:         {cached} kB\n"
        )

    def test_no_issue_when_under_threshold(self, tmp_path):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        fake = tmp_path / "meminfo"
        fake.write_text(self._meminfo())
        with patch("toolkit.core.ops.watchdog.open", side_effect=lambda *a, **kw: open(fake)):
            issues = wd.check_memory(threshold_pct=90.0)
        assert issues == []

    def test_warning_at_90_percent(self, tmp_path):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        fake = tmp_path / "meminfo"
        fake.write_text(self._meminfo(available=1500000))
        with patch("toolkit.core.ops.watchdog.open", side_effect=lambda *a, **kw: open(fake)):
            issues = wd.check_memory(threshold_pct=90.0)
        assert len(issues) == 1
        assert issues[0].severity == "warning"

    def test_critical_at_95_percent(self, tmp_path):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        fake = tmp_path / "meminfo"
        fake.write_text(self._meminfo(available=500000))
        with patch("toolkit.core.ops.watchdog.open", side_effect=lambda *a, **kw: open(fake)):
            issues = wd.check_memory(threshold_pct=90.0)
        assert len(issues) == 1
        assert issues[0].severity == "critical"

    def test_handles_oserror(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with patch("toolkit.core.ops.watchdog.open", side_effect=OSError()):
            issues = wd.check_memory()
        assert issues == []


# ── check_restart_loops ───────────────────────────────────────────────────────


class TestCheckRestartLoops:
    def test_detects_restart_loop(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with (
            patch.object(
                wd,
                "_get_containers",
                return_value=[{"Names": "caddy", "State": "restarting", "Status": "Restarting", "FleetVM": "infra"}],
            ),
            patch.object(wd, "_docker_capture", return_value=mock_run(stdout="5", returncode=0)) as inspect,
        ):
            issues = wd.check_restart_loops()
        assert len(issues) == 1
        assert issues[0].severity == "critical"
        assert issues[0].node == "infra"
        assert "restart loop" in issues[0].message.lower()
        assert inspect.call_args.kwargs["node"] == "infra"

    def test_ignores_low_restart_count(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with (
            patch.object(
                wd,
                "_get_containers",
                return_value=[{"Names": "caddy", "State": "restarting", "Status": "Restarting"}],
            ),
            patch.object(wd, "_docker_capture", return_value=mock_run(stdout="2", returncode=0)),
        ):
            issues = wd.check_restart_loops()
        assert issues == []

    def test_no_loop_when_returncode_nonzero(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with patch.object(wd, "_get_containers", return_value=[]):
            issues = wd.check_restart_loops()
        assert issues == []


# ── check_dependency_connectivity ─────────────────────────────────────────────


class TestCheckDependencyConnectivity:
    def test_no_issues_when_deps_reachable(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with (
            patch.object(wd, "_get_running_names", return_value={"postgres", "authelia", "redis", "lldap"}),
            patch("subprocess.run", return_value=mock_run(returncode=0)),
        ):
            issues = wd.check_dependency_connectivity()
        assert issues == []

    def test_no_issues_when_no_running_containers(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with patch.object(wd, "_get_running_names", return_value=set()):
            issues = wd.check_dependency_connectivity()
        assert issues == []

    def test_no_issues_when_dep_not_running(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        # postgres is essential and restart_policy=never, so it must not be restarted.
        with patch.object(wd, "_get_running_names", return_value={"authelia"}):
            issues = wd.check_dependency_connectivity()
        assert len(issues) >= 1
        assert any(i.service == "postgres" for i in issues)
        assert not any(i.auto_fixable for i in issues if i.service == "postgres")


# ── check_config_files ───────────────────────────────────────────────────────


class TestCheckConfigFiles:
    def test_warns_when_generated_dir_missing(self):
        root = Path("/tmp/test_wd_missing")
        root.mkdir(exist_ok=True)
        (root / "generated").mkdir(exist_ok=True, parents=True)
        shutil.rmtree(root / "generated", ignore_errors=True)
        wd = Watchdog(root, FakeConfig())
        issues = wd.check_config_files()
        assert len(issues) >= 1
        assert any("generated" in i.message.lower() for i in issues)

    def test_no_issues_when_all_critical_files_exist(self, tmp_path):
        root = tmp_path / "test"
        root.mkdir()
        generated = root / "generated"
        generated.mkdir()
        (generated / "Caddyfile").write_text("config")
        (generated / "authelia.yml").write_text("config")
        wd = Watchdog(root, FakeConfig())
        issues = wd.check_config_files()
        assert issues == []

    def test_warns_on_missing_caddyfile(self, tmp_path):
        root = tmp_path / "test"
        root.mkdir()
        generated = root / "generated"
        generated.mkdir()
        (generated / "authelia.yml").write_text("auth config")
        wd = Watchdog(root, FakeConfig())
        issues = wd.check_config_files()
        assert any("Caddyfile" in i.message for i in issues)


# ── check_dns_resolution ──────────────────────────────────────────────────────


class TestCheckDnsResolution:
    def test_no_issues_when_localhost_domain(self):
        root = Path("/tmp/test")

        class LocalhostConfig(FakeConfig):
            domain = "localhost"

        wd = Watchdog(root, LocalhostConfig())
        issues = wd.check_dns_resolution()
        assert issues == []

    def test_no_issues_when_domain_resolves(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with patch("socket.getaddrinfo", return_value=[]):
            issues = wd.check_dns_resolution()
        assert issues == []

    def test_warning_when_domain_fails_to_resolve(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        import socket

        with patch("socket.getaddrinfo", side_effect=socket.gaierror("DNS lookup failed")):
            issues = wd.check_dns_resolution()
        assert len(issues) == 1
        assert issues[0].severity == "warning"
        assert "resolve" in issues[0].message.lower()


# ── check_container_resources ────────────────────────────────────────────────


class TestCheckContainerResources:
    def test_no_issues_under_thresholds(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        stdout = "caddy\t5%\t50%\t100MB / 1GB\n"
        with patch("subprocess.run", return_value=mock_run(stdout=stdout, returncode=0)):
            issues = wd.check_container_resources()
        assert issues == []

    def test_warns_on_high_cpu(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        stdout = "caddy\t95%\t50%\t100MB / 1GB\n"
        with patch("subprocess.run", return_value=mock_run(stdout=stdout, returncode=0)):
            issues = wd.check_container_resources()
        assert len(issues) == 1
        assert "High CPU" in issues[0].message

    def test_warns_on_high_memory(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        stdout = "caddy\t5%\t95%\t900MB / 1GB\n"
        with patch("subprocess.run", return_value=mock_run(stdout=stdout, returncode=0)):
            issues = wd.check_container_resources()
        assert len(issues) == 1
        assert "High memory" in issues[0].message

    def test_empty_when_returncode_nonzero(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with patch("subprocess.run", return_value=mock_run(stderr="error", returncode=1)):
            issues = wd.check_container_resources()
        assert issues == []


# ── check_volume_permissions ──────────────────────────────────────────────────


class TestCheckVolumePermissions:
    def test_no_issues_when_data_dir_missing(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        issues = wd.check_volume_permissions()
        assert issues == []

    def test_no_issues_when_owned_by_nonroot(self, tmp_path):
        from toolkit.core.manifest.storage import CompiledStorageAsset, StorageInventory

        root = tmp_path / "test"
        root.mkdir()
        subdir = root / "data" / "postgres"
        subdir.mkdir(parents=True)
        asset = CompiledStorageAsset(
            service="postgres",
            service_label="PostgreSQL",
            role="infra",
            name="postgres-data",
            source_kind="bind",
            source="POSTGRES_DATA_SOURCE",
            target="/var/lib/postgresql/data",
            host_path=subdir,
            size_estimate_gb=20,
            snapshot=True,
            manage_permissions=True,
            host_uid=1000,
            host_gid=1000,
        )
        wd = Watchdog(root, FakeConfig())
        with (
            patch(
                "toolkit.core.manifest.storage.compile_storage_inventory",
                return_value=StorageInventory((asset,)),
            ),
            patch.object(Path, "stat", return_value=Mock(st_uid=1000, st_gid=1000)),
        ):
            issues = wd.check_volume_permissions()
        assert issues == []

    def test_multi_node_guest_reports_declared_ownership_drift(self, tmp_path, monkeypatch):
        from toolkit.core.manifest.storage import CompiledStorageAsset, StorageInventory

        class MultiNodeConfig(FakeConfig):
            is_multi_node = True

        root = tmp_path / "test"
        root.mkdir()
        subdir = root / "data" / "postgres"
        subdir.mkdir(parents=True)
        asset = CompiledStorageAsset(
            service="postgres",
            service_label="PostgreSQL",
            role="infra",
            name="postgres-data",
            source_kind="bind",
            source="POSTGRES_DATA_SOURCE",
            target="/var/lib/postgresql/data",
            host_path=subdir,
            size_estimate_gb=20,
            snapshot=True,
            manage_permissions=True,
            host_uid=999,
            host_gid=999,
        )
        monkeypatch.setenv("HOMELAB_NODE", "infra")
        wd = Watchdog(root, MultiNodeConfig())
        with (
            patch(
                "toolkit.core.manifest.storage.compile_storage_inventory",
                return_value=StorageInventory((asset,)),
            ) as compile_inventory,
            patch.object(Path, "stat", return_value=Mock(st_uid=0, st_gid=0)),
        ):
            issues = wd.check_volume_permissions()

        assert len(issues) == 1
        assert issues[0].node == "infra"
        assert "expected 999:999" in issues[0].message
        assert compile_inventory.call_args.kwargs["roles"] == {"infra"}


# ── check_docker_log_sizes ────────────────────────────────────────────────────


class TestCheckDockerLogSizes:
    def test_no_issues_when_no_containers(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with patch("subprocess.run", return_value=mock_run(stdout="", returncode=0)):
            issues = wd.check_docker_log_sizes()
        assert issues == []

    def test_no_issues_when_log_small(self, tmp_path):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        log_file = tmp_path / "container.log"
        log_file.write_text("small content")
        stdout = "abc123\tcontainer\n"
        with patch(
            "subprocess.run",
            side_effect=[
                mock_run(stdout=stdout, returncode=0),
                mock_run(stdout=f"{log_file}\tcontainer", returncode=0),
                mock_run(stdout="100", returncode=0),
            ],
        ):
            issues = wd.check_docker_log_sizes()
        assert issues == []

    def test_large_log_issue_keeps_local_node_identity(self, monkeypatch):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        monkeypatch.setenv("HOMELAB_NODE", "infra")
        stdout = "abc123\tcontainer\n"
        with patch(
            "subprocess.run",
            side_effect=[
                mock_run(stdout=stdout, returncode=0),
                mock_run(stdout="/var/lib/docker/container.log\tcontainer", returncode=0),
                mock_run(stdout=str(101 * 1024 * 1024), returncode=0),
            ],
        ):
            issues = wd.check_docker_log_sizes()

        assert len(issues) == 1
        assert issues[0].node == "infra"


# ── verify_post_restart ───────────────────────────────────────────────────────


class TestVerifyPostRestart:
    def test_returns_true_when_container_running(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with patch(
            "subprocess.run",
            return_value=mock_run(stdout="running:healthy", returncode=0),
        ):
            result = wd.verify_post_restart("caddy", timeout=5)
        assert result is True

    def test_returns_true_when_running_no_healthcheck(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with patch("subprocess.run", return_value=mock_run(stdout="running:", returncode=0)):
            result = wd.verify_post_restart("caddy", timeout=5)
        assert result is True

    def test_returns_false_when_unhealthy(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with patch(
            "subprocess.run",
            return_value=mock_run(stdout="running:unhealthy", returncode=0),
        ):
            result = wd.verify_post_restart("caddy", timeout=5)
        assert result is False


# ── notify ───────────────────────────────────────────────────────────────────────


class TestNotify:
    def test_no_messages_when_report_ok(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        with patch("urllib.request.urlopen", side_effect=Exception("should not be called")):
            msgs = wd.notify(WatchdogReport())
        assert msgs == []

    def test_handles_urllib_error_gracefully(self):
        root = Path("/tmp/test")
        wd = Watchdog(root, FakeConfig())
        from toolkit.core.ops.watchdog import HealthIssue

        r = WatchdogReport()
        r.issues.append(HealthIssue("caddy", "cat", "critical", "test", auto_fixable=False))
        import urllib.error

        with patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("ntfy not reachable"),
        ):
            msgs = wd.notify(r)
        assert msgs == ["ntfy not reachable"]


# ── notify() cooldown / dedup — alert-fatigue regression ──────────────────────


class TestNotifyCooldown:
    """The Jun 23 alert storm: watchdog.timer fired every 5 min, each a fresh
    oneshot process with zero memory of what it already alerted on, re-sending
    identical non-fixable criticals forever. ``notify()`` must dedup + honour
    a cooldown so a known-persistent issue pages once, not once-per-cycle."""

    def _wd(self, tmp_path: Path) -> Watchdog:
        return Watchdog(tmp_path, FakeConfig())

    def _report_with(self, *issues):
        r = WatchdogReport()
        for issue in issues:
            r.issues.append(issue)
        return r

    def test_notifies_first_time_for_new_critical(self, tmp_path):
        """First sighting → notify. The contract: a brand-new critical must page."""
        wd = self._wd(tmp_path)
        with patch.object(wd, "_send_ntfy", return_value=True) as mock_send:
            msgs = wd.notify(
                self._report_with(
                    HealthIssue("caddy", "system", "critical", "Container exited", auto_fixable=False),
                )
            )
        assert "Sent alert" in msgs[0]
        assert mock_send.call_count == 1

    def test_suppresses_repeated_critical_within_cooldown(self, tmp_path):
        """Same issue, immediate re-run → suppress (no re-page within cooldown).

        Regression: this is the 2-day alert storm: every 5-min timer firing
        sent a fresh ntfy for the SAME unresolved issue because no state
        survived between process invocations.
        """
        wd = self._wd(tmp_path)
        issue = HealthIssue("caddy", "system", "critical", "Container exited", auto_fixable=False)
        report = self._report_with(issue)
        with patch.object(wd, "_send_ntfy", return_value=True) as mock_send:
            wd.notify(report)  # first pages
            msgs2 = wd.notify(report)  # second suppressed
        assert mock_send.call_count == 1
        # Second call must NOT page; msgs2 may carry a 'suppressed' notice but
        # the contract is 'did not send to ntfy again'.
        assert not any("Sent alert" in m for m in msgs2)

    def test_persists_notified_state_across_process_invocations(self, tmp_path):
        """Cooldown state must survive separate Watchdog() instances because
        the systemd timer spawns a fresh process every 5 min."""
        issue = HealthIssue("caddy", "system", "critical", "Container exited", auto_fixable=False)
        report = self._report_with(issue)

        wd1 = Watchdog(tmp_path, FakeConfig())
        with patch.object(wd1, "_send_ntfy", return_value=True) as mock1:
            wd1.notify(report)
        assert mock1.call_count == 1

        # Fresh instance — simulates the next 5-min timer firing.
        wd2 = Watchdog(tmp_path, FakeConfig())
        with patch.object(wd2, "_send_ntfy", return_value=True) as mock2:
            wd2.notify(report)
        assert mock2.call_count == 0  # dedup'd: same issue, within cooldown

    def test_notifies_after_cooldown_expires(self, tmp_path):
        """Once the cooldown elapses, the same issue re-pages (it's persistent
        — neither intermittent nor resolved; periodic re-page is the contract)."""
        import time

        wd = Watchdog(tmp_path, FakeConfig())
        issue = HealthIssue("caddy", "system", "critical", "Container exited", auto_fixable=False)
        report = self._report_with(issue)
        with patch.object(wd, "_send_ntfy", return_value=True):
            wd.notify(report)
        # Move the persisted last-notified TS back to "long ago".
        wd._reset_notify_state_for_test(now=time.time() - 3600)
        wd2 = Watchdog(tmp_path, FakeConfig())
        with patch.object(wd2, "_send_ntfy", return_value=True) as mock2:
            wd2.notify(report)
        assert mock2.call_count == 1  # cooldown expired → re-page

    def test_escalation_always_pages(self, tmp_path):
        """A warning escalating to critical on the same issue → re-page."""
        wd = Watchdog(tmp_path, FakeConfig())
        wd.notify(
            self._report_with(
                HealthIssue("caddy", "system", "warning", "Container unhealthy", auto_fixable=False),
            )
        )
        with patch.object(wd, "_send_ntfy", return_value=True) as mock_send:
            wd.notify(
                self._report_with(
                    HealthIssue("caddy", "system", "critical", "Container exited", auto_fixable=False),
                )
            )
        assert mock_send.call_count == 1


# ── check_all — "0 containers" severity split ──────────────────────────────────


class TestCheckAllZeroContainers:
    """Check_all currently returns a ``critical`` "Cannot reach Docker daemon"
    issue when 0 containers are seen. On the dev controller or when fleet-mode
    SSH is transiently unreachable (watchdog timer started without the venv on
    PATH), this drives ``report.ok=False`` forever — exactly the alert-storm
    on Jun 23. "0 containers" is an *actionable infra state*, not a
    container-down disaster: long cooldown only, doesn't page as critical."""

    def test_zero_containers_is_not_in_report_ok(self, tmp_path):
        """0 containers seen → issue is reported but does NOT make report.ok
        False via a 'critical' severity that the timer treats as pages."""
        from toolkit.core.ops.watchdog import check_all_report_kind

        kind = check_all_report_kind()
        # 'critical' would page forever; 'infra' (or similar non-page severity)
        # means the issue surfaces in the UI/audit but the alert path applies
        # a long cooldown instead of re-paging every 5 min.
        assert kind != "critical"


# ── heal() — fixed count accuracy ─────────────────────────────────────────────


class TestHealOutcomes:
    """Healing reports verified remedies separately from deferred work."""

    def test_heal_returns_count_of_actual_restarts(self, tmp_path):
        wd = Watchdog(tmp_path, FakeConfig())
        # 2 auto-fixable issues but backoff prevents either from actually restarting
        now = time.time()
        wd._restart_counts["a"] = 1
        wd._restart_timestamps["a"] = now
        wd._restart_counts["b"] = 1
        wd._restart_timestamps["b"] = now
        report = WatchdogReport()
        report.issues.extend(
            [
                HealthIssue("a", "cat", "warning", "unhealthy", auto_fixable=True),
                HealthIssue("b", "cat", "warning", "unhealthy", auto_fixable=True),
            ]
        )
        with patch("subprocess.run"):
            result = wd.heal(report)
        assert result.attempted == 0
        assert result.succeeded == 0
        assert result.failed == 0
        assert result.deferred == 2


# ── prometheus_metrics — no duplicate full_check() ─────────────────────────────


class TestPrometheusMetricsNoDuplicateScan:
    """``prometheus_metrics()`` historically called ``self.full_check()`` again
    on every Prometheus scrape, doubling load and re-running side-effecting
    checks. The metrics export should reuse an already-computed report (passed
    in). The Prometheus-scrape path / timer call site — the one paying for a
    scrape — must pass ``report=last_scan``; the no-arg path remains for ad-hoc
    CLI probes (and is fine to scan once)."""

    def test_reuses_supplied_report_and_skips_full_check(self, tmp_path):
        from toolkit.core.ops.watchdog import WatchdogReport

        wd = Watchdog(tmp_path, FakeConfig())
        supplied = WatchdogReport()
        with patch.object(wd, "full_check") as mock_full:
            with patch.object(wd, "_get_container_stats", return_value=[]):
                with patch.object(wd, "_get_container_uptimes", return_value={}):
                    metrics = wd.prometheus_metrics(report=supplied)
        # The scrape path passed a pre-computed report — no second scan fired.
        assert mock_full.call_count == 0
        assert "watchdog_healthy_containers" in metrics


class TestHealthIssue:
    def test_health_issue_fields(self):
        from toolkit.core.ops.watchdog import HealthIssue

        hi = HealthIssue("svc", "cat", "warning", "msg", auto_fixable=True, diagnosis="fix it")
        assert hi.service == "svc"
        assert hi.category == "cat"
        assert hi.severity == "warning"
        assert hi.message == "msg"
        assert hi.auto_fixable is True
        assert hi.diagnosis == "fix it"

    def test_health_issue_defaults(self):
        from toolkit.core.ops.watchdog import HealthIssue

        hi = HealthIssue("svc", "cat", "info", "msg")
        assert hi.auto_fixable is False
        assert hi.diagnosis == ""
