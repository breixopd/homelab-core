"""Shared system checks used by both watchdog and health_report.

Each helper returns structured data (dataclasses) so callers can render the
result in whichever shape they need (HealthIssue, report section, etc.).
The helpers take an optional ``run`` callable so callers can route subprocess
calls through their own executor (e.g. fleet SSH for watchdog, local
subprocess for health_report) and tests can mock execution.

Functions:
    check_cert_days_left      — TLS handshake to read cert notAfter.
    collect_stale_images      — containers running images older than N days.
    collect_large_container_logs — containers whose Docker log file exceeds N MB.
"""

from __future__ import annotations

import datetime
import email.utils
import logging
import socket
import ssl
import subprocess
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

# Default subprocess runner used when no ``run`` is supplied by the caller.
# Matches the behaviour of health_report._run / watchdog._run (capture stdout
# and stderr, never raise on failure).
_RunT = Callable[[list[str], int], subprocess.CompletedProcess]


def _default_run(cmd: list[str], timeout: int) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return subprocess.CompletedProcess(cmd, returncode=-1, stdout="", stderr=str(exc))


# ═══════════════════════════════════════════════════════════════════════════
# Dataclasses
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class StaleImage:
    """A container running an image older than the configured threshold."""

    name: str
    image: str
    age_days: int

    def to_dict(self) -> dict:
        return {"name": self.name, "image": self.image, "age_days": self.age_days}


@dataclass
class ContainerLogInfo:
    """A container whose Docker log file exceeds the size threshold."""

    name: str
    log_path: str
    size_bytes: int
    size_mb: int


# ═══════════════════════════════════════════════════════════════════════════
# SSL certificate expiry
# ═══════════════════════════════════════════════════════════════════════════


def check_cert_days_left(host: str, port: int = 443, timeout: int = 10) -> int | None:
    """Return days remaining before the TLS certificate for ``host`` expires.

    Performs a TLS handshake against ``host:port`` and parses the certificate's
    ``notAfter`` field. Returns ``None`` if the certificate cannot be inspected
    (TLS error, network failure, missing field, etc.).
    """
    try:
        ctx = ssl.create_default_context()
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        with ctx.wrap_socket(socket.socket(), server_hostname=host) as s:
            s.settimeout(timeout)
            s.connect((host, port))
            cert = s.getpeercert()
            if not cert:
                return None
            expiry_str = cert.get("notAfter", "")
            if not expiry_str or not isinstance(expiry_str, str):
                return None
            # SSL cert notAfter uses RFC 2822 format: "Jan 15 10:30:00 2025 GMT"
            parsed = email.utils.parsedate_to_datetime(expiry_str.replace("GMT", "+0000"))
            return (parsed - datetime.datetime.now(datetime.UTC)).days
    except ssl.SSLError as exc:
        logger.warning("SSL certificate error for %s: %s", host, exc)
    except (OSError, TimeoutError, ValueError):
        pass
    return None


# ═══════════════════════════════════════════════════════════════════════════
# Stale container images
# ═══════════════════════════════════════════════════════════════════════════


def collect_stale_images(
    max_age_days: int = 90,
    *,
    run: _RunT | None = None,
) -> list[StaleImage]:
    """Return containers whose image was created more than ``max_age_days`` ago.

    Thin wrapper over :func:`toolkit.core.compose.image_age.list_stale_container_images`
    that returns structured :class:`StaleImage` records instead of plain dicts.
    """
    from toolkit.core.compose.image_age import list_stale_container_images

    _run = run or _default_run
    try:
        raw = list_stale_container_images(
            max_age_days=max_age_days,
            run=lambda cmd, timeout: _run(cmd, timeout),
        )
    except (subprocess.TimeoutExpired, FileNotFoundError):
        return []
    return [StaleImage(name=item["name"], image=item["image"], age_days=item["age_days"]) for item in raw]


# ═══════════════════════════════════════════════════════════════════════════
# Large Docker container logs
# ═══════════════════════════════════════════════════════════════════════════


def collect_large_container_logs(
    threshold_mb: int = 100,
    *,
    run: _RunT | None = None,
) -> list[ContainerLogInfo]:
    """Return containers whose Docker log file exceeds ``threshold_mb``.

    Iterates running container IDs via ``docker ps -q --no-trunc`` and inspects
    each container's ``LogPath``. File size is collected through the supplied
    runner as well, allowing the complete check to execute on a remote node.
    """
    _run = run or _default_run
    threshold_bytes = threshold_mb * 1024 * 1024
    large: list[ContainerLogInfo] = []

    try:
        ps = _run(["docker", "ps", "-q", "--no-trunc"], 10)
        if ps.returncode != 0:
            return large
        for cid in ps.stdout.strip().splitlines():
            if not cid:
                continue
            try:
                inspect = _run(
                    ["docker", "inspect", "--format", "{{.LogPath}}\t{{.Name}}", cid],
                    10,
                )
                if inspect.returncode != 0:
                    continue
                parts = inspect.stdout.strip().split("\t", 1)
                if len(parts) < 2:
                    continue
                log_path, name = parts
                name = name.lstrip("/")
                if not log_path:
                    continue
                stat = _run(["stat", "--format=%s", "--", log_path], 10)
                if stat.returncode != 0:
                    continue
                size = int(stat.stdout.strip())
                if size > threshold_bytes:
                    large.append(
                        ContainerLogInfo(
                            name=name,
                            log_path=log_path,
                            size_bytes=size,
                            size_mb=size // (1024 * 1024),
                        )
                    )
            except (subprocess.TimeoutExpired, FileNotFoundError, OSError, ValueError):
                continue
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass
    return large
