"""Auto-detect host environment settings to reduce manual configuration."""

from __future__ import annotations

import os
import socket
import subprocess
from pathlib import Path

from toolkit.core.config.config import DEFAULT_PROXMOX_NODE
from toolkit.core.infra.proxmox import discover_machine_ips


def detect_timezone() -> str:
    """Detect the host timezone. Falls back to UTC."""
    # 1. TZ env var
    tz = os.environ.get("TZ", "")
    if tz:
        return tz

    # 2. /etc/timezone (Debian/Ubuntu)
    try:
        with open("/etc/timezone") as fh:
            tz = fh.read().strip()
            if tz:
                return tz
    except OSError:
        pass

    # 3. timedatectl (systemd)
    try:
        result = subprocess.run(
            ["timedatectl", "show", "--property=Timezone", "--value"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    # 4. /etc/localtime symlink
    try:
        link = os.readlink("/etc/localtime")
        # Typical: /usr/share/zoneinfo/Region/City
        if "zoneinfo/" in link:
            return link.split("zoneinfo/", 1)[1]
    except OSError:
        pass

    return "UTC"


# Proxmox unprivileged LXC: container UID 0 maps to host 100000 (see lxc.idmap).
DEFAULT_LXC_IDMAP_BASE = 100_000


def host_uid_for_container(container_uid: int, *, idmap_base: int = DEFAULT_LXC_IDMAP_BASE) -> int:
    """Map a container UID to the owning UID on an unprivileged LXC host."""
    return idmap_base + container_uid


def detect_uid_gid() -> tuple[int, int]:
    """Detect UID/GID for bind-mount ownership on the LXC host (idmapped when root)."""
    uid = os.getuid()
    gid = os.getgid()
    if uid == 0:
        uid = host_uid_for_container(1000)
        gid = host_uid_for_container(1000)
    return uid, gid


def detect_compose_uid_gid() -> tuple[int, int]:
    """PUID/PGID for LinuxServer-style containers (in-container users, not host idmap)."""
    uid = os.getuid()
    gid = os.getgid()
    if uid == 0:
        return 1000, 1000
    return uid, gid


def detect_gateway(private_subnet: str = "10.10.10") -> str:
    """Detect the network gateway for the private subnet."""
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            # Parse: "default via 10.10.10.1 dev eth0 ..."
            for line in result.stdout.strip().splitlines():
                parts = line.split()
                if "via" in parts:
                    idx = parts.index("via") + 1
                    if idx < len(parts):
                        gw = parts[idx]
                        if gw.startswith(private_subnet):
                            return gw
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return f"{private_subnet}.1"


def detect_hw_transcoding() -> str:
    """Detect the most likely hardware transcoding backend on this host."""
    if os.path.exists("/dev/nvidiactl") or os.path.exists("/dev/nvidia0"):
        return "nvidia"

    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0 and result.stdout.strip():
            return "nvidia"
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass

    if os.path.exists("/dev/dri") and bool(os.listdir("/dev/dri")):
        return "vaapi"

    return "none"


def detect_public_ip() -> str:
    """Best-effort detection of the current machine's public IPv4 address."""
    import ipaddress
    import urllib.error
    import urllib.request

    services = [
        "https://api.ipify.org",
        "https://ifconfig.me/ip",
        "https://icanhazip.com",
    ]
    for url in services:
        try:
            with urllib.request.urlopen(url, timeout=5) as response:  # noqa: S310
                candidate = response.read().decode().strip()
            ip = ipaddress.ip_address(candidate)
            if ip.version == 4:
                return str(ip)
        except (ValueError, urllib.error.URLError, OSError):
            continue
    return ""


def detect_ssh_public_key() -> tuple[str, str]:
    """Return the first likely local SSH public key and its path."""
    candidates = [
        Path.home() / ".ssh" / "id_ed25519.pub",
        Path.home() / ".ssh" / "id_ecdsa.pub",
        Path.home() / ".ssh" / "id_rsa.pub",
    ]
    for path in candidates:
        try:
            content = path.read_text().strip()
        except OSError:
            continue
        if content.startswith(("ssh-ed25519 ", "ecdsa-", "ssh-rsa ")):
            return content, str(path)
    return "", ""


def detect_docker_available() -> bool:
    """Check if Docker is available on the host."""
    try:
        result = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def detect_compose_available() -> bool:
    """Check if Docker Compose is available."""
    try:
        result = subprocess.run(
            ["docker", "compose", "version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        return result.returncode == 0
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return False


def discover_proxmox_machines(
    api_url: str,
    token_id: str,
    token_secret: str,
    node: str = DEFAULT_PROXMOX_NODE,
    *,
    verify_ssl: bool = False,
) -> dict[str, str]:
    """Query Proxmox and discover configured machine IPs by VMID or hostname."""
    return discover_machine_ips(
        api_url,
        token_id,
        token_secret,
        node,
        verify_ssl=verify_ssl,
    )


def check_vm_reachable(ip: str, port: int = 22, timeout: int = 5) -> bool:
    """Check if a VM is reachable via SSH port."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return True
    except (OSError, TimeoutError):
        return False
