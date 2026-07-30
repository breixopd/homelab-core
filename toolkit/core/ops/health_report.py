"""Comprehensive daily health report for homelab systems.

Collects system health data across multiple dimensions in parallel
and produces structured reports in text, JSON, HTML, and Markdown formats.

Usage:
    from toolkit.core.ops.health_report import create_health_report
    report = create_health_report(root, cfg)
    print(report.format_text())

Section collectors run independently via ThreadPoolExecutor. Each collector
gracefully degrades if system commands are unavailable (no Docker daemon,
no ZFS, etc.).
"""

from __future__ import annotations

import datetime
import html
import json
import logging
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

logger = logging.getLogger(__name__)

# Shared inline styles for HTML email rendering (email clients need inline CSS).
_HTML_BODY_FONT = "-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif"
_HTML_CARD = (
    "width:100%;border-collapse:collapse;background:#fff;border-radius:8px;"
    "overflow:hidden;box-shadow:0 1px 3px rgba(0,0,0,0.08);"
)
_HTML_PANEL = "width:100%;background:#fff;border-radius:8px;box-shadow:0 1px 3px rgba(0,0,0,0.08);"
_TD8 = "padding:8px 12px;border-bottom:1px solid #e2e8f0;"
_TD8_MONO = f"{_TD8}font-family:monospace;font-size:12px;"
_TH8 = "padding:8px 12px;font-size:11px;font-weight:600;color:#64748b;text-transform:uppercase;letter-spacing:0.5px;"


# ═══════════════════════════════════════════════════════════════════════════
# Section dataclasses
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class UpdatesSection:
    """Available system and tool updates."""

    python_lock: list[dict] = field(default_factory=list)
    toolkit_binaries: list[dict] = field(default_factory=list)
    ansible_collections: list[dict] = field(default_factory=list)
    old_images: list[dict] = field(default_factory=list)
    check_error: str = ""

    @property
    def total(self) -> int:
        return len(self.python_lock) + len(self.toolkit_binaries) + len(self.ansible_collections) + len(self.old_images)

    @property
    def has_updates(self) -> bool:
        return self.total > 0


@dataclass
class ContainerHealthSection:
    """Docker container health summary."""

    total: int = 0
    healthy: int = 0
    unhealthy: list[str] = field(default_factory=list)
    restarting: list[str] = field(default_factory=list)
    paused: list[str] = field(default_factory=list)
    issues: list[dict] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return len(self.unhealthy) == 0 and len(self.restarting) == 0


@dataclass
class ResourcesSection:
    """System CPU, memory, disk, and GPU resources."""

    cpu_cores: int = 0
    load_avg: tuple[float, float, float] = (0.0, 0.0, 0.0)
    memory_total_gb: float = 0.0
    memory_used_percent: float = 0.0
    swap_total_gb: float = 0.0
    swap_used_percent: float = 0.0
    disk_root_percent: float = 0.0
    disk_docker_percent: float = 0.0
    gpu: str = "none"


@dataclass
class StorageSection:
    """ZFS pools, backups, and log storage."""

    zfs_pools: list[dict] = field(default_factory=list)
    kopia_last_backup: str = "never"
    docker_logs_large: list[str] = field(default_factory=list)


@dataclass
class CertificatesSection:
    """SSL/TLS certificate status for key endpoints."""

    wildcard_days_left: int = -1
    url_checks: list[dict] = field(default_factory=list)


@dataclass
class SecuritySection:
    """Security tooling status."""

    wazuh_status: str = "unknown"
    fail2ban_active: bool = False
    fail2ban_jails: int = 0
    ufw_active: bool = True
    ufw_available: bool = False


