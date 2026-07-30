"""Staggered Docker Compose startup — wave-based with health and load gates."""

from __future__ import annotations

import fcntl
import ipaddress
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Callable, Sequence, Set
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from toolkit.core.config.config import load_config
from toolkit.core.config.storage import env_path
from toolkit.core.infra.host_capacity import HostCapacity, detect_host_capacity
from toolkit.core.manifest.catalog import load_service_catalog

_COMPOSE_UP_RETRY_MARKERS = (
    "cannot assign requested address",
    "address already in use",
    "failed to bind host port",
    "failed to create task for container",
    "error mounting",
    "not a directory",
)

_LOG_PREFIX = "[staggered-up]"


def _log(msg: str) -> None:
    print(f"{_LOG_PREFIX} {msg}", flush=True)


def _warn(msg: str) -> None:
    print(f"{_LOG_PREFIX} WARN: {msg}", file=sys.stderr, flush=True)


def _read_env_value(env_file: Path, key: str) -> str:
    if not env_file.is_file():
        return ""
    pattern = re.compile(rf"^{re.escape(key)}=(.*)$")
    value = ""
    for line in env_file.read_text().splitlines():
        m = pattern.match(line.strip())
        if m:
            value = m.group(1).strip().strip('"').strip("'")
    return value


def _parse_profiles(env_file: Path) -> frozenset[str]:
    raw = _read_env_value(env_file, "COMPOSE_PROFILES")
    if not raw:
        return frozenset()
    return frozenset(p.strip() for p in raw.split(",") if p.strip())


