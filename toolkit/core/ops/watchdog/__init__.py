"""Watchdog that monitors containers and attempts safe auto-recovery.

Provides container health scanning, automated troubleshooting (log analysis,
dependency checks, port conflict detection), safe auto-restart with exponential
backoff, post-restart health verification, container resource monitoring,
volume permission checks, notification dispatch to ntfy, Prometheus-compatible
metrics export, persistent event logging, and a history of events for the UI.

Usage:
    from toolkit.core.ops.watchdog import Watchdog
    wd = Watchdog(root, config)
    report = wd.full_check()     # One-shot health scan
    result = wd.heal(report)     # Attempt to fix issues found
    wd.notify(report)            # Send alerts via ntfy

The data models and constants live in ``toolkit.core.watchdog.models``.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.core.projects.placement import project_node
from toolkit.core.state.paths import watchdog_events_path, watchdog_state_path
from toolkit.core.watchdog.models import (
    MAX_LOG_TAIL,
    MAX_STDERR_LEN,
    NOTIFY_COOLDOWN_CRITICAL_S,
    NOTIFY_COOLDOWN_INFRA_S,
    NOTIFY_COOLDOWN_WARNING_S,
    RESTART_BACKOFF_BASE,
    ContainerHealth,
    HealResult,
    HealthIssue,
    WatchdogEvent,
    WatchdogReport,
    _issue_key,
    _parse_docker_uptime,
    check_all_report_kind,
)

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.ops.backup_inventory import BackupNodeState
    from toolkit.core.ops.backup_restore_drill import BackupDrillEvidence

logger = logging.getLogger(__name__)


def _container_state_key(service: str, node: str = "") -> str:
    return f"{node}/{service}" if node else service


def _split_container_state_key(key: str) -> tuple[str, str]:
    if "/" not in key:
        return key, ""
    node, service = key.split("/", 1)
    return service, node


def build_reverse_dependency_map(forward: dict[str, list[str]]) -> dict[str, list[str]]:
    """Build the reverse dependency map: {dep: [consumers]}.

    Given a forward map ``{consumer: [deps]}`` (e.g. ``{"authelia": ["postgres",
    "redis"]}``), returns ``{dep: [consumers]}`` (e.g. ``{"postgres": ["authelia",
    "grafana"], "redis": ["authelia"]}``).

    The reverse map is the foundation of health-aware cascading restarts (F3):
    when a dependency (postgres) restarts, downstream consumers (authelia,
    grafana, nextcloud) are re-verified after a grace period, and a consumer
    whose dependency is terminal gets a ``degraded`` severity rather than
    falsely reporting healthy.

    Every forward-map key also appears in the reverse map (as a key with an
    empty list if nothing depends on it), so callers can do
    ``rev.get(consumer, [])`` without KeyErrors.
    """
    rev: dict[str, list[str]] = {svc: [] for svc in forward}
    for consumer, deps in forward.items():
        for dep in deps:
            rev.setdefault(dep, [])
            # Defensive dedup — a consumer shouldn't list a dep twice.
            if consumer not in rev[dep]:
                rev[dep].append(consumer)
    return rev


class Watchdog:
    """Monitors container health and attempts auto-recovery."""

    # Container restart policies are now sourced from category.yaml per-service
    # metadata (toolkit/core/config/service_metadata.py). The frozensets below
    # are used as ADDITIONAL fallbacks for containers that don't have a
    # category.yaml entry (e.g. exporters, agents, proxy infrastructure).
    # At runtime, _discover_docker_labels() + service_metadata augment these.
    # Restart policy + dependency edges are now read from service.yaml's
    # restart_policy (safe/careful/never) + depends_on fields via
    # service_metadata — no hardcoded fallback lists.

    def __init__(self, root: Path, config: Config):
        self.root = root
        self.config = config
        self._events: list[WatchdogEvent] = []
        self._restart_counts: dict[str, int] = {}  # track restart attempts per container
        self._restart_timestamps: dict[str, float] = {}  # last restart time per container
        # Per-issue notify state: {issue_key: {last_notified_ts, severity, notified_count, terminal}}
        # Loaded alongside restart state from watchdog-state.json so the systemd
        # timer — which spawns a fresh process every 5 min — inherits memory of
        # what it already paged about. Without this, every known-persistent
        # issue re-pages every cycle forever (the Jun 23 alert storm).
        self._notify_state: dict[str, dict] = {}
        # F5: last watchdog-auto-triggered recover timestamp (per VM). Persisted
        # so the 1h cooldown survives across the 5-min process boundaries.
        self._last_auto_recover_at: dict[str, float] = {}
        self._backup_nodes: tuple[BackupNodeState, ...] = ()
        self._backup_drill_evidence: BackupDrillEvidence | None = None
        self._fleet_scan_errors: set[str] = set()
        self._container_snapshot: list[dict] | None = None
        self._event_log_path = watchdog_events_path(root)
        self._state_path = watchdog_state_path(root)
        self._load_persistent_events()
        self._load_restart_state()
        # Docker label auto-discovery storage
        self._discovered_safe: set[str] = set()
        self._discovered_careful: set[str] = set()
        self._discovered_blocked: set[str] = set()
        self._discovered_deps: dict[str, list[str]] = {}
        self._discover_docker_labels()
        # Merge YAML-sourced service metadata with the base hardcoded lists
        self._merge_service_metadata()

    def _merge_service_metadata(self) -> None:
        """Merge category.yaml service metadata into the runtime sets.

        service.yaml's ``restart_policy`` (safe/careful/never), ``depends_on``,
        and ``memory_tier`` are now the single source of truth — no hardcoded
        fallback list. A metadata-load failure logs (so the operator notices)
        rather than silently reverting to a divergent set nobody audits.
        """
        try:
            from toolkit.core.config.service_metadata import (
                careful_restart_services,
                dependency_map,
                never_restart_services,
                safe_to_restart_services,
            )

            self._discovered_safe.update(safe_to_restart_services())
            self._discovered_careful.update(careful_restart_services())
            self._discovered_blocked.update(never_restart_services())
            for svc, deps in dependency_map().items():
                self._discovered_deps.setdefault(svc, deps)
            from toolkit.services import discover_service_plugins

            for plugin in discover_service_plugins():
                if plugin.essential:
                    self._discovered_safe.discard(plugin.service)
                    self._discovered_careful.add(plugin.service)
            projects = getattr(getattr(self.config, "projects", None), "entries", ())
            self._discovered_safe.update(project.subdomain for project in projects)
        except Exception as exc:
            logger.warning("service_metadata load failed; restart policy + deps EMPTY: %s", exc)

    @property
    def events(self) -> list[WatchdogEvent]:
        return list(self._events)

    def _log_event(self, action: str, service: str, detail: str) -> None:
        self._events.append(WatchdogEvent(time.time(), action, service, detail))
        # Keep last 200 events in memory
        if len(self._events) > 200:
            self._events = self._events[-200:]
        self._persist_event(self._events[-1])

    def _load_persistent_events(self) -> None:
        """Load last 200 saved events from disk on startup."""
        try:
            if self._event_log_path.exists():
                # Read only the tail to avoid loading large files fully into memory
                with open(self._event_log_path, "rb") as f:
                    # Seek near end; 200 events × ~200 bytes each ≈ 40KB max
                    try:
                        f.seek(-50_000, 2)
                        f.readline()  # skip partial first line
                    except OSError:
                        f.seek(0)
                    tail = f.read().decode("utf-8", errors="replace")
                for line in tail.strip().splitlines()[-200:]:
                    try:
                        d = json.loads(line)
                        self._events.append(
                            WatchdogEvent(
                                d["timestamp"],
                                d["action"],
                                d["service"],
                                d["detail"],
                            )
                        )
                    except (json.JSONDecodeError, KeyError):
                        continue
        except OSError:
            pass

    def _persist_event(self, event: WatchdogEvent) -> None:
        """Append an event to the on-disk log (JSONL format)."""
        try:
            self._event_log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._event_log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(asdict(event)) + "\n")
            # Rotate if file exceeds 1 MB (atomic write to avoid TOCTOU race)
            if self._event_log_path.stat().st_size > 1_048_576:
                tmp_path = self._event_log_path.with_suffix(".event_log.tmp")
                lines = self._event_log_path.read_text(encoding="utf-8").strip().splitlines()
                try:
                    tmp_path.write_text(
                        "\n".join(lines[-200:]) + "\n",
                        encoding="utf-8",
                    )
                    os.replace(tmp_path, self._event_log_path)
                except OSError:
                    if tmp_path.exists():
                        tmp_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _load_restart_state(self) -> None:
        """Load restart counts/timestamps AND notify-state from disk so backoff
        and alert cooldown both survive across ``watchdog daemon`` invocations
        (cron every 5 min creates a fresh ``Watchdog()`` instance — without
        persistence, backoff state is lost and a crashing container gets
        restarted every 5 min forever; likewise alert repeat-cooldowns are
        lost and a persistent non-fixable issue re-pages every cycle forever).
        """
        try:
            if not self._state_path.exists():
                return
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            # Prune entries older than 24h — they're stale and shouldn't
            # block restarts after a legitimate fix.
            cutoff = time.time() - 86400
            for name, ts in (data.get("timestamps") or {}).items():
                if ts > cutoff:
                    self._restart_timestamps[name] = float(ts)
                    count = (data.get("counts") or {}).get(name, 0)
                    if count:
                        self._restart_counts[name] = int(count)
            # Per-issue notify state (cooldown + dedup). Prune entries whose
            # last_notified_ts is older than the longest cooldown (6h) — they
            # can no longer be in-cooldown.
            notify_cutoff = time.time() - NOTIFY_COOLDOWN_INFRA_S - 60
            for key, rec in (data.get("notify_state") or {}).items():
                if not isinstance(rec, dict):
                    continue
                last = float(rec.get("last_notified_ts") or 0)
                if last > notify_cutoff:
                    self._notify_state[key] = {
                        "last_notified_ts": last,
                        "severity": rec.get("severity", ""),
                        "notified_count": int(rec.get("notified_count") or 0),
                        "terminal": bool(rec.get("terminal", False)),
                    }
            # F5: per-VM last-auto-recover timestamps (1h cooldown). Prune >7d.
            recover_cutoff = time.time() - 7 * 86400
            for vm, ts in (data.get("last_auto_recover_at") or {}).items():
                try:
                    if float(ts) > recover_cutoff:
                        self._last_auto_recover_at[vm] = float(ts)
                except (TypeError, ValueError):
                    continue
        except (OSError, json.JSONDecodeError, ValueError, TypeError):
            pass

    def _save_restart_state(self) -> None:
        """Persist restart counts/timestamps + notify-state to disk for the next ``watchdog`` run."""
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "counts": self._restart_counts,
                "timestamps": self._restart_timestamps,
                "notify_state": self._notify_state,
                "last_auto_recover_at": self._last_auto_recover_at,
                "saved_at": time.time(),
            }
            tmp = self._state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload), encoding="utf-8")
            os.replace(tmp, self._state_path)
        except OSError:
            pass

    def _reset_notify_state_for_test(self, *, now: float | None = None) -> None:
        """Test helper: walk every notify-state entry back so the next
        ``notify()`` call treats all entries as cooldown-expired.

        Used by ``TestNotifyCooldown.test_notifies_after_cooldown_expires``
        to verify the cooldown boundary without freezing the test for 30 min.
        The offset is kept BELOW the load-pruning cutoff (6h + 60s) so the
        state survives ``_load_restart_state`` on a fresh ``Watchdog()``
        instance, and ABOVE the critical cooldown (30 min) so the same issue
        re-pages on the next cycle. Persists to disk so a fresh ``Watchdog()``
        instance (the systemd-timer model) sees the reset state.
        """
        ts = now if now is not None else time.time()
        reset_to = ts - 90 * 60  # 90 min: > 30 min critical cooldown, < 6 h load-prune
        for rec in self._notify_state.values():
            rec["last_notified_ts"] = reset_to
        self._save_restart_state()

    # ── Docker label auto-discovery ──────────────────────────────

    def _discover_docker_labels(self) -> None:
        """Discover Docker labels for auto-configuration of restart policies and dependencies.

        Reads homelab.watchdog.restart-policy and homelab.watchdog.depends-on labels
        from running containers and merges them with plugin manifest policy.

        Label values:
          homelab.watchdog.restart-policy: "safe", "careful", or "never"
          homelab.watchdog.depends-on: comma-separated dependency container names
        """
        command = [
            "docker",
            "ps",
            "--format",
            '{{.Names}}\t{{.Label "homelab.watchdog.restart-policy"}}\t{{.Label "homelab.watchdog.depends-on"}}',
        ]
        try:
            results: list[str] = []
            if self._use_fleet_watchdog():
                import shlex

                for node in self.config.enabled_nodes:
                    rc, out, _err = self._fleet_docker(node, shlex.join(command), timeout=30)
                    if rc == 0:
                        results.append(out)
            else:
                result = self._run(command)
                if result.returncode == 0:
                    results.append(result.stdout)
            for output in results:
                for raw in output.strip().splitlines():
                    if not raw.strip():
                        continue
                    parts = raw.split("\t", 2)
                    name = parts[0]

                    if len(parts) >= 2 and parts[1]:
                        policy = parts[1].strip().lower()
                        if policy == "safe":
                            self._discovered_safe.add(name)
                        elif policy == "careful":
                            self._discovered_safe.discard(name)
                            self._discovered_careful.add(name)
                        elif policy == "never":
                            self._discovered_blocked.add(name)

                    if len(parts) >= 3 and parts[2]:
                        deps = [dependency.strip() for dependency in parts[2].split(",") if dependency.strip()]
                        if deps:
                            existing = self._discovered_deps.get(name, [])
                            for dependency in deps:
                                if dependency not in existing:
                                    existing.append(dependency)
                            self._discovered_deps[name] = existing
        except (OSError, subprocess.SubprocessError):
            pass

    @property
    def _safe_restart_names(self) -> set[str]:
        """Combined set of containers safe to restart (YAML metadata + base defaults + Docker labels)."""
        return self._discovered_safe - self._discovered_blocked

    def restartable_services(self) -> frozenset[str]:
        """Container names safe for unattended restart (``restart_policy: safe``)."""
        return frozenset(self._safe_restart_names)

    def structured_heal_services(self) -> frozenset[str]:
        """Services handled by plugin ``heal()`` — skip generic container restart."""
        from toolkit.services import heal_routing_map

        return frozenset(heal_routing_map())

    @property
    def _dependency_links(self) -> dict[str, list[str]]:
        """Combined dependency map (YAML metadata + compose graph + Docker labels)."""
        from toolkit.core.registry.stagger_planner import compose_dependency_map

        # Start with YAML-sourced deps + extra hardcoded deps as base
        merged: dict[str, list[str]] = {k: list(v) for k, v in self._discovered_deps.items()}
        for svc, deps in compose_dependency_map(self.root).items():
            bucket = merged.setdefault(svc, [])
            for dep in deps:
                if dep not in bucket:
                    bucket.append(dep)
        for svc, deps in self._discovered_deps.items():
            bucket = merged.setdefault(svc, [])
            for dep in deps:
                if dep not in bucket:
                    bucket.append(dep)
        return merged

    @property
    def _reverse_dep_links(self) -> dict[str, list[str]]:
        """Reverse dependency map: {dep: [consumers]}. F3 cascading-restart basis.

        Built lazily from :pyattr:`_dependency_links` via the pure
        :func:`build_reverse_dependency_map`. Used by heal() to re-verify
        downstream consumers after a successful restart, and to emit a degraded
        severity when a dependency is terminal.
        """
        return build_reverse_dependency_map(self._dependency_links)

    # ── Docker helpers ─────────────────────────────────────────

    def _run(self, cmd: list[str], timeout: int = 15) -> subprocess.CompletedProcess:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)

    def _service_vm(self, service: str) -> str | None:
        """Map container name to fleet VM (None if unknown or local mode)."""
        if not self._use_fleet_watchdog():
            return None
        nodes: set[str] = set()
        for c in self._get_containers():
            name = (c.get("Names") or "").lstrip("/")
            if name == service:
                node = str(c.get("FleetVM") or "")
                if node:
                    nodes.add(node)
        return next(iter(nodes)) if len(nodes) == 1 else None

    def _fleet_docker(self, vm: str, docker_cmd: str, *, timeout: int = 90) -> tuple[int, str, str]:
        from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

        return ssh_run_on_vm(self.config, self.config.node_ip(vm), docker_cmd, root=self.root, timeout=timeout)

    @property
    def _local_node(self) -> str:
        return os.environ.get("HOMELAB_NODE", "")

    def _node_capture(self, node: str, command: list[str], *, timeout: int) -> subprocess.CompletedProcess:
        """Run a host command locally or on one explicitly selected fleet node."""
        if self._use_fleet_watchdog():
            import shlex

            rc, out, err = self._fleet_docker(node, shlex.join(command), timeout=timeout)
            return subprocess.CompletedProcess(command, rc, out, err)
        return self._run(command, timeout=timeout)

    def _docker_capture(
        self,
        args: list[str],
        *,
        service: str = "",
        node: str = "",
        timeout: int = 60,
    ) -> subprocess.CompletedProcess:
        """Run one Docker command locally or on the explicitly identified fleet node."""
        if self._use_fleet_watchdog():
            import shlex

            target = node or (self._service_vm(service) if service else "")
            if not target:
                return subprocess.CompletedProcess(
                    ["docker", *args],
                    1,
                    "",
                    "container node is ambiguous or unavailable",
                )
            rc, out, err = self._fleet_docker(target, shlex.join(["docker", *args]), timeout=timeout)
            return subprocess.CompletedProcess(["docker", *args], rc, out, err)
        return self._run(["docker", *args], timeout=timeout)

    def _docker_action(self, service: str, action: str, *, node: str = "", timeout: int = 60) -> tuple[int, str]:
        """restart|start|stop — routes to guest SSH when in fleet watchdog mode."""
        result = self._docker_capture([action, service], service=service, node=node, timeout=timeout)
        self._container_snapshot = None
        combined = (result.stdout or "") + (result.stderr or "")
        return result.returncode, combined.strip()

    def _use_fleet_watchdog(self) -> bool:
        """On the controller, scan managed machines via SSH instead of local Docker."""
        if os.environ.get("HOMELAB_NODE") or os.environ.get("HOMELAB_WATCHDOG_LOCAL"):
            return False
        inventory = self.root / "automation" / "ansible" / "inventory" / "hosts.yml"
        try:
            from toolkit.core.ansible.ansible_ssh import resolve_tool

            ansible_ok = resolve_tool("ansible", self.root) is not None
        except OSError:
            ansible_ok = False
        return (
            self.config.is_multi_node and self.config.proxmox.provision_machines and inventory.is_file() and ansible_ok
        )

    def _get_fleet_containers(self) -> list[dict]:
        from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

        containers: list[dict] = []
        self._fleet_scan_errors.clear()
        for vm in self.config.enabled_nodes:
            ip = self.config.node_ip(vm)
            rc, out, _ = ssh_run_on_vm(
                self.config,
                ip,
                'docker ps -a --format "{{json .}}"',
                root=self.root,
                timeout=90,
            )
            if rc != 0:
                self._fleet_scan_errors.add(vm)
                continue
            parsed = 0
            for line in out.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                    entry["FleetVM"] = vm
                    containers.append(entry)
                    parsed += 1
                except json.JSONDecodeError:
                    continue
            if out.strip() and parsed == 0:
                self._fleet_scan_errors.add(vm)
        return containers

    def _get_containers(self) -> list[dict]:
        """Get all containers via docker ps (local or fleet LXCs)."""
        if self._container_snapshot is not None:
            return list(self._container_snapshot)
        if self._use_fleet_watchdog():
            self._container_snapshot = self._get_fleet_containers()
            return list(self._container_snapshot)
        try:
            result = self._run(["docker", "ps", "-a", "--format", "{{json .}}"])
            if result.returncode != 0:
                return []
            containers = []
            for line in result.stdout.strip().splitlines():
                if line:
                    containers.append(json.loads(line))
            self._container_snapshot = containers
            return list(containers)
        except (subprocess.TimeoutExpired, FileNotFoundError, json.JSONDecodeError):
            return []

    def _get_container_logs(self, name: str, tail: int = MAX_LOG_TAIL, *, node: str = "") -> str:
        """Get recent logs from a container for diagnosis."""
        try:
            result = self._docker_capture(
                ["logs", "--tail", str(tail), name],
                service=name,
                node=node,
                timeout=10,
            )
            # Combine stdout+stderr (many apps log to stderr)
            return (result.stdout + result.stderr).strip()
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return ""

    def _get_running_names(self) -> set[str]:
        """Get names of running containers."""
        if self._container_snapshot is not None:
            return {
                str(container.get("Names") or "").lstrip("/")
                for container in self._container_snapshot
                if container.get("State") == "running"
            }
        if self._use_fleet_watchdog():
            return {(c.get("Names") or "").lstrip("/") for c in self._get_containers() if c.get("State") == "running"}
        try:
            result = self._run(["docker", "ps", "--format", "{{.Names}}"])
            if result.returncode == 0:
                return set(result.stdout.strip().splitlines())
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        return set()

    def _container_category(self, name: str) -> str:
        """Map container name to category."""
        from toolkit.core.manifest.catalog import load_service_catalog

        for manifest in load_service_catalog().manifests:
            if name == manifest.name:
                return manifest.category
        projects = getattr(getattr(self.config, "projects", None), "entries", ())
        if any(project.subdomain == name for project in projects):
            return "projects"
        return "unknown"

    # ── Diagnosis helpers ──────────────────────────────────────

    def diagnose(self, name: str, state: str, *, node: str = "") -> str:
        """Analyze why a container is unhealthy/exited and suggest fixes."""
        parts: list[str] = []

        # Check logs for common errors
        logs = self._get_container_logs(name, node=node)
        if logs:
            lower = logs.lower()
            if "connection refused" in lower or "econnrefused" in lower:
                parts.append("Connection refused — a dependency may be down")
            if "permission denied" in lower:
                parts.append("Permission denied — check volume ownership (PUID/PGID)")
            if "out of memory" in lower or "oom" in lower:
                parts.append("Out of memory — increase container mem_limit")
            if "address already in use" in lower:
                parts.append("Port conflict — another process is using the same port")
            if "no such file" in lower or "not found" in lower:
                parts.append("Missing file/config — check volume mounts")
            if "authentication failed" in lower or "password" in lower:
                parts.append("Auth failure — check credentials in secrets")

        # Check dependencies
        deps = self._dependency_links.get(name, [])
        if deps:
            running = self._get_running_names()
            missing = [d for d in deps if d not in running]
            if missing:
                parts.append(f"Dependencies not running: {', '.join(missing)}")

        return "; ".join(parts) if parts else ""

    def check_port_conflicts(self) -> list[HealthIssue]:
        """Check for port conflicts between containers."""
        issues: list[HealthIssue] = []
        port_map: dict[tuple[str, str], set[str]] = {}
        for container in self._get_containers():
            name = str(container.get("Names") or "").lstrip("/")
            node = str(container.get("FleetVM") or "")
            for binding in str(container.get("Ports") or "").split(","):
                host_part = binding.strip().split("->", 1)[0]
                if "->" not in binding or ":" not in host_part:
                    continue
                port = host_part.rsplit(":", 1)[-1]
                # Docker reports separate IPv4 and IPv6 bindings for the same
                # published port. A single container owning both is valid.
                port_map.setdefault((node, port), set()).add(name)
        for (node, port), names in sorted(port_map.items()):
            if len(names) > 1:
                issues.append(
                    HealthIssue(
                        service="port-conflict",
                        category="system",
                        severity="warning",
                        message=f"Port {port} bound by multiple containers: {', '.join(sorted(names))}",
                        node=node,
                    )
                )
        return issues

    def check_project_endpoints(self) -> list[HealthIssue]:
        """Probe opt-in project health paths and mark failures restartable."""
        issues: list[HealthIssue] = []
        running = self._get_running_names()
        local_role = os.environ.get("HOMELAB_NODE", "")
        for project in getattr(getattr(self.config, "projects", None), "entries", ()):
            if not project.health_endpoint or project.subdomain not in running:
                continue
            node = project_node(self.config, project)
            if local_role and self.config.is_multi_node and node != local_role:
                continue
            url = f"http://{self.config.node_ip(node)}:{project.container_port}{project.health_endpoint}"
            command = ["curl", "-fsS", "--max-time", "5", "-o", "/dev/null", url]
            try:
                if self._use_fleet_watchdog():
                    import shlex

                    code, _out, _err = self._fleet_docker(node, shlex.join(command), timeout=15)
                else:
                    code = self._run(command, timeout=15).returncode
            except (OSError, subprocess.TimeoutExpired):
                code = 1
            if code != 0:
                issues.append(
                    HealthIssue(
                        service=project.subdomain,
                        category="projects",
                        severity="warning",
                        message=f"Health endpoint failed: {project.health_endpoint}",
                        auto_fixable=True,
                        node=node if self.config.is_multi_node else "",
                    )
                )
        return issues

    # ── Health checks ──────────────────────────────────────────

    def check_all(self) -> WatchdogReport:
        """Run a full health scan of all containers."""
        report = WatchdogReport()
        containers = self._get_containers()

        for node in sorted(self._fleet_scan_errors):
            report.issues.append(
                HealthIssue(
                    service="docker",
                    category="watchdog-infra",
                    severity="infra",
                    message="Cannot collect Docker inventory from configured node",
                    diagnosis="Check node reachability, SSH authentication, and Docker service state.",
                    node=node,
                )
            )

        if not containers and not self._fleet_scan_errors:
            # "0 containers" is an *actionable infra state*, not a per-cycle
            # page-able disaster. On the dev controller, when the systemd timer
            # started without the venv on PATH, or during fleet SSH blips, the
            # old 'critical' severity here flipped report.ok=False and paged
            # every 5 min for ~2 days (the Jun 23 alert storm). The 'infra'
            # severity participates in the long-cooldown channel instead and
            # is reported in the UI/audit as "can't reach containers", not
            # conflated with a container-down critical.
            report.issues.append(
                HealthIssue(
                    service="docker",
                    category="watchdog-infra",
                    severity=check_all_report_kind(),
                    message="Cannot reach Docker daemon or no containers running",
                    diagnosis="Check Docker / venv PATH / fleet SSH. This is reported, not paged per-cycle.",
                )
            )
            return report
        if not containers:
            return report

        for c in containers:
            name = c.get("Names", "")
            state = c.get("State", "")
            status = c.get("Status", "")
            node = str(c.get("FleetVM") or "")

            if state == "running":
                if "unhealthy" in status.lower():
                    is_safe = name in self._safe_restart_names
                    diag = self.diagnose(name, state, node=node)
                    report.issues.append(
                        HealthIssue(
                            service=name,
                            category=self._container_category(name),
                            severity="warning",
                            message=f"Container unhealthy: {status}",
                            auto_fixable=is_safe,
                            diagnosis=diag,
                            node=node,
                        )
                    )
                else:
                    report.healthy.append(ContainerHealth(name=name, node=node))
            elif state == "exited":
                # Successful manifest-declared one-shot runtimes are expected
                # to remain exited after initialization. A non-zero exit still
                # reports as a critical failure below.
                if "Exited (0)" in status:
                    try:
                        from toolkit.core.config.service_metadata import get_service_runtime_mode

                        if get_service_runtime_mode(name) == "oneshot":
                            report.healthy.append(ContainerHealth(name=name, node=node))
                            continue
                    except (OSError, ValueError, TypeError):
                        logger.exception("failed to load runtime mode for %s", name)
                diag = self.diagnose(name, state, node=node)
                report.issues.append(
                    HealthIssue(
                        service=name,
                        category=self._container_category(name),
                        severity="critical",
                        message=f"Container exited: {status}",
                        auto_fixable=name in self._safe_restart_names,
                        diagnosis=diag,
                        node=node,
                    )
                )
            elif state == "restarting":
                diag = self.diagnose(name, state, node=node)
                report.issues.append(
                    HealthIssue(
                        service=name,
                        category=self._container_category(name),
                        severity="warning",
                        message="Container in restart loop",
                        auto_fixable=name in self._safe_restart_names,
                        diagnosis=diag,
                        node=node,
                    )
                )

        return report

    def check_disk_space(self, threshold_pct: float = 90.0) -> list[HealthIssue]:
        """Check disk usage on key paths."""
        issues: list[HealthIssue] = []
        for path, label in [("/", "Root filesystem"), ("/var/lib/docker", "Docker data")]:
            try:
                usage = shutil.disk_usage(path)
                pct = (usage.used / usage.total) * 100
                if pct >= threshold_pct:
                    issues.append(
                        HealthIssue(
                            service="disk",
                            category="system",
                            severity="critical" if pct >= 95 else "warning",
                            message=f"{label} ({path}): {pct:.1f}% used ({usage.free // (1024**3)}GB free)",
                            node=self._local_node,
                        )
                    )
            except OSError:
                pass
        return issues

    def check_memory(self, threshold_pct: float = 90.0) -> list[HealthIssue]:
        """Check system memory usage."""
        issues: list[HealthIssue] = []
        try:
            with open("/proc/meminfo", encoding="utf-8") as f:
                info = {}
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        info[parts[0].rstrip(":")] = int(parts[1])

            total = info.get("MemTotal", 0)
            available = info.get("MemAvailable", 0)
            if total > 0:
                used_pct = ((total - available) / total) * 100
                if used_pct >= threshold_pct:
                    free_mb = available // 1024
                    issues.append(
                        HealthIssue(
                            service="memory",
                            category="system",
                            severity="critical" if used_pct >= 95 else "warning",
                            message=f"Memory: {used_pct:.1f}% used ({free_mb}MB free)",
                            node=self._local_node,
                        )
                    )
        except OSError:
            pass
        return issues

    def check_restart_loops(self) -> list[HealthIssue]:
        """Detect containers stuck in restart loops via Docker inspect."""
        issues: list[HealthIssue] = []
        for container in self._get_containers():
            name = str(container.get("Names") or "").lstrip("/")
            status = str(container.get("Status") or "")
            state = str(container.get("State") or "")
            node = str(container.get("FleetVM") or "")
            if not name or "restarting" not in f"{state} {status}".lower():
                continue
            try:
                inspected = self._docker_capture(
                    ["inspect", "--format", "{{.RestartCount}}", name],
                    service=name,
                    node=node,
                    timeout=10,
                )
                restart_count = int(inspected.stdout.strip()) if inspected.returncode == 0 else 0
            except (OSError, ValueError, subprocess.SubprocessError):
                restart_count = 0
            if restart_count >= 5:
                issues.append(
                    HealthIssue(
                        service=name,
                        category=self._container_category(name),
                        severity="critical",
                        message=f"Restart loop detected ({restart_count} restarts)",
                        auto_fixable=False,
                        diagnosis="Container keeps crashing — check logs and config",
                        node=node,
                    )
                )
        return issues

    def check_dependency_connectivity(self) -> list[HealthIssue]:
        """Check network connectivity between containers and their dependencies.

        Consumers are derived from the discovered dependency graph and probe
        ports are owned by dependency service manifests. Dependencies without
        an explicit TCP probe contract are covered by container health only.
        """
        issues: list[HealthIssue] = []
        running = self._get_running_names()
        if not running:
            return issues

        from toolkit.core.config.service_metadata import service_endpoint_ports

        probe_ports = service_endpoint_ports()

        reverse = self._reverse_dep_links
        for dep_name, consumers in reverse.items():
            port = probe_ports.get(dep_name)
            if port is None:
                continue
            active_consumers = [consumer for consumer in consumers if consumer in running]
            if not active_consumers:
                continue
            if dep_name not in running:
                issues.append(
                    HealthIssue(
                        service=dep_name,
                        category=self._container_category(dep_name),
                        severity="critical",
                        message=f"Dependency {dep_name} is not running",
                        auto_fixable=dep_name in self._safe_restart_names,
                        diagnosis="Downstream services will fail until this is restored",
                        node=self._local_node,
                    )
                )
                continue
            for consumer in active_consumers:
                try:
                    # Try multiple connectivity test methods since containers vary
                    # (Alpine lacks bash, some lack wget/curl)
                    cmds = [
                        ["docker", "exec", consumer, "bash", "-c", f"cat < /dev/null > /dev/tcp/{dep_name}/{port}"],
                        [
                            "docker",
                            "exec",
                            consumer,
                            "sh",
                            "-c",
                            f"nc -z -w2 {dep_name} {port} 2>/dev/null || "
                            f"wget -q --spider --timeout=2 http://{dep_name}:{port} 2>/dev/null || "
                            f"echo fail",
                        ],
                    ]
                    reachable = False
                    for cmd in cmds:
                        result = self._run(cmd, timeout=10)
                        if result.returncode == 0 and "fail" not in result.stdout:
                            reachable = True
                            break
                    if not reachable:
                        issues.append(
                            HealthIssue(
                                service=consumer,
                                category=self._container_category(consumer),
                                severity="warning",
                                message=f"Cannot reach {dep_name}:{port} from {consumer}",
                                diagnosis=(
                                    f"Network connectivity issue — {dep_name} may not be on the same Docker network"
                                ),
                                node=self._local_node,
                            )
                        )
                except (subprocess.TimeoutExpired, FileNotFoundError):
                    pass
        return issues

    def check_config_files(self) -> list[HealthIssue]:
        """Validate that critical generated config files exist."""
        issues: list[HealthIssue] = []
        generated = self.root / "generated"
        if not generated.exists():
            issues.append(
                HealthIssue(
                    service="config",
                    category="system",
                    severity="warning",
                    message="No generated/ directory — run config generation first",
                    auto_fixable=False,
                )
            )
            return issues

        critical_files = [
            ("Caddyfile", "Caddy reverse proxy config"),
        ]
        if self.config.category_enabled("management"):
            critical_files.append(("authelia.yml", "Authelia SSO config"))

        for filename, desc in critical_files:
            path = generated / filename
            if not path.exists():
                issues.append(
                    HealthIssue(
                        service="config",
                        category="system",
                        severity="warning",
                        message=f"Missing {desc}: {filename}",
                        auto_fixable=False,
                        diagnosis="Run 'homelab-toolkit generate' to create config files",
                    )
                )
            elif path.stat().st_size == 0:
                issues.append(
                    HealthIssue(
                        service="config",
                        category="system",
                        severity="warning",
                        message=f"Empty {desc}: {filename}",
                        auto_fixable=False,
                        diagnosis="Config file is empty — regenerate configs",
                    )
                )
        return issues

    def check_dns_resolution(self) -> list[HealthIssue]:
        """Check that the domain resolves correctly (basic external connectivity)."""
        issues: list[HealthIssue] = []
        domain = self.config.domain
        if not domain or domain == "localhost":
            return issues

        try:
            socket.getaddrinfo(domain, 443, socket.AF_INET, socket.SOCK_STREAM)
        except socket.gaierror:
            issues.append(
                HealthIssue(
                    service="dns",
                    category="system",
                    severity="warning",
                    message=f"Cannot resolve domain: {domain}",
                    diagnosis="DNS not configured or propagated yet — check Cloudflare/DNS settings",
                )
            )
        except OSError:
            pass
        return issues

    def check_container_resources(self) -> list[HealthIssue]:
        """Check CPU/memory usage per container — flags runaway resource consumers."""
        issues: list[HealthIssue] = []
        for stats in self._get_container_stats():
            name = stats["name"]
            node = stats.get("node", "")
            try:
                cpu_pct = float(stats["cpu"].rstrip("%"))
                mem_pct = float(stats["mem_pct"].rstrip("%"))
            except ValueError:
                continue
            if cpu_pct > 90:
                issues.append(
                    HealthIssue(
                        service=name,
                        category=self._container_category(name),
                        severity="warning",
                        message=f"High CPU: {cpu_pct:.0f}%",
                        diagnosis=(
                            "Container is consuming excessive CPU — may indicate "
                            "a bug, infinite loop, or resource-intensive task"
                        ),
                        node=node,
                    )
                )
            if mem_pct > 90:
                issues.append(
                    HealthIssue(
                        service=name,
                        category=self._container_category(name),
                        severity="warning",
                        message=f"High memory: {mem_pct:.0f}% ({stats['mem_usage']})",
                        diagnosis="Container is near its memory limit — consider increasing its declared resources",
                        node=node,
                    )
                )
        return issues

    def check_volume_permissions(self) -> list[HealthIssue]:
        """Check manifest-managed bind mounts against declared ownership."""
        issues: list[HealthIssue] = []
        node = self._local_node
        if self.config.is_multi_node and not node:
            return issues
        if not node:
            nodes = getattr(self.config, "enabled_nodes", ())
            if not nodes:
                return issues
            node = nodes[0]
        try:
            from toolkit.core.manifest.catalog import ManifestCatalogError
            from toolkit.core.manifest.storage import StorageInventoryError, compile_storage_inventory

            inventory = compile_storage_inventory(self.config, self.root, roles={node})
        except (ManifestCatalogError, OSError, StorageInventoryError, ValueError):
            return issues
        for asset in inventory.assets:
            if asset.host_path is None or not asset.manage_permissions or not asset.host_path.exists():
                continue
            try:
                stat = asset.host_path.stat()
            except OSError:
                continue
            if (stat.st_uid, stat.st_gid) == (asset.host_uid, asset.host_gid):
                continue
            issues.append(
                HealthIssue(
                    service=asset.service,
                    category="system",
                    severity="warning",
                    message=(
                        f"Volume {asset.name} is owned by {stat.st_uid}:{stat.st_gid}; "
                        f"expected {asset.host_uid}:{asset.host_gid}"
                    ),
                    diagnosis="Run the node permission reconciliation and inspect the service journal.",
                    node=node,
                )
            )
        return issues

    def check_ssl_certificates(self) -> list[HealthIssue]:
        """Check SSL certificate expiry for the configured domain."""
        from toolkit.core.manifest.catalog import provider_service_name
        from toolkit.core.ops.system_checks import check_cert_days_left

        issues: list[HealthIssue] = []
        domain = getattr(self.config, "domain", "")
        if not domain or domain in ("localhost", "example.com"):
            return issues
        days_left = check_cert_days_left(domain, port=443)
        if days_left is None:
            return issues
        ingress_service = provider_service_name("ingress")
        if days_left < 0:
            issues.append(
                HealthIssue(
                    service=ingress_service,
                    category="management",
                    severity="critical",
                    message=f"SSL certificate for {domain} expired {-days_left} days ago",
                    diagnosis="Certificate has expired — HTTPS connections will fail. "
                    "Check ingress logs and ensure the ACME challenge is working.",
                )
            )
        elif days_left < 7:
            issues.append(
                HealthIssue(
                    service=ingress_service,
                    category="management",
                    severity="warning",
                    message=f"SSL certificate for {domain} expires in {days_left} days",
                    diagnosis="Certificate is expiring soon — the ingress provider should auto-renew it. "
                    "Verify ingress is running and the DNS challenge is configured.",
                )
            )
        elif days_left < 14:
            issues.append(
                HealthIssue(
                    service=ingress_service,
                    category="management",
                    severity="info",
                    message=f"SSL certificate for {domain} expires in {days_left} days",
                    diagnosis="Certificate renewal should happen automatically through the ingress provider.",
                )
            )
        return issues

    def check_docker_log_sizes(self) -> list[HealthIssue]:
        """Check for containers with excessively large log files (>100MB)."""
        from toolkit.core.ops.system_checks import collect_large_container_logs

        threshold_mb = 100
        issues: list[HealthIssue] = []
        nodes = self.config.enabled_nodes if self._use_fleet_watchdog() else [self._local_node]
        for node in nodes:

            def run_on_node(
                command: list[str],
                timeout: int,
                target: str = node,
            ) -> subprocess.CompletedProcess:
                return self._node_capture(target, command, timeout=timeout)

            try:
                large_logs = collect_large_container_logs(
                    threshold_mb=threshold_mb,
                    run=run_on_node,
                )
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue
            for info in large_logs:
                issues.append(
                    HealthIssue(
                        service=info.name,
                        category=self._container_category(info.name),
                        severity="warning",
                        message=f"Container log is {info.size_mb}MB (>{threshold_mb}MB threshold)",
                        diagnosis=(
                            "Large log file consuming disk space. "
                            "Check Docker daemon log driver config — "
                            "ensure max-size and max-file are set in compose."
                        ),
                        node=node,
                    )
                )
        return issues

    def check_image_updates(self) -> list[HealthIssue]:
        """Check for containers running images created more than 90 days ago."""
        from toolkit.core.ops.system_checks import collect_stale_images

        issues: list[HealthIssue] = []
        try:
            stale = collect_stale_images(
                run=lambda cmd, timeout: self._run(cmd, timeout=timeout),
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            return issues
        for item in stale:
            issues.append(
                HealthIssue(
                    service=item.name,
                    category=self._container_category(item.name),
                    severity="info",
                    message=f"Image {item.age_days} days old ({item.image})",
                    diagnosis=("Image is over 90 days old — consider pulling latest version for security patches"),
                )
            )
        return issues

    def _get_container_uptimes(self) -> dict[tuple[str, str], float]:
        """Get uptime in seconds for each running container."""
        uptimes: dict[tuple[str, str], float] = {}
        for container in self._get_containers():
            if str(container.get("State") or "") != "running":
                continue
            name = str(container.get("Names") or "").lstrip("/")
            node = str(container.get("FleetVM") or "")
            seconds = _parse_docker_uptime(str(container.get("Status") or ""))
            if name and seconds > 0:
                uptimes[(name, node)] = seconds
        return uptimes

    def _get_container_stats(self) -> list[dict[str, str]]:
        """Get per-container CPU/memory stats from docker stats."""
        stats: list[dict[str, str]] = []
        command = [
            "docker",
            "stats",
            "--no-stream",
            "--format",
            "{{.Name}}\t{{.CPUPerc}}\t{{.MemPerc}}\t{{.MemUsage}}\t{{.NetIO}}\t{{.BlockIO}}",
        ]
        results: list[tuple[str, subprocess.CompletedProcess]] = []
        try:
            if self._use_fleet_watchdog():
                import shlex

                for node in self.config.enabled_nodes:
                    rc, out, err = self._fleet_docker(node, shlex.join(command), timeout=30)
                    results.append((node, subprocess.CompletedProcess(command, rc, out, err)))
            else:
                results.append(("", self._run(command, timeout=30)))
        except (OSError, subprocess.SubprocessError):
            return stats
        for node, result in results:
            if result.returncode != 0:
                continue
            for line in result.stdout.strip().splitlines():
                parts = line.split("\t")
                if len(parts) < 4:
                    continue
                stats.append(
                    {
                        "name": parts[0],
                        "cpu": parts[1],
                        "mem_pct": parts[2],
                        "mem_usage": parts[3],
                        "net_io": parts[4] if len(parts) > 4 else "",
                        "block_io": parts[5] if len(parts) > 5 else "",
                        "node": node,
                    }
                )
        return stats

    def verify_post_restart(self, name: str, timeout: int = 30, *, node: str = "") -> bool:
        """Wait for a container to become healthy after restart."""
        interval = 5
        elapsed = 0
        while elapsed < timeout:
            time.sleep(interval)
            elapsed += interval
            try:
                result = self._docker_capture(
                    ["inspect", "--format", "{{.State.Status}}:{{.State.Health.Status}}", name],
                    service=name,
                    node=node,
                    timeout=10,
                )
                if result.returncode != 0:
                    continue
                output = result.stdout.strip()
                # Containers without healthcheck report format "running:<no value>"
                if "running" in output and "unhealthy" not in output:
                    return True
            except (subprocess.TimeoutExpired, FileNotFoundError):
                continue
        return False

    def _verify_cascade_consumers(self, dependency: str, *, grace_s: int = 30) -> list[HealthIssue]:
        """F3: after a dependency restarts, re-verify downstream consumers.

        Walks :pyattr:`_reverse_dep_links` for ``dependency`` and calls
        :func:`verify_post_restart` (abbreviated, 15s) on each consumer after a
        grace period. Returns a ``HealthIssue`` per consumer that came back
        unhealthy (callers log + decide whether to escalate; the generic heal
        loop will pick them up on the next cycle).

        ``time.sleep`` is patched to a no-op under the autouse test stub
        (conftest.py:54), so the grace period collapses in tests.
        """
        consumers = self._reverse_dep_links.get(dependency, [])
        if not consumers:
            return []
        running = self._get_running_names()
        issues: list[HealthIssue] = []
        if grace_s > 0:
            time.sleep(grace_s)
        from toolkit.core.watchdog.models import HealthIssue

        for consumer in consumers:
            # Only probe consumers that are actually running — a stopped
            # consumer is a separate issue, not a cascade side-effect.
            if consumer not in running:
                continue
            if not self.verify_post_restart(consumer, timeout=15):
                issues.append(
                    HealthIssue(
                        service=consumer,
                        category=dependency,
                        severity="warning",
                        message=f"unhealthy after {dependency} restart (cascade)",
                        auto_fixable=True,
                    )
                )
        return issues

    def _maybe_auto_trigger_recover(self, report, logs: list[str]) -> tuple[int, int, int]:
        """Run guardrailed, non-destructive recovery for affected nodes."""
        import time as _time

        from toolkit.core.manifest.settings import service_setting_bool, service_setting_int
        from toolkit.core.ops.watchdog.recover_policy import (
            RecoverAutoConfig,
            RecoverSignal,
            recover_decisions,
        )
        from toolkit.core.state.audit_log import AuditAction, audit

        now = _time.time()
        attempted = 0
        succeeded = 0
        failed = 0
        policy = RecoverAutoConfig()
        if hasattr(self.config, "service_settings"):
            policy = RecoverAutoConfig(
                enabled=service_setting_bool(self.config, "homelab-ui", "auto-recover"),
                cooldown_seconds=service_setting_int(self.config, "homelab-ui", "recover-cooldown-minutes") * 60,
                terminal_threshold=service_setting_int(self.config, "homelab-ui", "recover-terminal-threshold"),
                multi_failure_min=service_setting_int(self.config, "homelab-ui", "recover-critical-threshold"),
            )
        vm_for_service: dict[str, str] = {}
        try:
            from toolkit.core.manifest.catalog import load_service_catalog
            from toolkit.core.manifest.placement import service_node_map

            vm_for_service.update(service_node_map(self.config, load_service_catalog()))
        except Exception as exc:
            logger.warning("watchdog recovery placement could not be resolved", exc_info=exc)
            return attempted, succeeded, failed
        for project in getattr(getattr(self.config, "projects", None), "entries", ()):
            vm_for_service[project.subdomain] = project_node(self.config, project)

        decisions = recover_decisions(
            (
                RecoverSignal(
                    service=issue.service,
                    severity=issue.severity,
                    terminal=issue.terminal,
                    restart_count=self._restart_counts.get(
                        _container_state_key(issue.service, issue.node),
                        0,
                    ),
                )
                for issue in report.issues
            ),
            vm_for_service=vm_for_service,
            last_recover_at=self._last_auto_recover_at,
            now=now,
            cfg=policy,
        )
        for vm, decision in decisions.items():
            if not decision.trigger:
                continue
            attempted += 1
            logs.append(f"HEAL auto-recover: triggering deploy recover --node {vm} ({decision.reason})")
            self._last_auto_recover_at[vm] = now
            self._save_restart_state()
            try:
                result = self._run(
                    [
                        sys.executable,
                        "-m",
                        "toolkit.cli",
                        "--root",
                        str(self.root),
                        "deploy",
                        "recover",
                        "--node",
                        vm,
                        "-y",
                    ],
                    timeout=600,
                )
                audit(
                    self.root,
                    AuditAction.WATCHDOG,
                    actor="watchdog-auto-recover",
                    ok=(result.returncode == 0),
                    detail=f"auto-triggered deploy recover --node {vm} ({decision.reason})",
                    vm=vm,
                )
                if result.returncode == 0:
                    succeeded += 1
                    logs.append(f"HEAL auto-recover: {vm} recover completed")
                else:
                    failed += 1
                    logs.append(f"HEAL auto-recover: {vm} recover failed (exit {result.returncode})")
            except Exception as exc:
                failed += 1
                logs.append(f"HEAL auto-recover: {vm} error: {exc}")
                audit(
                    self.root,
                    AuditAction.WATCHDOG,
                    actor="watchdog-auto-recover",
                    ok=False,
                    detail=f"deploy recover --node {vm} raised {type(exc).__name__}",
                    vm=vm,
                )
        return attempted, succeeded, failed

    def check_hook_failures(self) -> list[HealthIssue]:
        """G71: check the last deploy hooks report for failures.

        Reads ``.homelab-state/last-hooks.json`` (written by the
        deploy workflow). If any VM has ``passed: false``, emit a pageable
        issue. Hook failures are intentionally not directly auto-fixable:
        ``deploy recover`` is a whole-VM operation and must go through the
        broader smart-recover policy/cooldown or explicit operator action.
        """
        import json

        from toolkit.core.state.paths import last_hooks_path

        issues: list[HealthIssue] = []
        path = last_hooks_path(self.root)
        if not path.is_file():
            return issues
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return issues
        for vm, audit in data.items():
            if not isinstance(audit, dict):
                continue
            if audit.get("passed"):
                continue
            critical = audit.get("critical", 0)
            warning = audit.get("warning", 0)
            issues.append(
                HealthIssue(
                    service="hooks",
                    category="deploy",
                    severity="critical" if critical else "warning",
                    message=f"{vm} hooks failed ({critical} critical, {warning} warning)",
                    auto_fixable=False,
                    diagnosis=f"Run: homelab-toolkit deploy recover --node {vm}",
                )
            )
        return issues

    def check_backup_freshness(self) -> list[HealthIssue]:
        """Check central Kopia freshness once on its owning node or controller."""
        if not self.config.backups.enabled:
            self._backup_nodes = ()
            return []
        local_role = os.environ.get("HOMELAB_NODE", "")
        from toolkit.core.manifest.placement import service_node

        if local_role and local_role != service_node(self.config, "kopia"):
            self._backup_nodes = ()
            return []

        from toolkit.core.ops.backup_inventory import read_backup_inventory

        inventory = read_backup_inventory(self.config, self.root)
        self._backup_nodes = inventory.nodes
        if inventory.error:
            return [
                HealthIssue(
                    service="backup-repository",
                    category="backups",
                    severity="infra",
                    message="Backup inventory is unavailable",
                    diagnosis=inventory.error,
                )
            ]

        issues: list[HealthIssue] = []
        for node in inventory.nodes:
            if node.ok:
                continue
            if node.status == "stale":
                age = f"{node.age_hours:.1f} hours" if node.age_hours is not None else "an unknown duration"
                message = "Latest snapshot is stale"
                diagnosis = f"The {node.role} snapshot is {age} old. Check kopia-snapshot.timer and its journal."
            else:
                message = "No snapshot has been recorded"
                diagnosis = f"Check kopia-snapshot.timer and the Kopia agent on {node.role}."
            issues.append(
                HealthIssue(
                    service=f"backup-{node.role}",
                    category="backups",
                    severity="critical",
                    message=message,
                    auto_fixable=False,
                    diagnosis=diagnosis,
                )
            )
        return issues

    def check_backup_restore_drill(self) -> list[HealthIssue]:
        """Require recent proof that snapshot content can be restored."""
        if not self.config.backups.enabled:
            self._backup_drill_evidence = None
            return []
        local_role = os.environ.get("HOMELAB_NODE", "")
        from toolkit.core.manifest.placement import service_node

        if local_role and local_role != service_node(self.config, "kopia"):
            self._backup_drill_evidence = None
            return []

        from toolkit.core.ops.backup_restore_drill import read_backup_drill_evidence

        evidence = read_backup_drill_evidence(self.root)
        self._backup_drill_evidence = evidence
        if evidence is None:
            return [
                HealthIssue(
                    service="backup-restore-drill",
                    category="backups",
                    severity="warning",
                    message="Backup content has not been restore-verified",
                    auto_fixable=False,
                    diagnosis="Check kopia-restore-drill.timer and its journal.",
                )
            ]
        if not evidence.ok:
            return [
                HealthIssue(
                    service="backup-restore-drill",
                    category="backups",
                    severity="critical",
                    message="The latest backup content drill failed",
                    auto_fixable=False,
                    diagnosis="Inspect kopia-restore-drill.service and rerun the bounded drill.",
                )
            ]
        age_hours = max(0.0, (time.time() - evidence.checked_at.timestamp()) / 3_600)
        if age_hours > 8 * 24:
            return [
                HealthIssue(
                    service="backup-restore-drill",
                    category="backups",
                    severity="critical",
                    message="Backup content verification is overdue",
                    auto_fixable=False,
                    diagnosis=f"The latest successful drill is {age_hours / 24:.1f} days old.",
                )
            ]
        return []

    def full_check(self) -> WatchdogReport:
        """Run all health checks including system resources, ports, config, DNS, and per-container stats."""
        self._container_snapshot = None
        fleet = self._use_fleet_watchdog()
        report = self.check_all()
        report.issues.extend(self.check_disk_space())
        report.issues.extend(self.check_memory())
        report.issues.extend(self.check_port_conflicts())
        report.issues.extend(self.check_project_endpoints())
        report.issues.extend(self.check_restart_loops())
        report.issues.extend(self.check_config_files())
        report.issues.extend(self.check_dns_resolution())
        if not fleet:
            report.issues.extend(self.check_dependency_connectivity())
        report.issues.extend(self.check_container_resources())
        report.issues.extend(self.check_volume_permissions())
        if not fleet:
            report.issues.extend(self.check_image_updates())
        report.issues.extend(self.check_ssl_certificates())
        report.issues.extend(self.check_docker_log_sizes())
        report.issues.extend(self.check_hook_failures())
        report.issues.extend(self.check_backup_freshness())
        report.issues.extend(self.check_backup_restore_drill())
        self._log_event("check", "system", report.summary())
        return report

    # ── Healing ────────────────────────────────────────────────

    def heal(self, report: WatchdogReport) -> HealResult:
        return self._heal(
            report,
            allow_broad_recovery=True,
            allow_dependency_actions=True,
            allow_structured_heal=True,
        )

    def heal_targeted(self, report: WatchdogReport, *, service: str) -> HealResult:
        """Heal one safe service without permitting VM recovery or unrelated remedies."""
        if service not in self.restartable_services():
            raise ValueError("targeted service is not safe for unattended restart")
        if any(issue.service != service for issue in report.issues):
            raise ValueError("targeted heal report contains an unrelated service")
        return self._heal(
            report,
            allow_broad_recovery=False,
            allow_dependency_actions=False,
            allow_structured_heal=False,
        )

    def _heal(
        self,
        report: WatchdogReport,
        *,
        allow_broad_recovery: bool,
        allow_dependency_actions: bool,
        allow_structured_heal: bool,
    ) -> HealResult:
        """Attempt safe auto-recovery for fixable issues with backoff and health verification.

        Every attempted remedy is classified as verified success or failure.
        Backoff and exhausted restart budgets are reported separately as
        deferred work so no caller has to infer outcomes from log text.
        """
        from toolkit.core.deploy.operation_lease import OperationLease

        lease = OperationLease.inspect(self.root)
        if lease.active:
            operation = lease.snapshot.operation if lease.snapshot is not None else "platform"
            deferred = sum(1 for issue in report.issues if issue.auto_fixable)
            return HealResult(
                logs=[f"DEFER watchdog healing: {operation} operation is active"],
                deferred=deferred,
            )

        logs: list[str] = []
        attempted = 0
        succeeded = 0
        failed = 0
        deferred = 0
        max_restarts_per_service = 3  # avoid restart loops

        if any(i.service == "disk" for i in report.issues):
            logs.append(f"HEAL disk: {self.prune_docker()}")
            try:
                from toolkit.core.ops.maintenance import vacuum_journal

                logs.extend(vacuum_journal())
            except Exception as exc:
                logs.append(f"HEAL disk journal: {exc}")

        # Structured heals via service plugins (database reconciliation, etc.)
        from toolkit.services import heal_routing_map

        heal_routes = heal_routing_map()
        structured_heal = self.structured_heal_services() if allow_structured_heal else frozenset()
        heal_targets = sorted(
            {issue.service for issue in report.issues if issue.auto_fixable and issue.service in structured_heal}
        )
        for target in heal_targets:
            plugin = heal_routes.get(target)
            if plugin is None:
                continue
            attempted += 1
            try:
                lines = plugin.heal(self.config, self.root, service=target)
                if lines is None:
                    logs.append(f"HEAL {target}: plugin returned no heal steps")
                    failed += 1
                    continue
                logs.append(f"HEAL {target}: {plugin.service} plugin heal")
                logs.extend(lines)
                succeeded += 1
            except Exception as exc:
                failed += 1
                logs.append(f"HEAL {target} ERROR: {exc}")

        for issue in report.issues:
            if not issue.auto_fixable:
                logs.append(f"SKIP {issue.service}: {issue.message} (not auto-fixable)")
                continue

            if issue.service in structured_heal:
                continue

            # Guard against restart loops with exponential backoff
            state_key = _container_state_key(issue.service, issue.node)
            count = self._restart_counts.get(state_key, 0)
            if count >= max_restarts_per_service:
                deferred += 1
                logs.append(f"SKIP {issue.service}: already attempted {count} restarts — manual intervention needed")
                self._log_event("heal", issue.service, f"Skipped: {count} restart attempts exceeded limit")
                # Mark the issue terminal so notify() silences re-pages after the first
                issue.terminal = True
                continue

            # Exponential backoff: wait BASE * 2^count seconds since last restart
            last_ts = self._restart_timestamps.get(state_key, 0)
            backoff = RESTART_BACKOFF_BASE * (2**count)
            elapsed = time.time() - last_ts
            if last_ts > 0 and elapsed < backoff:
                deferred += 1
                remaining = int(backoff - elapsed)
                logs.append(
                    f"BACKOFF {issue.service}: waiting {remaining}s before retry "
                    f"(attempt {count + 1}/{max_restarts_per_service})"
                )
                continue

            # Check dependencies first — restart them if they're down
            deps = self._dependency_links.get(issue.service, [])
            if deps and allow_dependency_actions:
                running = self._get_running_names()
                for dep in deps:
                    if dep not in running:
                        if dep in structured_heal:
                            continue  # handled by plugin heal
                        logs.append(f"  DEP: starting dependency {dep} for {issue.service}...")
                        try:
                            rc, _out = self._docker_action(dep, "start", timeout=60)
                            if rc == 0:
                                logs.append(f"  DEP OK: {dep} started")
                                self._log_event("heal", dep, "Started as dependency")
                            else:
                                logs.append(f"  DEP FAIL: {dep}: {_out[:MAX_STDERR_LEN]}")
                        except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                            logs.append(f"  DEP ERROR: {dep}: {e}")

            logs.append(f"HEAL {issue.service}: attempting restart (attempt {count + 1}/{max_restarts_per_service})...")
            attempted += 1
            try:
                rc, out = self._docker_action(issue.service, "restart", node=issue.node, timeout=60)
                self._restart_counts[state_key] = count + 1
                self._restart_timestamps[state_key] = time.time()
                self._save_restart_state()
                if rc == 0:
                    # Verify container is healthy after restart
                    if self.verify_post_restart(issue.service, timeout=30, node=issue.node):
                        succeeded += 1
                        logs.append(f"  OK: {issue.service} restarted and verified healthy")
                        self._log_event("heal", issue.service, "Restarted and verified healthy")
                        # F3: health-aware cascading restarts — re-verify downstream
                        # consumers after the dependency came back healthy. A postgres
                        # restart leaves authelia/grafana/nextcloud in an unknown state;
                        # give them a brief grace period, then probe. time.sleep is a
                        # no-op under the autouse test stub (conftest.py:54).
                        cascade_issues = self._verify_cascade_consumers(issue.service)
                        if cascade_issues:
                            rev_names = ", ".join(c.service for c in cascade_issues)
                            logs.append(f"  CASCADE: {rev_names} unhealthy after {issue.service} restart")
                            for c in cascade_issues:
                                self._log_event("heal", c.service, f"Cascade-unhealthy after {issue.service} restart")
                    else:
                        failed += 1
                        logs.append(f"  WARN: {issue.service} restarted but health not confirmed within 30s")
                        self._log_event("heal", issue.service, "Restarted but health unconfirmed")
                else:
                    failed += 1
                    logs.append(f"  FAIL: {issue.service} restart failed: {out[:MAX_STDERR_LEN]}")
                    self._log_event("heal", issue.service, f"Restart failed: {out[:MAX_STDERR_LEN]}")
            except (subprocess.TimeoutExpired, FileNotFoundError) as e:
                failed += 1
                logs.append(f"  ERROR: {issue.service}: {e}")
                self._log_event("heal", issue.service, f"Error: {e}")

        # Broad recovery is a last resort. Evaluate it only after structured
        # heals, restart attempts, and exhausted restart budgets are reflected
        # in the current report and persisted state.
        if allow_broad_recovery:
            recover_attempted, recover_succeeded, recover_failed = self._maybe_auto_trigger_recover(report, logs)
            attempted += recover_attempted
            succeeded += recover_succeeded
            failed += recover_failed

        # Reset backoff counters for containers that are now healthy so a
        # transient issue doesn't permanently count against future restarts.
        # Only reset containers that were NOT just restarted in this heal()
        # call — a freshly-restarted container needs to survive the next
        # watchdog cycle before we trust it.
        if self._restart_counts:
            just_restarted = {
                _container_state_key(issue.service, issue.node) for issue in report.issues if issue.auto_fixable
            }
            running_healthy: set[str] = set()
            try:
                for state_key in list(self._restart_counts):
                    if state_key in just_restarted:
                        continue
                    name, node = _split_container_state_key(state_key)
                    inspected = self._docker_capture(
                        ["inspect", "--format", "{{.State.Status}}:{{.State.Health.Status}}", name],
                        service=name,
                        node=node,
                        timeout=10,
                    )
                    status = inspected.stdout.strip()
                    if inspected.returncode == 0 and "running" in status and "unhealthy" not in status:
                        running_healthy.add(state_key)
            except (OSError, subprocess.SubprocessError):
                pass
            for state_key in running_healthy:
                self._restart_counts.pop(state_key, None)
                self._restart_timestamps.pop(state_key, None)
            if running_healthy:
                self._save_restart_state()

        return HealResult(
            logs=logs,
            attempted=attempted,
            succeeded=succeeded,
            failed=failed,
            deferred=deferred,
        )

    def prune_docker(self) -> str:
        """Prune unused Docker resources (images/containers; never named volumes)."""
        from toolkit.core.ops.maintenance import run_docker_cleanup

        try:
            lines = run_docker_cleanup()
            msg = "; ".join(lines)[:400] if lines else "nothing pruned"
            self._log_event("prune", "docker", msg[:200])
            return msg
        except OSError as e:
            return f"Prune error: {e}"

    # ── Notifications ──────────────────────────────────────────

    def _send_ntfy(self, body: str, *, title: str, priority: str, tags: str) -> bool:
        """Post to ntfy. Isolated so tests can patch the network hop in isolation
        from the cooldown/dedup logic above it."""
        from toolkit.services.ntfy.client import NtfyClient, resolve_local_ntfy_base

        client = NtfyClient(resolve_local_ntfy_base())
        return client.send("watchdog", body, title=title, priority=priority, tags=tags)

    def _cooldown_for(self, issue: HealthIssue) -> int:
        if issue.category == "backups" or issue.severity == "infra":
            return NOTIFY_COOLDOWN_INFRA_S
        if issue.severity == "critical":
            return NOTIFY_COOLDOWN_CRITICAL_S
        if issue.severity == "warning":
            return NOTIFY_COOLDOWN_WARNING_S
        return NOTIFY_COOLDOWN_INFRA_S

    def notify(self, report: WatchdogReport) -> list[str]:
        """Send alerts for issues via ntfy, with per-issue cooldown + dedup.

        State persists to ``watchdog-state.json`` so a fresh systemd-timer-spawned
        process inherits memory of what it already paged about. A known issue
        re-pages after its cooldown (so a persistent critical still surfaces
        periodically), escalations always page immediately, and terminal issues
        (heal gave up) page once. This closes the Jun 23 two-day alert storm
        where every 5-min timer fired a fresh ntfy for the same non-fixable
        critical because no state survived across process boundaries.
        """
        msgs: list[str] = []
        if not report.issues:
            return msgs

        # Only page-worthy issues make it to ntfy in the first place. 'info' is
        # an audit/UI surface, never a page.
        pageable = [i for i in report.issues if i.severity in ("critical", "warning", "infra") and not i.terminal]
        if not pageable:
            # Possibly only terminal or info issues — log a single summary line.
            return msgs

        now = time.time()
        sent_any = False
        for issue in pageable:
            key = _issue_key(issue)
            rec = self._notify_state.get(key)
            if rec is not None:
                last = float(rec.get("last_notified_ts") or 0)
                prev_sev = rec.get("severity", "")
                cooldown = self._cooldown_for(issue)
                # Same severity + within cooldown → suppress.
                # Severity escalation (warning → critical) → always page.
                if issue.severity == prev_sev and (now - last) < cooldown:
                    continue
            # Page.
            body_lines = [f"Watchdog: {report.summary()}"]
            icon = "🔴" if issue.severity == "critical" else ("🟠" if issue.severity == "infra" else "🟡")
            body_lines.append(f"{icon} {issue.service}: {issue.message}")
            if issue.diagnosis:
                body_lines.append(f"   💡 {issue.diagnosis}")
            body = "\n".join(body_lines)
            if self._send_ntfy(
                body,
                title=f"Homelab Watchdog: {report.summary()}",
                priority="high" if issue.severity == "critical" else "default",
                tags="rotating_light" if issue.severity == "critical" else "warning",
            ):
                sent_any = True
                self._notify_state[key] = {
                    "last_notified_ts": now,
                    "severity": issue.severity,
                    "notified_count": int(rec.get("notified_count", 0)) + 1 if rec else 1,
                    "terminal": bool(rec.get("terminal", False)) if rec else False,
                }
                self._log_event("notify", "ntfy", report.summary())
            else:
                msgs.append("ntfy not reachable")

        if sent_any:
            msgs.append("Sent alert to ntfy")
            # Persist notify state so the next process invocation dedups.
            self._save_restart_state()
        return msgs

    # ── Prometheus metrics ─────────────────────────────────────

    def prometheus_metrics(self, report: WatchdogReport | None = None) -> str:
        """Export health data in Prometheus text exposition format.

        ``report`` is the already-computed scan from the timer path
        (``full_check()`` runs once and is passed in here). Calling this
        without one falls back to a snapshot only for ad-hoc CLI probes
        — the Prometheus scrape path never triggers a second full_scan
        (that doubled load + re-ran side-effecting checks on every scrape,
        the audit-flagged SEV2).
        """
        if report is None:
            report = self.full_check()
        # Node is part of identity because fleet agents intentionally reuse
        # container names on multiple machines.
        issue_containers = {
            (i.service, i.node)
            for i in report.issues
            if i.service not in ("docker", "disk", "memory", "port-conflict", "dns", "config")
        }

        lines = [
            "# HELP watchdog_healthy_containers Number of healthy containers",
            "# TYPE watchdog_healthy_containers gauge",
            f"watchdog_healthy_containers {len(report.healthy)}",
            "# HELP watchdog_issues_total Number of detected issues by severity",
            "# TYPE watchdog_issues_total gauge",
            f'watchdog_issues_total{{severity="critical"}} {sum(1 for i in report.issues if i.severity == "critical")}',
            f'watchdog_issues_total{{severity="warning"}} {sum(1 for i in report.issues if i.severity == "warning")}',
            f'watchdog_issues_total{{severity="info"}} {sum(1 for i in report.issues if i.severity == "info")}',
            "# HELP watchdog_ok Overall health status (1=ok, 0=issues)",
            "# TYPE watchdog_ok gauge",
            f"watchdog_ok {1 if report.ok else 0}",
            "# HELP watchdog_containers_total Total number of containers tracked",
            "# TYPE watchdog_containers_total gauge",
            f"watchdog_containers_total {len(report.healthy) + len(issue_containers)}",
            "# HELP watchdog_auto_fixable_issues Number of issues eligible for safe auto-recovery",
            "# TYPE watchdog_auto_fixable_issues gauge",
            f"watchdog_auto_fixable_issues {sum(1 for i in report.issues if i.auto_fixable)}",
            "# HELP watchdog_last_check_timestamp_seconds Unix timestamp of the last health check",
            "# TYPE watchdog_last_check_timestamp_seconds gauge",
            f"watchdog_last_check_timestamp_seconds {report.timestamp:.0f}",
        ]

        # Per-container status (1=healthy, 0=issue) — deduplicated
        lines.append("# HELP watchdog_container_healthy Per-container health (1=healthy, 0=issue)")
        lines.append("# TYPE watchdog_container_healthy gauge")
        for container in report.healthy:
            if (container.name, container.node) not in issue_containers:
                lines.append(f'watchdog_container_healthy{{container="{container.name}",node="{container.node}"}} 1')
        for name, node_label in sorted(issue_containers):
            lines.append(f'watchdog_container_healthy{{container="{name}",node="{node_label}"}} 0')

        # Per-container CPU and memory usage from docker stats
        stats = self._get_container_stats()
        if stats:
            lines.append("# HELP watchdog_container_cpu_percent Per-container CPU usage percentage")
            lines.append("# TYPE watchdog_container_cpu_percent gauge")
            for s in stats:
                try:
                    cpu = float(s["cpu"].rstrip("%"))
                    lines.append(
                        f'watchdog_container_cpu_percent{{container="{s["name"]}",node="{s["node"]}"}} {cpu:.2f}'
                    )
                except ValueError:
                    continue
            lines.append("# HELP watchdog_container_memory_percent Per-container memory usage percentage")
            lines.append("# TYPE watchdog_container_memory_percent gauge")
            for s in stats:
                try:
                    mem = float(s["mem_pct"].rstrip("%"))
                    lines.append(
                        f'watchdog_container_memory_percent{{container="{s["name"]}",node="{s["node"]}"}} {mem:.2f}'
                    )
                except ValueError:
                    continue

        # Per-container uptime
        uptimes = self._get_container_uptimes()
        if uptimes:
            lines.append("# HELP watchdog_container_uptime_seconds Container uptime in seconds")
            lines.append("# TYPE watchdog_container_uptime_seconds gauge")
            for (name, node_label), secs in uptimes.items():
                lines.append(f'watchdog_container_uptime_seconds{{container="{name}",node="{node_label}"}} {secs:.0f}')

        # Disk usage (root + docker data)
        for path, label in [("/", "root"), ("/var/lib/docker", "docker_data")]:
            try:
                usage = shutil.disk_usage(path)
                pct = (usage.used / usage.total) * 100
                lines.extend(
                    [
                        f"# HELP watchdog_disk_usage_percent Disk usage percentage for {label}",
                        "# TYPE watchdog_disk_usage_percent gauge",
                        f'watchdog_disk_usage_percent{{mount="{label}"}} {pct:.1f}',
                        f'watchdog_disk_free_bytes{{mount="{label}"}} {usage.free}',
                        f'watchdog_disk_total_bytes{{mount="{label}"}} {usage.total}',
                    ]
                )
            except OSError:
                pass

        # Memory usage (detailed)
        try:
            with open("/proc/meminfo", encoding="utf-8") as f:
                info = {}
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        info[parts[0].rstrip(":")] = int(parts[1])
            total = info.get("MemTotal", 0)
            available = info.get("MemAvailable", 0)
            buffers = info.get("Buffers", 0)
            cached = info.get("Cached", 0)
            if total > 0:
                mem_pct = ((total - available) / total) * 100
                lines.extend(
                    [
                        "# HELP watchdog_memory_usage_percent System memory usage percentage",
                        "# TYPE watchdog_memory_usage_percent gauge",
                        f"watchdog_memory_usage_percent {mem_pct:.1f}",
                        "# HELP watchdog_memory_total_bytes Total system memory in bytes",
                        "# TYPE watchdog_memory_total_bytes gauge",
                        f"watchdog_memory_total_bytes {total * 1024}",
                        "# HELP watchdog_memory_available_bytes Available system memory in bytes",
                        "# TYPE watchdog_memory_available_bytes gauge",
                        f"watchdog_memory_available_bytes {available * 1024}",
                        "# HELP watchdog_memory_cached_bytes Cached memory in bytes",
                        "# TYPE watchdog_memory_cached_bytes gauge",
                        f"watchdog_memory_cached_bytes {(buffers + cached) * 1024}",
                    ]
                )
        except OSError:
            pass

        # Restart attempt counts
        if self._restart_counts:
            lines.append("# HELP watchdog_restart_attempts_total Number of restart attempts per container")
            lines.append("# TYPE watchdog_restart_attempts_total counter")
            for name, count in self._restart_counts.items():
                lines.append(f'watchdog_restart_attempts_total{{container="{name}"}} {count}')

        # Heal success/failure tracking from event log
        heal_ok = sum(1 for e in self._events if e.action == "heal" and "verified healthy" in e.detail.lower())
        heal_fail = sum(
            1
            for e in self._events
            if e.action == "heal" and ("failed" in e.detail.lower() or "unconfirmed" in e.detail.lower())
        )
        lines.extend(
            [
                "# HELP watchdog_heal_success_total Total successful heal operations",
                "# TYPE watchdog_heal_success_total counter",
                f"watchdog_heal_success_total {heal_ok}",
                "# HELP watchdog_heal_failure_total Total failed heal operations",
                "# TYPE watchdog_heal_failure_total counter",
                f"watchdog_heal_failure_total {heal_fail}",
            ]
        )

        if self._backup_nodes:
            lines.extend(
                [
                    "# HELP watchdog_backup_healthy Per-node backup freshness (1=fresh, 0=unhealthy)",
                    "# TYPE watchdog_backup_healthy gauge",
                ]
            )
            for node in self._backup_nodes:
                lines.append(f'watchdog_backup_healthy{{node="{node.role}"}} {1 if node.ok else 0}')
            nodes_with_age = [node for node in self._backup_nodes if node.age_hours is not None]
            if nodes_with_age:
                lines.extend(
                    [
                        "# HELP watchdog_backup_age_hours Age of the latest per-node snapshot in hours",
                        "# TYPE watchdog_backup_age_hours gauge",
                    ]
                )
                for node in nodes_with_age:
                    lines.append(f'watchdog_backup_age_hours{{node="{node.role}"}} {node.age_hours:.2f}')

        if self._backup_drill_evidence is not None:
            lines.extend(
                [
                    "# HELP watchdog_backup_restore_drill_healthy "
                    "Latest bounded restore drill result (1=passed, 0=failed)",
                    "# TYPE watchdog_backup_restore_drill_healthy gauge",
                    f"watchdog_backup_restore_drill_healthy {1 if self._backup_drill_evidence.ok else 0}",
                    "# HELP watchdog_backup_restore_drill_timestamp_seconds "
                    "Timestamp of the latest bounded restore drill",
                    "# TYPE watchdog_backup_restore_drill_timestamp_seconds gauge",
                    "watchdog_backup_restore_drill_timestamp_seconds "
                    f"{self._backup_drill_evidence.checked_at.timestamp():.0f}",
                ]
            )

        lines.append("")
        return "\n".join(lines)