@dataclass
class MaintenanceSection:
    """Maintenance-related reclaimable space metrics."""

    docker_prune_gb: float = 0.0
    journal_size_mb: float = 0.0
    old_logs_count: int = 0


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess:
    """Run a subprocess safely, returning a CompletedProcess even on failure."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        result = subprocess.CompletedProcess(cmd, returncode=-1, stdout="", stderr=str(exc))
        return result


def _run_framework_check_script(root: Path) -> tuple[list[dict], str]:
    """Run the framework dependency checker and preserve failures."""
    script = Path(__file__).resolve().parent.parent.parent.parent / "scripts" / "check-framework-updates.py"
    result = _run(
        [sys.executable, str(script), "--json", "--cache", "--root", str(root.resolve())],
        timeout=300,
    )
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "dependency checker exited unsuccessfully").strip()[:200]
        logger.warning("Framework dependency check failed: %s", detail)
        return [], detail
    try:
        payload = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        detail = f"invalid dependency-check response: {exc}"
        logger.warning("%s", detail)
        return [], detail
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        detail = "invalid dependency-check response type"
        logger.warning("%s", detail)
        return [], detail
    return payload, ""


def _get_load_avg() -> tuple[float, float, float]:
    """Read 1/5/15-minute load averages from /proc/loadavg."""
    try:
        with open("/proc/loadavg", encoding="utf-8") as f:
            parts = f.read().strip().split()
            return (float(parts[0]), float(parts[1]), float(parts[2]))
    except (OSError, IndexError, ValueError):
        return (0.0, 0.0, 0.0)


def _get_memory_info() -> tuple[float, float, float, float]:
    """Read memory stats from /proc/meminfo.

    Returns (total_gb, used_percent, swap_gb, swap_used_percent).
    """
    try:
        with open("/proc/meminfo", encoding="utf-8") as f:
            info: dict[str, int] = {}
            for line in f:
                parts = line.split()
                if len(parts) >= 2:
                    info[parts[0].rstrip(":")] = int(parts[1])

        total_kb = info.get("MemTotal", 0)
        available_kb = info.get("MemAvailable", total_kb)
        total_gb = total_kb / (1024 * 1024)
        used_pct = ((total_kb - available_kb) / total_kb * 100) if total_kb > 0 else 0.0

        swap_total_kb = info.get("SwapTotal", 0)
        swap_free_kb = info.get("SwapFree", 0)
        swap_gb = swap_total_kb / (1024 * 1024)
        swap_pct = ((swap_total_kb - swap_free_kb) / swap_total_kb * 100) if swap_total_kb > 0 else 0.0

        return (total_gb, used_pct, swap_gb, swap_pct)
    except (OSError, KeyError, ValueError):
        return (0.0, 0.0, 0.0, 0.0)


def _get_disk_usage(path: str) -> float:
    """Get disk usage percentage for a given mount point."""
    result = _run(["df", "--output=pcent", path], timeout=10)
    if result.returncode != 0:
        return 0.0
    lines = [ln.strip().rstrip("%") for ln in result.stdout.splitlines() if ln.strip() and ln.strip() != "Use%"]
    if lines:
        try:
            return float(lines[-1])
        except ValueError:
            pass
    return 0.0


def _parse_size_to_gb(size_str: str) -> float:
    """Parse a Docker/Metric size string (e.g. '123MB', '1.2GB') to GB."""
    if not size_str or size_str == "0B":
        return 0.0
    m = re.match(r"(\d+(?:\.\d+)?)\s*([KMGTP]?B)", size_str)
    if not m:
        return 0.0
    val = float(m.group(1))
    unit = m.group(2).upper()
    multipliers: dict[str, float] = {
        "B": 1e-9,
        "KB": 1e-6,
        "MB": 1e-3,
        "GB": 1.0,
        "TB": 1e3,
        "PB": 1e6,
    }
    return val * multipliers.get(unit, 0.0)


def _detect_gpu() -> str:
    """Detect available GPU acceleration.

    Returns 'nvidia', 'vaapi', or 'none'. Delegates to the unified capability
    detector (Phase F1) so a single canonical path decides GPU backend — the old
    ``vainfo``-binary check here previously diverged from the
    ``/dev/dri``-glob check elsewhere and could disagree on the same host.
    """
    from toolkit.core.capabilities.detect import detect_capabilities

    return detect_capabilities(vm="local").gpu.backend


def _cpu_count() -> int:
    """Count available CPU cores via /sys or os.cpu_count()."""
    try:
        cores = list(Path("/sys/devices/system/cpu").glob("cpu[0-9]*"))
        if cores:
            return len(cores)
    except OSError:
        pass
    return os_cpu_count()


def os_cpu_count() -> int:
    """Fallback CPU count via os module."""
    import os

    return os.cpu_count() or 0


# ═══════════════════════════════════════════════════════════════════════════
# Section collectors
# ═══════════════════════════════════════════════════════════════════════════


def _collect_updates(root: Path, cfg: Config | None = None) -> UpdatesSection:
    """Collect available system and image updates."""
    section = UpdatesSection()

    framework_updates, section.check_error = _run_framework_check_script(root)
    for item in framework_updates:
        source = item.get("source", "")
        if source == "python-lock":
            section.python_lock.append(item)
        elif source == "toolkit-binary":
            section.toolkit_binaries.append(item)
        elif source == "ansible-collection":
            section.ansible_collections.append(item)

    section.old_images = _collect_old_images()
    return section


def _collect_old_images() -> list[dict]:
    """Find containers running images older than 90 days."""
    from toolkit.core.ops.system_checks import collect_stale_images

    return [img.to_dict() for img in collect_stale_images(run=lambda cmd, timeout: _run(cmd, timeout=timeout))]


def _collect_containers() -> ContainerHealthSection:
    """Collect Docker container health information."""
    section = ContainerHealthSection()
    result = _run(["docker", "ps", "-a", "--format", "json"], timeout=15)
    if result.returncode != 0:
        section.issues.append({"container": "docker", "issue": "Cannot reach Docker daemon", "severity": "critical"})
        return section

    containers: list[dict] = []
    for line in result.stdout.strip().splitlines():
        if line:
            try:
                containers.append(json.loads(line))
            except json.JSONDecodeError:
                continue

    section.total = len(containers)
    for c in containers:
        name = c.get("Names", "")
        state = c.get("State", "")
        status = c.get("Status", "")

        if state == "running":
            if "unhealthy" in status.lower():
                section.unhealthy.append(name)
                section.issues.append({"container": name, "issue": f"Unhealthy: {status}", "severity": "warning"})
            else:
                section.healthy += 1
        elif state == "restarting":
            section.restarting.append(name)
            section.issues.append({"container": name, "issue": "Container in restart loop", "severity": "critical"})
        elif state == "paused":
            section.paused.append(name)
            section.issues.append({"container": name, "issue": "Container is paused", "severity": "warning"})
        elif state == "exited":
            section.issues.append({"container": name, "issue": f"Container exited: {status}", "severity": "warning"})
    return section


def _collect_resources() -> ResourcesSection:
    """Collect system resource utilisation."""
    section = ResourcesSection()
    section.cpu_cores = _cpu_count()
    section.load_avg = _get_load_avg()

    mem_total, mem_pct, swap_total, swap_pct = _get_memory_info()
    section.memory_total_gb = round(mem_total, 2)
    section.memory_used_percent = round(mem_pct, 1)
    section.swap_total_gb = round(swap_total, 2)
    section.swap_used_percent = round(swap_pct, 1)

    section.disk_root_percent = _get_disk_usage("/")
    section.disk_docker_percent = _get_disk_usage("/var/lib/docker")
    section.gpu = _detect_gpu()
    return section


def _collect_storage(root: Path) -> StorageSection:
    """Collect storage-related information."""
    section = StorageSection()
    section.zfs_pools = _collect_zfs_pools()
    section.kopia_last_backup = _collect_kopia_last_backup()
    section.docker_logs_large = _collect_large_logs()
    return section


def _collect_zfs_pools() -> list[dict]:
    """Check ZFS pool status via zpool list."""
    pools: list[dict] = []
    result = _run(["zpool", "list", "-H", "-o", "name,size,health"], timeout=15)
    if result.returncode != 0:
        return pools
    for line in result.stdout.strip().splitlines():
        parts = line.strip().split("\t")
        if len(parts) >= 3:
            pools.append({"name": parts[0], "size": parts[1], "health": parts[2]})
    return pools


def _collect_kopia_last_backup() -> str:
    """Get the ISO timestamp of the most recent Kopia snapshot.

    Returns 'never' if Kopia is unreachable or no snapshots exist.
    """
    result = _run(["docker", "exec", "kopia", "kopia", "snapshot", "list", "--json"], timeout=30)
    if result.returncode != 0:
        return "never"
    try:
        snapshots = json.loads(result.stdout)
        if not snapshots:
            return "never"
        entries = snapshots if isinstance(snapshots, list) else [snapshots]
        timestamps: list[str] = []
        for snap in entries:
            ts = snap.get("startTime", "")
            if ts:
                timestamps.append(ts)
        if timestamps:
            timestamps.sort(reverse=True)
            return timestamps[0]
    except (json.JSONDecodeError, TypeError, ValueError):
        pass
    return "never"


def _collect_large_logs() -> list[str]:
    """Find containers with Docker log files over 100 MB."""
    from toolkit.core.ops.system_checks import collect_large_container_logs

    large_logs = collect_large_container_logs(
        threshold_mb=100,
        run=lambda cmd, timeout: _run(cmd, timeout=timeout),
    )
    return [f"{info.name} ({info.size_mb} MB)" for info in large_logs]


def _collect_certificates(domain: str) -> CertificatesSection:
    """Check SSL certificate status for the domain and key endpoints."""
    section = CertificatesSection()
    if not domain:
        return section
    section.wildcard_days_left = _check_cert_days_left(domain)

    from toolkit.services.sdk import authelia_public_url_for_domain

    urls = [
        f"https://homelab.{domain}",
        authelia_public_url_for_domain(domain),
        f"https://{domain}",
    ]
    for url in urls:
        section.url_checks.append(_check_endpoint(url))
    return section


def _check_cert_days_left(hostname: str) -> int:
    """Check remaining days before SSL certificate expiry.

    Returns the number of days remaining, or -1 if uncheckable.
    """
    from toolkit.core.ops.system_checks import check_cert_days_left

    days = check_cert_days_left(hostname)
    return -1 if days is None else days


def _check_endpoint(url: str) -> dict:
    """Check HTTP endpoint availability and SSL validity."""
    result: dict[str, object] = {"url": url, "status_code": 0, "ssl_ok": False}
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "homelab-toolkit"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            result["status_code"] = resp.status
            result["ssl_ok"] = True
    except urllib.error.HTTPError as exc:
        result["status_code"] = exc.code
        result["ssl_ok"] = True
    except (urllib.error.URLError, OSError, TimeoutError):
        pass
    return result


def _collect_security() -> SecuritySection:
    """Collect security tooling status."""
    section = SecuritySection()
    section.wazuh_status = _check_wazuh_status()

    f2b_result = _run(["fail2ban-client", "status"], timeout=10)
    if f2b_result.returncode == 0:
        section.fail2ban_active = True
        m = re.search(r"Jail list:\s*(.*)", f2b_result.stdout)
        if m:
            jails = [j.strip() for j in m.group(1).split(",") if j.strip()]
            section.fail2ban_jails = len(jails)

    ufw_result = _run(["ufw", "status"], timeout=10)
    if ufw_result.returncode == 0:
        section.ufw_available = True
        section.ufw_active = "active" in ufw_result.stdout.lower()
    else:
        # ufw not installed — not an issue
        section.ufw_active = True

    return section


def _check_wazuh_status() -> str:
    """Derive Wazuh health from running container states.

    Returns 'green', 'yellow', 'red', or 'unknown'.
    """
    result = _run(["docker", "ps", "--format", "{{.Names}}\t{{.Status}}"], timeout=10)
    if result.returncode != 0:
        return "unknown"

    wazuh_lines = [ln for ln in result.stdout.strip().splitlines() if "wazuh" in ln.lower()]
    if not wazuh_lines:
        return "unknown"

    all_green = True
    any_issue = False
    for line in wazuh_lines:
        if "unhealthy" in line.lower() or "restarting" in line.lower() or "exited" in line.lower():
            all_green = False
            any_issue = True

    if all_green:
        return "green"
    if any_issue:
        return "red"
    return "yellow"


def _collect_maintenance(root: Path) -> MaintenanceSection:
    """Collect maintenance-reclaimable space metrics."""
    section = MaintenanceSection()

    df_result = _run(["docker", "system", "df", "--format", "json"], timeout=30)
    if df_result.returncode == 0:
        total_reclaimable = 0.0
        for line in df_result.stdout.strip().splitlines():
            if line:
                try:
                    entry = json.loads(line)
                    reclaimable = entry.get("Reclaimable", "0B")
                    total_reclaimable += _parse_size_to_gb(reclaimable)
                except (json.JSONDecodeError, TypeError):
                    continue
        section.docker_prune_gb = round(total_reclaimable, 2)

    journal_result = _run(["journalctl", "--disk-usage"], timeout=15)
    if journal_result.returncode == 0:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(\w+)", journal_result.stdout)
        if m:
            val = float(m.group(1))
            unit = m.group(2).lower()
            if unit in ("b",):
                section.journal_size_mb = val / (1024 * 1024)
            elif unit in ("k", "kb"):
                section.journal_size_mb = val / 1024
            elif unit in ("m", "mb"):
                section.journal_size_mb = val
            elif unit in ("g", "gb"):
                section.journal_size_mb = val * 1024

    section.old_logs_count = _count_old_logs(root)
    return section


def _count_old_logs(root: Path, max_age_days: int = 14) -> int:
    """Count log files older than max_age_days under the repository root."""
    cutoff = time.time() - max_age_days * 86400
    count = 0
    try:
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            if path.suffix not in (".log",) and ".log" not in path.name:
                continue
            try:
                if path.stat().st_mtime < cutoff:
                    count += 1
            except OSError:
                continue
    except OSError:
        pass
    return count


# ═══════════════════════════════════════════════════════════════════════════
# Parallel collector runner
# ═══════════════════════════════════════════════════════════════════════════


def _run_collectors(root: Path, cfg: Config | None = None) -> dict[str, Any]:
    """Run all section collectors in parallel via ThreadPoolExecutor.

    Returns a dict mapping collector name to its result (or None on failure).
    """
    results: dict[str, Any] = {}
    domain = cfg.domain if cfg is not None else ""

    with ThreadPoolExecutor(max_workers=4) as pool:
        futures: dict[Future[Any], str] = {
            pool.submit(_collect_updates, root, cfg): "updates",
            pool.submit(_collect_containers): "containers",
            pool.submit(_collect_resources): "resources",
            pool.submit(_collect_storage, root): "storage",
            pool.submit(_collect_certificates, domain): "certificates",
            pool.submit(_collect_security): "security",
            pool.submit(_collect_maintenance, root): "maintenance",
        }

        for future in as_completed(futures):
            key = futures[future]
            try:
                results[key] = future.result()
            except Exception as exc:
                logger.error("Health collector '%s' failed: %s", key, exc)
                results[key] = None

    return results


# ═══════════════════════════════════════════════════════════════════════════
# HealthReport dataclass
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class HealthReport:
    """Complete system health snapshot with multiple output formats."""

    timestamp: str = field(default_factory=lambda: datetime.datetime.now(datetime.UTC).isoformat())
    hostname: str = field(default_factory=socket.gethostname)
    domain: str = ""
    updates: UpdatesSection = field(default_factory=UpdatesSection)
    containers: ContainerHealthSection = field(default_factory=ContainerHealthSection)
    resources: ResourcesSection = field(default_factory=ResourcesSection)
    storage: StorageSection = field(default_factory=StorageSection)
    certificates: CertificatesSection = field(default_factory=CertificatesSection)
    security: SecuritySection = field(default_factory=SecuritySection)
    maintenance: MaintenanceSection = field(default_factory=MaintenanceSection)

    def has_issues(self) -> bool:
        """Return True if any section has items needing attention."""
        checks = [
            self.updates.has_updates,
            bool(self.updates.check_error),
            not self.containers.ok,
            self.resources.disk_root_percent > 90,
            self.resources.disk_docker_percent > 90,
            self.resources.memory_used_percent > 90,
            self.certificates.wildcard_days_left < 14 if self.certificates.wildcard_days_left >= 0 else False,
            self.security.wazuh_status == "red",
            self.security.ufw_available and not self.security.ufw_active,
            self.maintenance.docker_prune_gb > 5,
            self.maintenance.journal_size_mb > 500,
        ]
        return any(checks)

    # ── Text format ─────────────────────────────────────────────────

    def format_text(self) -> str:
        """Render a human-readable plain-text report for the console."""
        lines: list[str] = [
            "=" * 72,
            f"  HEALTH REPORT \u2014 {self.hostname}.{self.domain}",
            f"  {self.timestamp}",
            "=" * 72,
            "",
        ]

        self._text_updates(lines)
        self._text_containers(lines)
        self._text_resources(lines)
        self._text_storage(lines)
        self._text_certificates(lines)
        self._text_security(lines)
        self._text_maintenance(lines)

        lines.append("=" * 72)
        return "\n".join(lines)

    def _text_updates(self, lines: list[str]) -> None:
        lines.append(f"{'UPDATES':\u2501^72}")
        if self.updates.check_error:
            lines.append(f"  \u2717 Dependency check failed: {self.updates.check_error}")
            lines.append("")
            return
        if not self.updates.has_updates:
            lines.append("  \u2713 All systems up-to-date")
            lines.append("")
            return

        if self.updates.python_lock:
            lines.append(f"  Python lock ({len(self.updates.python_lock)}):")
            for p in self.updates.python_lock[:10]:
                lines.append(f"    {p['name']:<30} {p['current']:<16}\u2192 {p['latest']}")
        if self.updates.toolkit_binaries:
            lines.append(f"  Toolkit binaries ({len(self.updates.toolkit_binaries)}):")
            for t in self.updates.toolkit_binaries:
                latest = t.get("latest", "?")
                lines.append(f"    {t['name']:<30} {t.get('current', '?'):<16}\u2192 {latest}")
        if self.updates.ansible_collections:
            lines.append(f"  Ansible collections ({len(self.updates.ansible_collections)}):")
            for collection in self.updates.ansible_collections:
                lines.append(
                    f"    {collection['name']:<30} {collection.get('current', '?'):<16}"
                    f"\u2192 {collection.get('latest', '?')}"
                )
        if self.updates.old_images:
            lines.append(f"  Old images ({len(self.updates.old_images)}):")
            for img in self.updates.old_images:
                lines.append(f"    {img['name']:<30} {img['age_days']} days old")
        lines.append("")

    def _text_containers(self, lines: list[str]) -> None:
        lines.append(f"{'CONTAINERS':\u2501^72}")
        lines.append(f"  Total: {self.containers.total} | Healthy: {self.containers.healthy}")
        if self.containers.unhealthy:
            lines.append(f"  \u2717 Unhealthy ({len(self.containers.unhealthy)}):")
            for name in self.containers.unhealthy:
                lines.append(f"    - {name}")
        if self.containers.restarting:
            lines.append(f"  \u2717 Restarting ({len(self.containers.restarting)}):")
            for name in self.containers.restarting:
                lines.append(f"    - {name}")
        if self.containers.paused:
            lines.append(f"  \u23f8 Paused ({len(self.containers.paused)}):")
            for name in self.containers.paused:
                lines.append(f"    - {name}")
        if not self.containers.unhealthy and not self.containers.restarting:
            lines.append("  \u2713 All containers healthy")
        lines.append("")

    def _text_resources(self, lines: list[str]) -> None:
        lines.append(f"{'RESOURCES':\u2501^72}")
        lines.append(f"  CPU cores:  {self.resources.cpu_cores}")
        la = self.resources.load_avg
        lines.append(f"  Load avg:   {la[0]:.1f}, {la[1]:.1f}, {la[2]:.1f}")
        mem_icon = "\u26a0" if self.resources.memory_used_percent > 90 else "\u2713"
        lines.append(
            f"  Memory:     {mem_icon} {self.resources.memory_used_percent:.1f}% "
            f"of {self.resources.memory_total_gb:.1f} GB"
        )
        lines.append(f"  Swap:       {self.resources.swap_used_percent:.1f}% of {self.resources.swap_total_gb:.1f} GB")
        root_icon = "\u26a0" if self.resources.disk_root_percent > 90 else "\u2713"
        lines.append(f"  Disk /:     {root_icon} {self.resources.disk_root_percent:.1f}%")
        docker_icon = "\u26a0" if self.resources.disk_docker_percent > 90 else "\u2713"
        lines.append(f"  Docker:     {docker_icon} {self.resources.disk_docker_percent:.1f}%")
        lines.append(f"  GPU:        {self.resources.gpu}")
        lines.append("")

    def _text_storage(self, lines: list[str]) -> None:
        lines.append(f"{'STORAGE':\u2501^72}")
        if self.storage.zfs_pools:
            lines.append("  ZFS pools:")
            for pool in self.storage.zfs_pools:
                icon = "\u2713" if pool["health"] == "ONLINE" else "\u2717"
                lines.append(f"    {icon} {pool['name']:<20} {pool['size']:<8} {pool['health']}")
        else:
            lines.append("  ZFS: not configured")
        lines.append(f"  Kopia last backup: {self.storage.kopia_last_backup}")
        if self.storage.docker_logs_large:
            lines.append("  Large logs (>100 MB):")
            for entry in self.storage.docker_logs_large:
                lines.append(f"    \u26a0 {entry}")
        lines.append("")

    def _text_certificates(self, lines: list[str]) -> None:
        lines.append(f"{'CERTIFICATES':\u2501^72}")
        if self.certificates.wildcard_days_left >= 0:
            icon = "\u2713" if self.certificates.wildcard_days_left > 14 else "\u26a0"
            lines.append(f"  {icon} SSL ({self.domain}): {self.certificates.wildcard_days_left} days left")
        else:
            lines.append(f"  ? SSL ({self.domain}): could not check")
        for check in self.certificates.url_checks:
            icon = "\u2713" if check.get("ssl_ok") else "\u2717"
            lines.append(f"  {icon} {check.get('url', '?'):<50} HTTP {check.get('status_code', '?')}")
        lines.append("")

    def _text_security(self, lines: list[str]) -> None:
        lines.append(f"{'SECURITY':\u2501^72}")
        wazuh_icons = {"green": "\u2713", "yellow": "\u26a0", "red": "\u2717", "unknown": "?"}
        lines.append(f"  Wazuh:      {wazuh_icons.get(self.security.wazuh_status, '?')} {self.security.wazuh_status}")
        f2b_icon = "\u2713" if self.security.fail2ban_active else "\u2717"
        f2b_state = "active" if self.security.fail2ban_active else "inactive"
        lines.append(f"  fail2ban:   {f2b_icon} {f2b_state} ({self.security.fail2ban_jails} jails)")
        ufw_icon = "\u2713" if self.security.ufw_active else "\u2717"
        lines.append(f"  UFW:        {ufw_icon} {'active' if self.security.ufw_active else 'inactive'}")
        lines.append("")

    def _text_maintenance(self, lines: list[str]) -> None:
        lines.append(f"{'MAINTENANCE':\u2501^72}")
        lines.append(f"  Docker reclaimable: {self.maintenance.docker_prune_gb:.2f} GB")
        lines.append(f"  Journal size:       {self.maintenance.journal_size_mb:.1f} MB")
        lines.append(f"  Old log files:      {self.maintenance.old_logs_count}")
        lines.append("")

    # ── JSON format ─────────────────────────────────────────────────

    def format_json(self) -> str:
        """Render the report as a JSON string."""
        data = {
            "timestamp": self.timestamp,
            "hostname": self.hostname,
            "domain": self.domain,
            "has_issues": self.has_issues(),
            "updates": asdict(self.updates),
            "containers": asdict(self.containers),
            "resources": asdict(self.resources),
            "storage": asdict(self.storage),
            "certificates": asdict(self.certificates),
            "security": asdict(self.security),
            "maintenance": asdict(self.maintenance),
        }
        return json.dumps(data, indent=2, default=str)

    # ── HTML (email) format ─────────────────────────────────────────

    def format_html(self) -> str:
        """Render the report as an HTML email body."""
        issues_html = self._html_issues()
        updates_html = self._html_updates()
        containers_html = self._html_containers()
        resources_html = self._html_resources()
        security_html = self._html_security()

        subtitle = f"{self.hostname}.{self.domain} &middot; {self.timestamp[:10]}"
        return f"""<!DOCTYPE html>