def _local_ip_bindable(ip: str) -> bool:
    if not ip or ip in ("127.0.0.1", "0.0.0.0", "localhost"):
        return True
    try:
        proc = subprocess.run(
            ["ip", "-4", "addr", "show"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
        if proc.returncode == 0 and f" {ip}/" in proc.stdout:
            return True
    except OSError:
        pass
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind((ip, 0))
        return True
    except OSError:
        return False


def _ensure_regular_file(path: Path) -> None:
    """Docker creates a directory when bind-mounting a missing file path."""
    if path.is_dir():
        shutil.rmtree(path)


def _compose_document(path: Path) -> dict[str, Any]:
    document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(document, dict) or not isinstance(document.get("services"), dict):
        raise ValueError(f"Compose model {path} must contain a services mapping")
    return document


def _active_services(document: dict[str, Any], profiles: frozenset[str]) -> dict[str, dict[str, Any]]:
    active: dict[str, dict[str, Any]] = {}
    for name, raw in document["services"].items():
        if not isinstance(name, str) or not isinstance(raw, dict):
            raise ValueError("Compose services must be named mappings")
        declared_profiles = set(raw.get("profiles") or ())
        if not declared_profiles or declared_profiles.intersection(profiles):
            active[name] = raw
    return active


def _project_catalog_root(root: Path) -> Path | None:
    if any((root / "toolkit" / "services").glob("*/service.yaml")):
        return root
    if any((root / "services").glob("*/service.yaml")):
        return root
    return None


def unplanned_active_services(
    compose_path: Path,
    profiles: frozenset[str],
    planned_services: set[str],
) -> set[str]:
    """Return active Compose services omitted from the declarative wave plan."""
    document = yaml.safe_load(compose_path.read_text(encoding="utf-8")) or {}
    missing: set[str] = set()
    for name, spec in (document.get("services") or {}).items():
        service_profiles = set((spec or {}).get("profiles") or ())
        active = not service_profiles or bool(service_profiles & profiles)
        if active and name not in planned_services:
            missing.add(name)
    return missing


def _compose_ps_items(raw: str) -> list[dict[str, Any]]:
    data = raw.strip()
    if not data:
        return []
    try:
        items = json.loads(data)
    except json.JSONDecodeError:
        try:
            items = [json.loads(line) for line in data.splitlines() if line.strip()]
        except json.JSONDecodeError:
            return []
    if isinstance(items, dict):
        items = [items]
    if not isinstance(items, list):
        return []
    return [item for item in items if isinstance(item, dict)]


def _service_health_from_ps_json(raw: str, *, allow_completed: bool = False) -> str:
    items = _compose_ps_items(raw)
    if not items:
        return "error" if raw.strip() else "missing"
    try:
        for item in items:
            state = str(item.get("State", "") or "").lower()
            status = str(item.get("Status", "") or "").lower()
            health = str(item.get("Health", "") or "").lower()
            running = state in ("running", "up") or status.startswith("up") or "running" in status
            if not running:
                if allow_completed and state == "exited" and int(item.get("ExitCode", 1) or 0) == 0:
                    return "ok"
                return "exited"
            if health in ("", "none", "n/a"):
                return "ok"
            if health == "healthy":
                return "ok"
            if health == "starting":
                return "starting"
            return "not-ready"
        return "not-ready"
    except (TypeError, AttributeError):
        return "error"


def _compose_ps_args(service: str, completed_services: Set[str]) -> tuple[str, ...]:
    if service in completed_services:
        return "ps", "--all", "--format", "json", service
    return "ps", "--format", "json", service


@dataclass
class StaggeredComposeRunner:
    root: Path
    node: str
    subprocess_run: Callable[..., subprocess.CompletedProcess[Any]] = subprocess.run
    sleep: Callable[[float], None] = time.sleep
    wave_failures: int = 0
    _lock_fd: int | None = field(default=None, repr=False)
    _profiles: frozenset[str] = field(default_factory=frozenset, repr=False)
    _env_file: Path = field(init=False, repr=False)
    _status_file: Path = field(init=False, repr=False)
    _lock_file: Path = field(init=False, repr=False)
    _compose_cmd: list[str] = field(default_factory=list, repr=False)
    _compose_file: Path = field(init=False, repr=False)
    _compose_up_extra: list[str] = field(default_factory=lambda: ["--remove-orphans"], repr=False)
    _capacity: HostCapacity | None = field(default=None, repr=False)
    _completed_services: frozenset[str] = field(init=False, repr=False)
    _runtime_host_paths: dict[str, tuple[str, ...]] = field(init=False, repr=False)
    _recovery_mode: bool = False
    _lifecycle_state: dict[str, Any] = field(default_factory=dict, repr=False)

    def __post_init__(self) -> None:
        self.root = self.root.resolve()
        self._env_file = env_path(self.node, self.root)
        self._status_file = self.root / f".compose-up.{self.node}.status"
        self._lock_file = self.root / f".compose-deploy.{self.node}.lock"
        self._compose_file = self._env_file.parent / "compose.yaml"
        catalog = load_service_catalog(_project_catalog_root(self.root))
        completed: set[str] = set()
        host_paths: dict[str, tuple[str, ...]] = {}
        for manifest in catalog.manifests:
            for runtime_name, runtime in manifest.runtimes.items():
                if runtime.mode == "oneshot":
                    completed.add(runtime_name)
                if runtime.required_host_paths:
                    host_paths[runtime_name] = runtime.required_host_paths
        self._completed_services = frozenset(completed)
        self._runtime_host_paths = host_paths

    def _write_status(self, value: str) -> None:
        self._status_file.write_text(f"{value}\n")

    def _acquire_lock(self) -> bool:
        self._lock_file.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self._lock_file, os.O_CREAT | os.O_RDWR, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            _warn(f"Another staggered compose is running for {self.node} (lock {self._lock_file})")
            # We do NOT own the status file — another runner does. Writing
            # 'failed' here would clobber the legitimate owner's 'running'/'ok'
            # marker, causing the next compose_wait poll to skip a real deploy.
            return False
        self._lock_fd = fd
        return True

    def _release_lock(self) -> None:
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None

    def _apply_lxc_env_from_file(self) -> None:
        cores = _read_env_value(self._env_file, "HOMELAB_NODE_CORES")
        mem = _read_env_value(self._env_file, "HOMELAB_NODE_MEM_MB")
        if cores:
            os.environ["HOMELAB_NODE_CORES"] = cores
        if mem:
            os.environ["HOMELAB_NODE_MEM_MB"] = mem
        os.environ["HOMELAB_NODE"] = self.node

    def _load_capacity(self) -> HostCapacity:
        cfg = None
        if (self.root / "config.yaml").is_file():
            cfg = load_config(self.root / "config.yaml")
        return detect_host_capacity(cfg=cfg, root=self.root)

    def _load_threshold(self) -> float:
        if self._capacity:
            return self._capacity.load_threshold
        cores = os.cpu_count() or 2
        return float(cores * 2)

    @property
    def inter_wave_sleep(self) -> int:
        return self._capacity.inter_wave_sleep_s if self._capacity else 4

    @property
    def wave_timeout(self) -> int:
        return self._capacity.wave_timeout_s if self._capacity else 180

    @property
    def max_pull_parallel(self) -> int:
        return self._capacity.max_pull_parallel if self._capacity else 2

    def _read_load_1m(self) -> float:
        try:
            return float(Path("/proc/loadavg").read_text().split()[0])
        except OSError:
            return 0.0

    def load_gate_strict(self) -> None:
        max_waits = 12
        threshold = self._load_threshold()
        waits = 0
        la = self._read_load_1m()
        while la > threshold:
            waits += 1
            if waits > max_waits:
                _warn(f"Load {la} still > {threshold} after {max_waits} waits — continuing with caution")
                return
            wait_s = self.inter_wave_sleep * 3
            cores = self._capacity.cpu_cores if self._capacity else (os.cpu_count() or 2)
            _warn(f"Load {la} > {threshold} ({cores} cores) — sleeping {wait_s}s ({waits}/{max_waits})")
            self.sleep(wait_s)
            la = self._read_load_1m()

    def load_gate_ok(self) -> None:
        la = self._read_load_1m()
        threshold = self._load_threshold()
        if la > threshold:
            wait_s = self.inter_wave_sleep * 3
            _warn(f"Load {la} > {threshold} — inter-wave sleep {wait_s}s")
            self.sleep(wait_s)

    def wave_sleep(self) -> None:
        self.load_gate_ok()
        self.sleep(self.inter_wave_sleep)

    def _services_healthy(self, services: Sequence[str]) -> bool:
        proc = self._run_compose("ps", "--all", "--format", "json", *services, check=False)
        if proc.returncode != 0:
            return False
        items = {str(item.get("Service", "")): item for item in _compose_ps_items(proc.stdout or "")}
        for svc in services:
            item = items.get(svc)
            if item is None:
                return False
            status = _service_health_from_ps_json(json.dumps(item), allow_completed=svc in self._completed_services)
            if status != "ok":
                return False
        return True

    def _compose_up_would_change(self, services: Sequence[str]) -> bool:
        planned = tuple(service for service in services if service not in self._completed_services)
        if not planned:
            return False
        proc = self._run_compose("--dry-run", "up", "-d", "--no-deps", *planned, check=False)
        output = f"{proc.stdout or ''}\n{proc.stderr or ''}".strip()
        if proc.returncode != 0 or not output:
            return True
        change = re.compile(
            r"\b(?:Creating|Created|Recreate|Recreated|Starting|Started|Restarting|"
            r"Removing|Removed|Pulling|Pulled|Building|Built)\b",
            re.IGNORECASE,
        )
        return bool(change.search(output))

    def _can_skip_recovery_wave(self, services: Sequence[str]) -> bool:
        return self._recovery_mode and self._services_healthy(services) and not self._compose_up_would_change(services)

    def _build_compose_cmd(self) -> list[str]:
        from toolkit.core.compose.docker import deployment_compose_path

        cfg = load_config(self.root / "config.yaml")
        compose_file = deployment_compose_path(cfg, self.root, self.node)
        if not compose_file.is_file():
            raise FileNotFoundError(f"Compose model missing for {self.node}: {compose_file}")
        self._compose_file = compose_file
        limits = self.root / "generated" / self.node / "compose.limits.yml"
        cmd = [
            "docker",
            "compose",
            "-f",
            str(compose_file),
            "--env-file",
            str(self._env_file),
        ]
        if limits.is_file():
            cmd.extend(["-f", str(limits)])
        for profile in sorted(self._profiles):
            cmd.extend(["--profile", profile])
        return cmd

    def _run_compose(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[Any]:
        from toolkit.core.compose.docker import compose_process_environment

        return self.subprocess_run(
            [*self._compose_cmd, *args],
            cwd=self.root,
            text=True,
            capture_output=not sys.stdout.isatty(),
            env=compose_process_environment(self._env_file),
        )

    def _run_compose_streamed(self, *args: str) -> subprocess.CompletedProcess[Any]:
        """Run docker compose with live line-by-line output streaming.

        Emits each line via _log() so the user sees pull/start progress in real
        time instead of waiting in silence for minutes. Returns the same
        CompletedProcess shape as _run_compose (with stdout captured).
        """
        from toolkit.core.compose.docker import compose_process_environment

        proc = subprocess.Popen(
            [*self._compose_cmd, *args],
            cwd=self.root,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            env=compose_process_environment(self._env_file),
        )
        collected: list[str] = []
        if proc.stdout is None:
            proc.kill()
            proc.wait()
            raise RuntimeError("Compose output pipe was not created")
        for line in proc.stdout:
            line = line.rstrip()
            if line:
                _log(f"    {line}")
                collected.append(line)
        proc.wait()
        stdout_text = "\n".join(collected)
        return subprocess.CompletedProcess(
            args=[*self._compose_cmd, *args],
            returncode=proc.returncode,
            stdout=stdout_text,
            stderr="",
        )

    def compose_pull_retry(self, services: Sequence[str] = ()) -> bool:
        max_attempts = 5
        batch_size = self.max_pull_parallel
        if not services:
            for attempt in range(1, max_attempts + 1):
                proc = self._run_compose("pull", "--ignore-buildable", check=False)
                if proc.returncode == 0:
                    return True
                _warn(f"docker compose pull failed (attempt {attempt}/{max_attempts})")
                self.sleep(5)
            return False

        idx = 0
        names = list(services)
        while idx < len(names):
            batch = names[idx : idx + batch_size]
            idx += len(batch)
            for attempt in range(1, max_attempts + 1):
                proc = self._run_compose("pull", "--ignore-buildable", *batch, check=False)
                if proc.returncode == 0:
                    break
                _warn(f"docker compose pull failed for batch (attempt {attempt}/{max_attempts}): {' '.join(batch)}")
                self.sleep(5)
            else:
                return False
            self.load_gate_ok()
        return True

    def wait_for_wave(self, wave_name: str, services: Sequence[str]) -> bool:
        if not services:
            return True
        deadline = time.monotonic() + self.wave_timeout
        _log(f"Waiting for wave '{wave_name}' ({len(services)} services, timeout {self.wave_timeout}s)...")
        while time.monotonic() < deadline:
            statuses: list[str] = []
            for svc in services:
                proc = self._run_compose(*_compose_ps_args(svc, self._completed_services), check=False)
                statuses.append(
                    _service_health_from_ps_json(proc.stdout or "", allow_completed=svc in self._completed_services)
                )
            if statuses and all(s == "ok" for s in statuses):
                _log(f"Wave '{wave_name}' healthy.")
                return True
            self.sleep(5)
        _warn(f"Wave '{wave_name}' did not reach healthy within {self.wave_timeout}s")
        return False

    def wait_for_remote_tcp(self, host: str, port: int, label: str) -> bool:
        deadline = time.monotonic() + self.wave_timeout
        _log(f"Waiting for {label} at {host}:{port} (timeout {self.wave_timeout}s)...")
        while time.monotonic() < deadline:
            try:
                with socket.create_connection((host, port), timeout=2):
                    _log(f"{label} reachable at {host}:{port}.")
                    return True
            except OSError:
                pass
            self.sleep(3)
        _warn(f"{label} not reachable at {host}:{port} within {self.wave_timeout}s")
        return False

    def wait_for_local_ip(self, ip: str, *, label: str = "PRIVATE_IP") -> bool:
        if not ip or ip in ("127.0.0.1", "0.0.0.0"):
            return True
        deadline = time.monotonic() + min(self.wave_timeout, 120)
        _log(f"Waiting for {label}={ip} to be bindable...")
        while time.monotonic() < deadline:
            if _local_ip_bindable(ip):
                _log(f"{label}={ip} bindable.")
                return True
            self.sleep(2)
        _warn(f"{label}={ip} not bindable within timeout")
        return False

    def _ensure_compose_artifacts(self) -> None:
        """Regenerate or repair files compose references before any up_wave."""
        document = _compose_document(self._compose_file)
        active = _active_services(document, self._profiles)
        from toolkit.services import discover_service_plugins

        cfg = load_config(self.root / "config.yaml")
        owners = []
        for plugin in discover_service_plugins():
            if not plugin.manifest.generated_artifacts:
                continue
            owns_active_runtime = False
            if plugin.manifest.runtime == "embedded":
                owns_active_runtime = plugin.is_enabled(cfg) and (
                    not cfg.is_multi_node or plugin.runtime_node(cfg) == self.node
                )
            elif plugin.has_compose_application:
                owns_active_runtime = bool(set(plugin.compose_application()["services"]).intersection(active))
            if owns_active_runtime:
                owners.append(plugin)
        missing: list[str] = []
        for plugin in owners:
            for artifact in plugin.manifest.generated_artifacts:
                path = self.root / artifact.path
                valid = path.is_symlink() if artifact.kind == "symlink" else path.is_file() and not path.is_symlink()
                if not valid:
                    missing.append(artifact.path)
        if not missing:
            return
        _log(f"Repairing {len(missing)} missing generated service artifact(s)...")
        try:
            from toolkit.core.config.storage import secrets_path
            from toolkit.core.generate.artifacts import generate_service_artifacts
            from toolkit.core.secrets.secrets import load_secrets_plaintext

            secrets = load_secrets_plaintext(secrets_path(self.root))
            generate_service_artifacts(cfg, self.root, secrets, plugins=owners)
        except Exception as exc:
            raise RuntimeError(f"could not repair generated service artifacts: {exc}") from exc

    def _remove_managed_network(
        self,
        network_name: str,
        *,
        allowed_container_ids: set[str],
        action: str,
    ) -> None:
        """Remove a managed bridge, tolerating Docker's asynchronous endpoint cleanup."""
        attempts = 5
        last_error = ""
        for attempt in range(1, attempts + 1):
            removed = self.run_host(["docker", "network", "rm", network_name])
            if removed.returncode == 0:
                return
            last_error = (removed.stderr or removed.stdout or "").strip()

            inspection = self.run_host(["docker", "network", "inspect", network_name])
            if inspection.returncode != 0:
                inspection_error = (inspection.stderr or inspection.stdout or "").strip().lower()
                if "no such network" in inspection_error or "not found" in inspection_error:
                    return
                last_error = inspection_error or last_error
            else:
                try:
                    payload = json.loads(inspection.stdout or "")
                    attached = set((payload[0].get("Containers") or {}).keys())
                except (IndexError, KeyError, AttributeError, TypeError, json.JSONDecodeError) as exc:
                    raise RuntimeError(f"could not inspect managed network {network_name} during removal") from exc
                unexpected = attached - allowed_container_ids
                if unexpected:
                    raise RuntimeError(
                        f"managed network {network_name} gained an unmanaged endpoint during removal; "
                        "refusing automatic cleanup"
                    )
            if attempt < attempts:
                delay = float(attempt)
                _warn(
                    f"Docker has not released network {network_name} yet "
                    f"(attempt {attempt}/{attempts}); retrying in {delay:.0f}s"
                )
                self.sleep(delay)

        detail = f": {last_error}" if last_error else ""
        raise RuntimeError(f"could not {action} managed network {network_name}{detail}")

    def _reconcile_changed_networks(self) -> None:
        """Drain managed endpoints before replacing bridges with changed IPAM."""
        document = _compose_document(self._compose_file)
        project = str(document.get("name") or self.root.name)
        networks = document.get("networks") or {}
        if not isinstance(networks, dict):
            raise ValueError("Compose networks must be a mapping")
        services = document.get("services") or {}
        if not isinstance(services, dict):
            raise ValueError("Compose services must be a mapping")

        desired_names = {
            str(raw.get("name") or f"{project}_{key}")
            for key, raw in networks.items()
            if isinstance(key, str) and isinstance(raw, dict) and not raw.get("external")
        }
        existing = self.run_host(
            [
                "docker",
                "network",
                "ls",
                "--filter",
                f"label=com.docker.compose.project={project}",
                "--format",
                "{{.Name}}",
            ]
        )
        if existing.returncode != 0:
            raise RuntimeError(f"could not list managed networks for Compose project {project}")
        obsolete = sorted(
            name.strip()
            for name in (existing.stdout or "").splitlines()
            if name.strip() and name.strip() not in desired_names
        )
        for network_name in obsolete:
            inspection = self.run_host(["docker", "network", "inspect", network_name])
            if inspection.returncode != 0:
                raise RuntimeError(f"could not inspect obsolete managed network {network_name}")
            try:
                payload = json.loads(inspection.stdout or "")
                attached = sorted((payload[0].get("Containers") or {}).keys())
            except (IndexError, KeyError, AttributeError, TypeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"could not inspect obsolete managed network {network_name}") from exc
            if attached:
                containers = self.run_host(["docker", "container", "inspect", *attached])
                if containers.returncode != 0:
                    raise RuntimeError(f"could not inspect containers attached to {network_name}")
                try:
                    container_payload = json.loads(containers.stdout or "")
                    projects = {
                        str(item.get("Config", {}).get("Labels", {}).get("com.docker.compose.project", ""))
                        for item in container_payload
                        if isinstance(item, dict)
                    }
                except (AttributeError, TypeError, json.JSONDecodeError) as exc:
                    raise RuntimeError(f"could not inspect containers attached to {network_name}") from exc
                if len(container_payload) != len(attached) or projects != {project}:
                    raise RuntimeError(
                        f"obsolete managed network {network_name} has an unmanaged container; "
                        "refusing automatic cleanup"
                    )
                _log(f"Removing obsolete network {network_name}; draining {len(attached)} managed container(s)")
                removed = self.run_host(["docker", "rm", "--force", *attached])
                if removed.returncode != 0:
                    raise RuntimeError(f"could not drain managed containers from {network_name}")
            self._remove_managed_network(
                network_name,
                allowed_container_ids=set(attached),
                action="remove obsolete",
            )

        for key, raw in sorted(networks.items()):
            if not isinstance(key, str) or not isinstance(raw, dict) or raw.get("external"):
                continue
            ipam = raw.get("ipam")
            if not isinstance(ipam, dict):
                continue
            configs = ipam.get("config")
            if not isinstance(configs, list):
                continue
            desired = {
                ipaddress.ip_network(subnet)
                for config in configs
                if isinstance(config, dict) and isinstance((subnet := config.get("subnet")), str) and "${" not in subnet
            }
            if not desired:
                continue

            network_name = str(raw.get("name") or f"{project}_{key}")
            inspection = self.run_host(["docker", "network", "inspect", network_name])
            if inspection.returncode != 0:
                continue
            try:
                payload = json.loads(inspection.stdout or "")
                network = payload[0]
                actual = {
                    ipaddress.ip_network(config["Subnet"])
                    for config in network["IPAM"]["Config"]
                    if isinstance(config, dict) and config.get("Subnet")
                }
                attached = sorted((network.get("Containers") or {}).keys())
            except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"could not inspect managed network {network_name}") from exc
            if desired == actual:
                continue

            declared_services: list[str] = []
            for service_name, service in services.items():
                if not isinstance(service_name, str) or not isinstance(service, dict):
                    continue
                service_networks = service.get("networks")
                network_names = (
                    service_networks
                    if isinstance(service_networks, list)
                    else service_networks.keys()
                    if isinstance(service_networks, dict)
                    else ()
                )
                if key in network_names:
                    declared_services.append(service_name)

            drain = set(attached)
            for service_name in declared_services:
                discovered = self.run_host(
                    [
                        "docker",
                        "ps",
                        "--all",
                        "--quiet",
                        "--filter",
                        f"label=com.docker.compose.project={project}",
                        "--filter",
                        f"label=com.docker.compose.service={service_name}",
                    ]
                )
                if discovered.returncode != 0:
                    raise RuntimeError(f"could not find managed service {service_name} for {network_name}")
                drain.update(line.strip() for line in (discovered.stdout or "").splitlines() if line.strip())

            attached = sorted(drain)
            if attached:
                containers = self.run_host(["docker", "container", "inspect", *attached])
                if containers.returncode != 0:
                    raise RuntimeError(f"could not inspect containers attached to {network_name}")
                try:
                    container_payload = json.loads(containers.stdout or "")
                    projects = {
                        str(item.get("Config", {}).get("Labels", {}).get("com.docker.compose.project", ""))
                        for item in container_payload
                        if isinstance(item, dict)
                    }
                except (AttributeError, TypeError, json.JSONDecodeError) as exc:
                    raise RuntimeError(f"could not inspect containers attached to {network_name}") from exc
                if len(container_payload) != len(attached) or projects != {project}:
                    raise RuntimeError(
                        f"managed network {network_name} has an unmanaged container; refusing automatic migration"
                    )
                _log(
                    f"Network {network_name} IPAM changed; "
                    f"draining {len(attached)} managed container(s) before migration"
                )
                removed = self.run_host(["docker", "rm", "--force", *attached])
                if removed.returncode != 0:
                    raise RuntimeError(f"could not drain managed containers from {network_name}")

            self._remove_managed_network(
                network_name,
                allowed_container_ids=set(attached),
                action="replace",
            )

    def up_wave(self, *services: str, force_recreate: bool = False) -> bool:
        if not services:
            return True
        self.load_gate_ok()
        private_ip = _read_env_value(self._env_file, "PRIVATE_IP")
        if private_ip and not _local_ip_bindable(private_ip):
            self.wait_for_local_ip(private_ip)
        if not self.compose_pull_retry(services):
            _warn(f"docker compose pull failed for: {' '.join(services)}")
            return False
        # A wave owns only the services it declares.  Without --no-deps,
        # Compose can reconcile a changed dependency during a later wave (for
        # example replacing Postgres while starting postgres-exporter).
        extra = [*self._compose_up_extra, "--no-deps"]
        if force_recreate and "--no-recreate" not in extra:
            extra.append("--force-recreate")
        max_attempts = 5
        for attempt in range(1, max_attempts + 1):
            # Stream the compose-up output live so the user sees pull + start progress
            # instead of waiting in silence for minutes.
            _log(f"  docker compose up -d {' '.join(services)} (attempt {attempt}/{max_attempts})")
            proc = self._run_compose_streamed("up", "-d", *extra, *services)
            if proc.returncode == 0:
                if proc.stdout:
                    _log(proc.stdout.strip())
                _log(f"  ✓ {' '.join(services)} started")
                return True
            err = f"{proc.stderr or ''}\n{proc.stdout or ''}".lower()
            if attempt < max_attempts and any(m in err for m in _COMPOSE_UP_RETRY_MARKERS):
                _warn(
                    f"docker compose up transient error for {' '.join(services)} "
                    f"(attempt {attempt}/{max_attempts}), retrying..."
                )
                if private_ip:
                    self.wait_for_local_ip(private_ip)
                self.sleep(self.inter_wave_sleep * attempt)
                continue
            _warn(f"docker compose up failed for: {' '.join(services)}")
            if proc.stderr:
                print(proc.stderr, file=sys.stderr, end="")
            return False
        return False

    def wave(self, name: str, *services: str) -> bool:
        if not self.up_wave(*services):
            self.wave_failures += 1
            _warn(f"Wave '{name}' up failed")
            return False
        if services and not self.wait_for_wave(name, services):
            self.wave_failures += 1
            _warn(f"Wave '{name}' health check failed")
            return False
        self.wave_sleep()
        return True

    def _record_failure(self) -> None:
        self.wave_failures += 1

    def compose(self, *args: str) -> subprocess.CompletedProcess[Any]:
        return self._run_compose(*args, check=False)

    def run_host(self, args: list[str]) -> subprocess.CompletedProcess[Any]:
        return self.subprocess_run(args, cwd=self.root, text=True, capture_output=True, check=False)

    def services_healthy(self, services: tuple[str, ...]) -> bool:
        return self._services_healthy(services)

    def wait_until_healthy(self, name: str, services: tuple[str, ...]) -> bool:
        return self.wait_for_wave(name, services)

    def retry_services(self, services: tuple[str, ...]) -> bool:
        return self.up_wave(*services)

    def run_recovery(self, function: str, module: str, **kwargs: Any) -> None:
        self._run_heal_lines(function, module, **kwargs)

    def record_failure(self) -> None:
        self._record_failure()

    def resolve_failure(self) -> None:
        if self.wave_failures > 0:
            self.wave_failures -= 1

    def log(self, message: str) -> None:
        _log(message)

    def warn(self, message: str) -> None:
        _warn(message)

    def environment(self, name: str, default: str = "") -> str:
        return _read_env_value(self._env_file, name) or default

    def state(self, key: str, default: Any = None) -> Any:
        return self._lifecycle_state.get(key, default)

    def set_state(self, key: str, value: Any) -> None:
        self._lifecycle_state[key] = value

    def add_compose_up_option(self, option: str) -> None:
        if option not in self._compose_up_extra:
            self._compose_up_extra.append(option)

    def remove_compose_up_option(self, option: str) -> None:
        self._compose_up_extra = [value for value in self._compose_up_extra if value != option]

    def _python_for_heals(self) -> str:
        venv_py = self.root / ".venv" / "bin" / "python3"
        if venv_py.is_file() and os.access(venv_py, os.X_OK):
            return str(venv_py)
        return sys.executable

    def _run_heal_lines(self, fn_name: str, module: str, **kwargs: Any) -> None:
        py = self._python_for_heals()
        kw = "".join(f", {k}={v!r}" for k, v in kwargs.items())
        root_repr = repr(str(self.root))
        code = (
            "import importlib\n"
            "from pathlib import Path\n"
            f"recovery = getattr(importlib.import_module({module!r}), {fn_name!r})\n"
            f"for line in recovery(Path({root_repr}){kw}):\n"
            f"    print('[staggered-up] ' + line)\n"
        )
        env = {**os.environ, "HOMELAB_ROOT": str(self.root), "PYTHONPATH": str(self.root)}
        try:
            proc = self.subprocess_run(
                [py, "-c", code],
                cwd=self.root,
                env=env,
                text=True,
                capture_output=False,
                check=False,
            )
            if proc.returncode != 0:
                _warn(f"{fn_name} heal exited {proc.returncode}")
        except OSError as exc:
            _warn(f"{fn_name} heal skipped: {exc}")

    def _run_role_waves(
        self,
        *,
        before_up: Callable[[str, tuple[str, ...]], tuple[str, ...] | None] | None = None,
        after_up: Callable[[str, tuple[str, ...]], None] | None = None,
    ) -> None:
        """Execute manifest-compiled waves with optional lifecycle hooks."""
        from toolkit.core.registry.stagger_planner import compose_stagger_waves

        cfg = load_config(self.root / "config.yaml")
        waves = compose_stagger_waves(
            self.root,
            cfg,
            self.node,
            compose_path=self._compose_file,
            profiles=self._profiles,
        )
        planned = {service for wave in waves for service in wave.services}
        missing = unplanned_active_services(self._compose_file, self._profiles, planned)
        if missing:
            raise RuntimeError(f"active services missing from startup waves: {', '.join(sorted(missing))}")
        for wave in waves:
            services = wave.services
            if not services:
                continue
            if self._can_skip_recovery_wave(services):
                _log(f"Wave '{wave.name}' already matches desired state and is healthy — skipping")
                continue
            if before_up:
                adjusted = before_up(wave.name, services)
                if adjusted is not None:
                    services = adjusted
                if not services:
                    continue
            force_recreate = not self._services_healthy(services)
            if not self.up_wave(*services, force_recreate=force_recreate):
                self._record_failure()
                _warn(f"{wave.name} startup failed; skipping health wait")
                self.wave_sleep()
                continue
            if not self.wait_for_wave(wave.name, services):
                self._record_failure()
                _warn(f"{wave.name} health check failed")
            if after_up:
                after_up(wave.name, services)
            self.wave_sleep()

    def _before_wave(self, wave_name: str, services: tuple[str, ...]) -> tuple[str, ...] | None:
        available: list[str] = []
        for service in services:
            missing = tuple(path for path in self._runtime_host_paths.get(service, ()) if not Path(path).exists())
            if missing:
                raise RuntimeError(f"runtime {service!r} requires unavailable host paths: {', '.join(missing)}")
            available.append(service)
        services = tuple(available)
        from toolkit.services import get_service_plugin

        plugin = get_service_plugin(wave_name)
        if plugin is not None:
            services = plugin.before_runtime_start(self, services)
        if os.environ.get("HOMELAB_PRESERVE_CONTROLLER", "").strip().lower() in {"1", "true", "yes"}:
            services = tuple(service for service in services if service != "homelab-controller")
        return services

    def _after_wave(self, wave_name: str, services: tuple[str, ...]) -> None:
        from toolkit.services import get_service_plugin

        plugin = get_service_plugin(wave_name)
        if plugin is not None:
            plugin.after_runtime_start(self, services)

    def _wait_for_remote_dependencies(self) -> None:
        cfg = load_config(self.root / "config.yaml")
        catalog = load_service_catalog(_project_catalog_root(self.root))
        from toolkit.core.manifest.placement import manifest_node, manifest_runtime_nodes
        from toolkit.core.manifest.routes import service_is_enabled

        waited: set[tuple[str, int]] = set()

        def wait(host: str, port_text: str, local_names: set[str], label: str) -> None:
            if not host or not port_text:
                return
            port = int(port_text)
            endpoint = (host, port)
            if host in local_names or endpoint in waited:
                return
            waited.add(endpoint)
            if not self.wait_for_remote_tcp(host, port, label):
                self._record_failure()

        for manifest in catalog.manifests:
            roles = {manifest_node(cfg, manifest)}
            for runtime_service in manifest.runtimes:
                roles.update(manifest_runtime_nodes(cfg, manifest, runtime_service))
            if not service_is_enabled(cfg, manifest, catalog) or self.node not in roles:
                continue
            for binding in manifest.databases:
                host = _read_env_value(self._env_file, binding.host_env)
                port_text = _read_env_value(self._env_file, binding.port_env)
                if not host or not port_text:
                    continue
                provider = catalog.require(binding.provider)
                contract = provider.database_provider
                endpoint = provider.service_endpoint
                if contract is None or endpoint is None:
                    raise ValueError(f"database provider {provider.name!r} has an incomplete manifest contract")
                local_names = {provider.name, endpoint.compose_service, "127.0.0.1", "localhost"}
                wait(host, port_text, local_names, f"PostgreSQL dependency for {manifest.label}")
            for integration in manifest.integrations:
                if not integration.required or not integration.host_env or not integration.port_env:
                    continue
                dependency = catalog.require(integration.service)
                host = _read_env_value(self._env_file, integration.host_env)
                port_text = _read_env_value(self._env_file, integration.port_env)
                wait(
                    host,
                    port_text,
                    {dependency.name, "127.0.0.1", "localhost"},
                    f"{dependency.label} dependency for {manifest.label}",
                )

    def _prepare_runtime_deployment(self) -> None:
        active = _active_services(_compose_document(self._compose_file), self._profiles)
        catalog = load_service_catalog(_project_catalog_root(self.root))
        from toolkit.services import get_service_plugin

        for manifest in catalog.manifests:
            plugin = get_service_plugin(manifest.name)
            if plugin is None or not plugin.has_compose_application:
                continue
            services = tuple(name for name in plugin.compose_application()["services"] if name in active)
            if services:
                plugin.prepare_runtime_deployment(self, services)

    def _run_node(self) -> None:
        _log(f"=== {self.node} Node — manifest-planned staggered startup ===")
        self._prepare_runtime_deployment()
        self._wait_for_remote_dependencies()
        self._run_role_waves(before_up=self._before_wave, after_up=self._after_wave)
        _log(f"{self.node} startup complete.")

    def run(self) -> int:
        if not self._acquire_lock():
            return 1
        self._write_status("running")
        try:
            if not self._env_file.is_file():
                _warn(f".env not found at {self._env_file} — run 'homelab-toolkit generate' first")
                self._write_status("failed")
                return 1

            self._apply_lxc_env_from_file()
            self._capacity = self._load_capacity()
            self._profiles = _parse_profiles(self._env_file)
            self._compose_cmd = self._build_compose_cmd()
            self._recovery_mode = os.environ.get("HOMELAB_RECOVERY", "").strip().lower() in {
                "1",
                "true",
                "yes",
            }

            self.load_gate_strict()
            from toolkit.core.ops.maintenance import maybe_prune_docker_before_deploy

            aggressive = os.environ.get("HOMELAB_FORCE_COMPOSE", "").strip() in ("1", "true", "yes")
            for line in maybe_prune_docker_before_deploy(aggressive=aggressive):
                _log(line)
            self._ensure_compose_artifacts()
            self._reconcile_changed_networks()
            private_ip = _read_env_value(self._env_file, "PRIVATE_IP")
            if private_ip:
                self.wait_for_local_ip(private_ip)

            self._run_node()

            if self.wave_failures > 0:
                _warn(f"Staggered startup finished with {self.wave_failures} wave failure(s) for {self.node}")
                self._write_status("failed")
                return 1

            _log(f"Staggered startup complete for node: {self.node}")
            self._write_status("ok")
            return 0
        # BaseException covers SIGTERM/SIGKILL forwarded by `systemd-run` /
        # `systemctl stop`. Catching only `Exception` left the `.compose-up.<vm>.status`
        # marker stuck on 'running', which the launcher's dead-process gate treated as
        # 'already up', silently no-op'ing the next deploy.
        except BaseException:
            self._write_status("failed")
            raise
        finally:
            self._release_lock()


def run_staggered_compose(root: Path, node: str) -> int:
    """Run staggered compose-up for a declared node; return its exit code."""
    from toolkit.core.config.config import config_path, load_config
    from toolkit.core.config.roles import uses_remote_nodes
    from toolkit.core.ops.controller_guard import skip_message, skip_on_workstation

    cfg = load_config(config_path(root))
    if uses_remote_nodes(cfg) and skip_on_workstation("compose_up"):
        import sys

        print(skip_message("compose_up"), file=sys.stderr)
        print(
            "Refusing local Docker Compose on the controller; deploy the declared remote node instead.",
            file=sys.stderr,
        )
        return 2
    return StaggeredComposeRunner(root=root, node=node).run()