<html>
<head><meta charset="utf-8"></head>
<body style="margin:0;padding:0;background:#f1f5f9;font-family:{_HTML_BODY_FONT};">
<table cellpadding="0" cellspacing="0" style="width:100%;max-width:640px;margin:0 auto;padding:32px 16px;">
    <tr>
        <td style="text-align:center;padding-bottom:24px;">
            <h1 style="color:#1e293b;font-size:22px;margin:0;">\U0001fa79 Homelab Health Report</h1>
            <p style="color:#64748b;font-size:14px;margin:6px 0 0;">{subtitle}</p>
        </td>
    </tr>
    {issues_html}
    {updates_html}
    {containers_html}
    {resources_html}
    {security_html}
    <tr>
        <td style="padding:24px 0 0;text-align:center;color:#94a3b8;font-size:12px;">
            <p style="margin:0;">Automated daily health report from your homelab toolkit.</p>
            <p style="margin:4px 0 0;">{self.hostname}.{self.domain}</p>
        </td>
    </tr>
</table>
</body>
</html>"""

    def _html_issues(self) -> str:
        """Generate HTML for the issues alert banner."""
        items: list[str] = []
        if self.updates.has_updates:
            items.append(f"<li>{self.updates.total} update(s) available</li>")
        if self.containers.unhealthy:
            items.append(f"<li>{len(self.containers.unhealthy)} unhealthy container(s)</li>")
        if self.containers.restarting:
            items.append(f"<li>{len(self.containers.restarting)} container(s) restarting</li>")
        if self.resources.disk_root_percent > 90:
            items.append(f"<li>Disk / at {self.resources.disk_root_percent:.0f}% capacity</li>")
        if self.resources.memory_used_percent > 90:
            items.append(f"<li>Memory at {self.resources.memory_used_percent:.0f}%</li>")
        if self.updates.check_error:
            items.append(f"<li>Framework dependency check failed: {html.escape(self.updates.check_error)}</li>")
        cert = self.certificates.wildcard_days_left
        if cert >= 0 and cert < 14:
            items.append(f"<li>SSL certificate expires in {cert} days</li>")

        if not items:
            return ""

        has_critical = any("capacity" in i or "expires" in i or "restarting" in i for i in items)
        color = "#dc2626" if has_critical else "#f59e0b"
        items_html = "".join(items)

        alert = (
            "background:#fff;border-radius:8px;padding:16px;"
            f"box-shadow:0 1px 3px rgba(0,0,0,0.08);border-left:4px solid {color};"
        )
        return f"""<tr>
    <td style="padding:0 0 24px;">
        <div style="{alert}">
            <h3 style="color:{color};font-size:15px;margin:0 0 8px;">{len(items)} issue(s) detected</h3>
            <ul style="margin:0;padding-left:20px;color:#475569;font-size:13px;">{items_html}</ul>
        </div>
    </td>
</tr>"""

    def _html_updates(self) -> str:
        """Generate HTML for the updates section."""
        if not self.updates.has_updates:
            return ""

        rows: list[str] = []
        all_items = self.updates.python_lock + self.updates.toolkit_binaries + self.updates.ansible_collections
        for item in all_items:
            latest = item.get("latest", "?")
            rows.append(f"""<tr>
                <td style="{_TD8_MONO}">{item.get("name", "?")}</td>
                <td style="{_TD8_MONO}color:#64748b;">{item.get("current", "?")}</td>
                <td style="{_TD8_MONO}color:#059669;font-weight:600;">{latest}</td>
            </tr>""")

        return f"""<tr>
    <td style="padding-bottom:24px;">
        <h3 style="color:#1e293b;font-size:16px;margin:0 0 12px;">Updates ({self.updates.total})</h3>
        <table cellpadding="0" cellspacing="0" style="{_HTML_CARD}">
            <thead>
                <tr style="background:#f8fafc;">
                    <th style="{_TH8}text-align:left;">Name</th>
                    <th style="{_TH8}text-align:left;">Current</th>
                    <th style="{_TH8}text-align:left;">Latest</th>
                </tr>
            </thead>
            <tbody>
                {"".join(rows)}
            </tbody>
        </table>
    </td>
</tr>"""

    def _html_containers(self) -> str:
        """Generate HTML for the container health section."""
        if not self.containers.issues:
            return ""

        rows: list[str] = []
        badge = "display:inline-block;padding:2px 8px;border-radius:4px;font-size:11px;font-weight:600;"
        for issue in self.containers.issues[:10]:
            color = "#dc2626" if issue["severity"] == "critical" else "#f59e0b"
            sev = f'<span style="{badge}background:{color}20;color:{color};">{issue["severity"]}</span>'
            rows.append(f"""<tr>
                <td style="{_TD8_MONO}">{issue["container"]}</td>
                <td style="{_TD8}font-size:12px;color:{color};">{issue["issue"][:60]}</td>
                <td style="{_TD8}text-align:center;">{sev}</td>
            </tr>""")

        title = f"Container Health ({self.containers.healthy}/{self.containers.total} healthy)"
        return f"""<tr>
    <td style="padding-bottom:24px;">
        <h3 style="color:#1e293b;font-size:16px;margin:0 0 12px;">{title}</h3>
        <table cellpadding="0" cellspacing="0" style="{_HTML_CARD}">
            <thead>
                <tr style="background:#f8fafc;">
                    <th style="{_TH8}text-align:left;">Container</th>
                    <th style="{_TH8}text-align:left;">Issue</th>
                    <th style="{_TH8}text-align:center;">Severity</th>
                </tr>
            </thead>
            <tbody>
                {"".join(rows)}
            </tbody>
        </table>
    </td>
</tr>"""

    def _html_resources(self) -> str:
        """Generate HTML for the system resources section."""
        mem_bar_pct = min(self.resources.memory_used_percent, 100)
        root_bar_pct = min(self.resources.disk_root_percent, 100)
        mem_color = "#dc2626" if mem_bar_pct > 90 else "#059669"
        disk_color = "#dc2626" if root_bar_pct > 90 else "#059669"
        bar = "height:8px;border-radius:4px;background:#e2e8f0;overflow:hidden;margin:4px 0;"

        mem_label = f"{self.resources.memory_used_percent:.1f}% of {self.resources.memory_total_gb:.1f} GB"
        mem_fill = f"height:100%;width:{mem_bar_pct:.0f}%;background:{mem_color};border-radius:4px;"
        disk_fill = f"height:100%;width:{root_bar_pct:.0f}%;background:{disk_color};border-radius:4px;"
        la = self.resources.load_avg
        load_label = f"{la[0]:.1f}, {la[1]:.1f}, {la[2]:.1f}"
        return f"""<tr>
    <td style="padding-bottom:24px;">
        <h3 style="color:#1e293b;font-size:16px;margin:0 0 12px;">Resources</h3>
        <table cellpadding="0" cellspacing="0" style="{_HTML_PANEL}">
            <tr>
                <td style="padding:12px 16px;border-bottom:1px solid #e2e8f0;">
                    <div style="display:flex;justify-content:space-between;font-size:13px;">
                        <span>Memory</span><span style="font-weight:600;">{mem_label}</span>
                    </div>
                    <div style="{bar}"><div style="{mem_fill}"></div></div>
                </td>
            </tr>
            <tr>
                <td style="padding:12px 16px;border-bottom:1px solid #e2e8f0;">
                    <div style="display:flex;justify-content:space-between;font-size:13px;">
                        <span>Disk /</span><span style="font-weight:600;">{self.resources.disk_root_percent:.1f}%</span>
                    </div>
                    <div style="{bar}"><div style="{disk_fill}"></div></div>
                </td>
            </tr>
            <tr>
                <td style="padding:12px 16px;font-size:13px;">
                    <div style="display:flex;justify-content:space-between;">
                        <span>CPU cores</span><span style="font-weight:600;">{self.resources.cpu_cores}</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;margin-top:4px;">
                        <span>Load avg</span><span style="font-weight:600;">{load_label}</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;margin-top:4px;">
                        <span>GPU</span><span style="font-weight:600;">{self.resources.gpu}</span>
                    </div>
                </td>
            </tr>
        </table>
    </td>
</tr>"""

    def _html_security(self) -> str:
        """Generate HTML for the security section."""
        f2b_state = "active" if self.security.fail2ban_active else "inactive"
        f2b_label = f"{f2b_state} ({self.security.fail2ban_jails} jails)"
        ufw_label = "active" if self.security.ufw_active else "inactive"
        return f"""<tr>
    <td style="padding-bottom:24px;">
        <h3 style="color:#1e293b;font-size:16px;margin:0 0 12px;">Security</h3>
        <table cellpadding="0" cellspacing="0" style="{_HTML_PANEL}">
            <tr>
                <td style="padding:10px 16px;font-size:13px;">
                    <div style="display:flex;justify-content:space-between;">
                        <span>Wazuh</span><span style="font-weight:600;">{self.security.wazuh_status}</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;margin-top:4px;">
                        <span>fail2ban</span><span style="font-weight:600;">{f2b_label}</span>
                    </div>
                    <div style="display:flex;justify-content:space-between;margin-top:4px;">
                        <span>UFW</span><span style="font-weight:600;">{ufw_label}</span>
                    </div>
                </td>
            </tr>
        </table>
    </td>
</tr>"""

    # ── Markdown (ntfy) format ──────────────────────────────────────

    def format_markdown(self) -> str:
        """Render the report as Markdown (suitable for ntfy push)."""
        lines: list[str] = [
            f"# \U0001fa79 Health Report \u2014 {self.hostname}.{self.domain}",
            f"**{self.timestamp[:10]}**",
            "",
        ]

        if self.has_issues():
            lines.append("**\u26a0 Issues detected**")
        else:
            lines.append("**\u2705 All systems healthy**")
        lines.append("")

        # Updates
        if self.updates.check_error:
            lines.append(f"**Framework dependency check failed:** {self.updates.check_error}")
            lines.append("")
        if self.updates.has_updates:
            lines.append(f"**Updates:** {self.updates.total} available")
            if self.updates.python_lock:
                lines.append(f"- Python lock: {len(self.updates.python_lock)} package(s)")
            if self.updates.toolkit_binaries:
                for t in self.updates.toolkit_binaries:
                    lines.append(f"- {t.get('name', '?')}: `{t.get('current', '?')}` \u2192 `{t.get('latest', '?')}`")
            if self.updates.ansible_collections:
                lines.append(f"- Ansible collections: {len(self.updates.ansible_collections)} update(s)")
            if self.updates.old_images:
                lines.append(f"- Old images: {len(self.updates.old_images)} container(s)")
            lines.append("")

        # Containers
        lines.append(f"**Containers:** {self.containers.healthy}/{self.containers.total} healthy")
        for name in self.containers.unhealthy:
            lines.append(f"- \u26a0 {name}: unhealthy")
        for name in self.containers.restarting:
            lines.append(f"- \U0001f504 {name}: restart loop")
        lines.append("")

        # Resources
        lines.append("**Resources:**")
        lines.append(f"- CPU: {self.resources.cpu_cores} cores, load {self.resources.load_avg[0]:.1f}")
        mem_icon = "\u26a0" if self.resources.memory_used_percent > 90 else "\u2713"
        lines.append(
            f"- Memory: {mem_icon} {self.resources.memory_used_percent:.1f}% of {self.resources.memory_total_gb:.1f} GB"
        )
        root_icon = "\u26a0" if self.resources.disk_root_percent > 90 else "\u2713"
        lines.append(f"- Disk /: {root_icon} {self.resources.disk_root_percent:.1f}%")
        lines.append(f"- GPU: {self.resources.gpu}")
        lines.append("")

        # Certificates
        cert = self.certificates.wildcard_days_left
        if cert >= 0:
            cert_icon = "\u2713" if cert > 14 else "\u26a0"
            lines.append(f"**SSL:** {cert_icon} {cert} days remaining")
        lines.append("")

        # Security
        lines.append(
            f"**Security:** Wazuh={self.security.wazuh_status}, "
            f"fail2ban={'active' if self.security.fail2ban_active else 'inactive'}, "
            f"UFW={'active' if self.security.ufw_active else 'inactive'}"
        )

        # Backup
        lines.append(f"**Backup:** Kopia last snapshot \u2014 {self.storage.kopia_last_backup}")
        lines.append("")
        lines.append(f"*{self.timestamp}*")

        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════
# Factory
# ═══════════════════════════════════════════════════════════════════════════


def create_health_report(
    root: Path,
    cfg: Config | None = None,
) -> HealthReport:
    """Run all health collectors in parallel and return a complete HealthReport.

    Args:
        root: Homelab repository root path.
        cfg: Optional Config object (provides domain, notification settings).

    Returns:
        A populated HealthReport with data from all collectors.
    """
    results = _run_collectors(root, cfg)

    domain = cfg.domain if cfg is not None else ""

    return HealthReport(
        domain=domain,
        updates=results.get("updates") or UpdatesSection(),
        containers=results.get("containers") or ContainerHealthSection(),
        resources=results.get("resources") or ResourcesSection(),
        storage=results.get("storage") or StorageSection(),
        certificates=results.get("certificates") or CertificatesSection(),
        security=results.get("security") or SecuritySection(),
        maintenance=results.get("maintenance") or MaintenanceSection(),
    )
