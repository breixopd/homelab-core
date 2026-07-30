from __future__ import annotations

import logging
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Literal

from toolkit.core.config.config import DEFAULT_PROXMOX_NODE
from toolkit.core.machines import MachineSpec, load_default_machines

logger = logging.getLogger(__name__)

# Retry configuration for _api_get_with_retry
_RETRY_ATTEMPTS = 3
_RETRY_DELAYS = (2, 4, 8)  # seconds between retries


@dataclass(slots=True)
class ProxmoxVM:
    vmid: int | None
    name: str
    status: str
    machine_id: str | None = None
    ip: str = ""
    is_template: bool = False
    type: str = "qemu"  # "qemu" or "lxc"


@dataclass(slots=True)
class ProxmoxPreflight:
    ok: bool
    base_url: str
    node: str
    recommendation: Literal["provision", "existing", "mixed"]
    message: str
    existing_machines: dict[str, ProxmoxVM] = field(default_factory=dict)
    missing_machines: list[str] = field(default_factory=list)


def normalize_proxmox_token(token_id: str, token_secret: str = "") -> tuple[str, str]:
    """Normalize either a full Proxmox token or separate id/secret values."""
    token_id = token_id.strip()
    token_secret = token_secret.strip()
    if token_id and "=" in token_id and not token_secret:
        token_id, token_secret = token_id.split("=", 1)
    return token_id.strip(), token_secret.strip()


def validate_proxmox_url(api_url: str) -> str:
    """Validate and normalize a Proxmox API URL to the /api2/json base."""
    from urllib.parse import urlparse

    parsed = urlparse(api_url)
    if parsed.scheme not in ("https", "http"):
        raise ValueError(f"Proxmox URL must use https:// or http:// scheme, got: {parsed.scheme or 'none'}")
    if not parsed.hostname:
        raise ValueError("Proxmox URL must include a hostname or IP address")
    base = f"{parsed.scheme}://{parsed.netloc}"
    if not base.rstrip("/").endswith("/api2/json"):
        base = base.rstrip("/") + "/api2/json"
    return base


def _build_auth_value(token_id: str, token_secret: str) -> str:
    token_id, token_secret = normalize_proxmox_token(token_id, token_secret)
    if not token_id:
        raise ValueError("Proxmox API token id is required")
    if "=" in token_id:
        return f"PVEAPIToken={token_id}"
    if not token_secret:
        raise ValueError("Proxmox API token secret is required")
    return f"PVEAPIToken={token_id}={token_secret}"


def _ssl_context(verify_ssl: bool, *, ca_file: str | None = None):
    import ssl

    ctx = ssl.create_default_context(cafile=ca_file)
    if not verify_ssl:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _api_get(
    base_url: str,
    path: str,
    auth_value: str,
    *,
    verify_ssl: bool,
    ca_file: str | None = None,
) -> dict:
    """Low-level Proxmox API GET. Does a single request and checks for error responses."""
    import json
    import urllib.request

    ctx = _ssl_context(verify_ssl, ca_file=ca_file)
    req = urllib.request.Request(
        f"{base_url}/{path.lstrip('/')}",
        headers={"Authorization": auth_value},
    )
    resp = urllib.request.urlopen(req, timeout=10, context=ctx)  # noqa: S310
    body = json.loads(resp.read().decode())
    if body.get("data") is None:
        logger.warning("Proxmox API returned no data for %s: %s", path, body)
        errors = body.get("errors")
        if errors:
            logger.error("Proxmox API errors for %s: %s", path, errors)
            # Propagate API-level errors so callers can detect them via .get("error")
            body["error"] = True
            body["message"] = str(errors)
    return body


def _api_get_with_retry(
    base_url: str,
    path: str,
    auth_value: str,
    *,
    verify_ssl: bool,
    ca_file: str | None = None,
) -> dict:
    """Call _api_get with exponential backoff retry for transient failures."""
    import json
    import urllib.error

    last_error: Exception | None = None
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            if ca_file is not None:
                return _api_get(base_url, path, auth_value, verify_ssl=verify_ssl, ca_file=ca_file)
            return _api_get(base_url, path, auth_value, verify_ssl=verify_ssl)
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
            last_error = exc
            logger.warning(
                "Proxmox API request failed (attempt %d/%d) for %s: %s",
                attempt + 1,
                _RETRY_ATTEMPTS,
                path,
                exc,
            )
            if attempt < _RETRY_ATTEMPTS - 1:
                time.sleep(_RETRY_DELAYS[attempt])
    return {"error": True, "message": f"Proxmox API request failed after {_RETRY_ATTEMPTS} attempts: {last_error}"}


def list_proxmox_nodes(
    api_url: str,
    token_id: str,
    token_secret: str,
    *,
    verify_ssl: bool = True,
) -> list[dict]:
    """Return nodes known to the Proxmox cluster."""
    base_url = validate_proxmox_url(api_url)
    auth_value = _build_auth_value(token_id, token_secret)
    data = _api_get(base_url, "nodes", auth_value, verify_ssl=verify_ssl)
    return list(data.get("data", []))


def choose_proxmox_node(
    api_url: str,
    token_id: str,
    token_secret: str,
    preferred_node: str = DEFAULT_PROXMOX_NODE,
    *,
    verify_ssl: bool = True,
) -> tuple[str, str]:
    """Pick the most likely node, preferring an explicit valid node when possible.

    Validates that the returned node actually exists in the discovered node list.
    """
    nodes = list_proxmox_nodes(api_url, token_id, token_secret, verify_ssl=verify_ssl)
    all_names = {str(node.get("node", "")).strip() for node in nodes if node.get("node")}
    online_names = {
        str(node.get("node", "")).strip() for node in nodes if str(node.get("status", "")).lower() == "online"
    }

    if preferred_node and preferred_node in all_names:
        return preferred_node, "preferred"

    if len(online_names) == 1:
        return next(iter(online_names)), "auto-single-online"

    if len(all_names) == 1:
        return next(iter(all_names)), "auto-single"

    # Fall back to first online node if preferred doesn't exist
    if online_names:
        return sorted(online_names)[0], "auto-first-online"

    # Last resort: return the first known node even if offline
    if all_names:
        return sorted(all_names)[0], "auto-first-any"

    logger.warning("No Proxmox nodes discovered; falling back to preferred=%s", preferred_node)
    return preferred_node or DEFAULT_PROXMOX_NODE, "fallback"


def list_proxmox_disks(
    api_url: str,
    token_id: str,
    token_secret: str,
    node: str = DEFAULT_PROXMOX_NODE,
    *,
    verify_ssl: bool = True,
) -> list[dict]:
    """Return disk inventory for a Proxmox node."""
    base_url = validate_proxmox_url(api_url)
    auth_value = _build_auth_value(token_id, token_secret)
    data = _api_get(base_url, f"nodes/{node}/disks/list", auth_value, verify_ssl=verify_ssl)
    return list(data.get("data", []))


def list_proxmox_lxcs(
    api_url: str,
    token_id: str,
    token_secret: str,
    node: str = DEFAULT_PROXMOX_NODE,
    *,
    verify_ssl: bool = True,
    ca_file: str | None = None,
) -> list[dict]:
    """Return the authoritative current LXC inventory for one Proxmox node."""
    base_url = validate_proxmox_url(api_url)
    auth_value = _build_auth_value(token_id, token_secret)
    data = _api_get_with_retry(
        base_url,
        f"nodes/{node}/lxc",
        auth_value,
        verify_ssl=verify_ssl,
        ca_file=ca_file,
    )
    if data.get("error") or not isinstance(data.get("data"), list):
        raise RuntimeError(f"Proxmox LXC inventory failed: {data.get('message', 'invalid response')}")
    return list(data["data"])


def list_proxmox_vms(
    api_url: str,
    token_id: str,
    token_secret: str,
    node: str = DEFAULT_PROXMOX_NODE,
    *,
    verify_ssl: bool = True,
    ca_file: str | None = None,
) -> list[dict]:
    """Return the authoritative current QEMU VM inventory for one Proxmox node."""
    base_url = validate_proxmox_url(api_url)
    auth_value = _build_auth_value(token_id, token_secret)
    data = _api_get_with_retry(
        base_url,
        f"nodes/{node}/qemu",
        auth_value,
        verify_ssl=verify_ssl,
        ca_file=ca_file,
    )
    if data.get("error") or not isinstance(data.get("data"), list):
        raise RuntimeError(f"Proxmox VM inventory failed: {data.get('message', 'invalid response')}")
    return list(data["data"])


def suggest_zfs_disk_devices(
    api_url: str,
    token_id: str,
    token_secret: str,
    node: str = DEFAULT_PROXMOX_NODE,
    *,
    verify_ssl: bool = True,
) -> list[str]:
    """Suggest raw disk device names suitable for creating a ZFS pool."""

    def _device_name(disk: dict) -> str:
        raw = str(
            disk.get("devpath")
            or disk.get("by_id_link")
            or disk.get("devname")
            or disk.get("device")
            or disk.get("name")
            or ""
        ).strip()
        if raw.startswith("/dev/"):
            raw = raw[5:]
        return raw

    def _is_candidate(disk: dict) -> bool:
        device = _device_name(disk)
        if not device or device.startswith(("loop", "sr", "md", "dm-", "zd")):
            return False

        used = str(disk.get("used") or "").strip().lower()
        if used and used not in {"0", "no", "false", "unused", "none"}:
            return False

        if disk.get("mounted") or disk.get("osdid") is not None:
            return False

        partitions = disk.get("partitions")
        if isinstance(partitions, list) and partitions:
            return False
        if isinstance(partitions, int) and partitions > 0:
            return False

        filesystem = str(disk.get("filesystem") or disk.get("fs") or "").strip().lower()
        if filesystem and filesystem not in {"", "unknown"}:
            return False

        return True

    disks = list_proxmox_disks(api_url, token_id, token_secret, node, verify_ssl=verify_ssl)
    candidates = [_device_name(disk) for disk in disks if _is_candidate(disk)]
    # Keep original API order but remove duplicates.
    return list(dict.fromkeys(candidates))


def _detect_vm_ip(base_url: str, node: str, vmid: int, auth_value: str, *, verify_ssl: bool) -> str:
    import json
    import re
    import urllib.error

    try:
        iface_data = _api_get(
            base_url,
            f"nodes/{node}/qemu/{vmid}/agent/network-get-interfaces",
            auth_value,
            verify_ssl=verify_ssl,
        )
        interfaces = iface_data.get("data", {}).get("result", [])
        for iface in interfaces:
            if iface.get("name") == "lo":
                continue
            for addr in iface.get("ip-addresses", []):
                ip = addr.get("ip-address", "")
                if ip and ":" not in ip and ip != "127.0.0.1":
                    return ip
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
        pass

    try:
        cfg_data = _api_get(
            base_url,
            f"nodes/{node}/qemu/{vmid}/config",
            auth_value,
            verify_ssl=verify_ssl,
        )
        ipconfig = cfg_data.get("data", {}).get("ipconfig0", "")
        match = re.search(r"ip=(\d+\.\d+\.\d+\.\d+)", ipconfig)
        if match:
            return match.group(1)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
        pass
    return ""


def detect_lxc_ip(base_url: str, node: str, vmid: int, auth_value: str, *, verify_ssl: bool) -> str:
    """Extract the IPv4 address from an LXC container's network config.

    Reads the container config via /lxc/{vmid}/config and parses net0 for the IP.
    """
    import json
    import re
    import urllib.error

    try:
        cfg_data = _api_get(
            base_url,
            f"nodes/{node}/lxc/{vmid}/config",
            auth_value,
            verify_ssl=verify_ssl,
        )
        net_config = cfg_data.get("data", {}).get("net0", "")
        # Typical net0: name=eth0,bridge=vmbr1,ip=10.10.10.10/24,gw=10.10.10.1
        match = re.search(r"ip=(\d+\.\d+\.\d+\.\d+)", net_config)
        if match:
            return match.group(1)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError):
        pass
    return ""


def inspect_proxmox_host(
    api_url: str,
    token_id: str,
    token_secret: str,
    node: str = DEFAULT_PROXMOX_NODE,
    *,
    verify_ssl: bool = True,
    machines: Mapping[str, MachineSpec] | None = None,
) -> ProxmoxPreflight:
    """Inspect a Proxmox node and decide whether to provision or reuse machines."""
    import json
    import urllib.error

    base_url = validate_proxmox_url(api_url)
    auth_value = _build_auth_value(token_id, token_secret)

    try:
        status_data = _api_get_with_retry(base_url, f"nodes/{node}/status", auth_value, verify_ssl=verify_ssl)
        if status_data.get("error"):
            return ProxmoxPreflight(
                False,
                base_url,
                node,
                "provision",
                f"Node status check failed: {status_data.get('message', 'unknown error')}",
            )
        vm_data = _api_get_with_retry(base_url, f"nodes/{node}/qemu", auth_value, verify_ssl=verify_ssl)
        lxc_data = _api_get_with_retry(base_url, f"nodes/{node}/lxc", auth_value, verify_ssl=verify_ssl)
        if vm_data.get("error"):
            return ProxmoxPreflight(
                False,
                base_url,
                node,
                "provision",
                f"Failed to list QEMU VMs: {vm_data.get('message', 'unknown error')}",
            )
        if lxc_data.get("error"):
            return ProxmoxPreflight(
                False,
                base_url,
                node,
                "provision",
                f"Failed to list LXCs: {lxc_data.get('message', 'unknown error')}",
            )
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="ignore").strip()
        message = detail or f"HTTP {exc.code} while talking to the Proxmox API"
        return ProxmoxPreflight(False, base_url, node, "provision", message)
    except (urllib.error.URLError, OSError, json.JSONDecodeError, KeyError, ValueError) as exc:
        return ProxmoxPreflight(False, base_url, node, "provision", str(exc))

    desired_machines = {
        machine_id: machine
        for machine_id, machine in sorted(
            (load_default_machines() if machines is None else machines).items(),
            key=lambda item: (item[1].startup_order, item[0]),
        )
        if machine.enabled
    }
    existing_machines: dict[str, ProxmoxVM] = {}

    def _process_containers(container_list: list[dict], container_type: str) -> None:
        ip_fn = detect_lxc_ip if container_type == "lxc" else _detect_vm_ip
        for raw in container_list:
            name = str(raw.get("name", ""))
            lowered = name.lower()
            vmid_raw = raw.get("vmid")
            vmid = int(vmid_raw) if vmid_raw is not None else None
            status = str(raw.get("status", ""))
            is_template = bool(raw.get("template"))
            if is_template:
                continue

            for machine_id, machine in desired_machines.items():
                name_match = lowered == machine.hostname or bool(
                    re.search(rf"(?:^|[^a-z0-9]){re.escape(machine_id)}(?:[^a-z0-9]|$)", lowered)
                )
                if (vmid == machine.vmid or name_match) and machine_id not in existing_machines:
                    ip = ip_fn(base_url, node, vmid, auth_value, verify_ssl=verify_ssl) if vmid is not None else ""
                    existing_machines[machine_id] = ProxmoxVM(
                        vmid=vmid,
                        name=name,
                        status=status,
                        machine_id=machine_id,
                        ip=ip,
                        is_template=is_template,
                        type=container_type,
                    )
                    break

    _process_containers(vm_data.get("data", []), "qemu")
    _process_containers(lxc_data.get("data", []), "lxc")

    missing_machines = [machine_id for machine_id in desired_machines if machine_id not in existing_machines]
    if existing_machines and not missing_machines:
        recommendation: Literal["provision", "existing", "mixed"] = "existing"
        message = f"Found all {len(existing_machines)} desired machines on node {node}; reuse them and auto-fill IPs."
    elif existing_machines:
        recommendation = "mixed"
        message = (
            f"Found {len(existing_machines)} existing machines on node {node}, but still missing: "
            f"{', '.join(missing_machines)}."
        )
    else:
        recommendation = "provision"
        message = f"No desired machines exist on node {node}; OpenTofu can provision them."

    return ProxmoxPreflight(
        ok=True,
        base_url=base_url,
        node=node,
        recommendation=recommendation,
        message=message,
        existing_machines=existing_machines,
        missing_machines=missing_machines,
    )


def discover_machine_ips(
    api_url: str,
    token_id: str,
    token_secret: str,
    node: str = DEFAULT_PROXMOX_NODE,
    *,
    verify_ssl: bool = True,
) -> dict[str, str]:
    """Return discovered desired-machine IPs from Proxmox."""
    report = inspect_proxmox_host(api_url, token_id, token_secret, node, verify_ssl=verify_ssl)
    if not report.ok:
        return {}
    return {machine_id: vm.ip for machine_id, vm in report.existing_machines.items() if vm.ip}


def lxc_network_config(
    lxc_name: str,
    ip_address: str,
    gateway: str,
    cidr: int = 24,
    bridge: str = "vmbr1",
) -> dict:
    """Return a BPG provider network block dict for an LXC container."""
    return {
        "name": lxc_name,
        "bridge": bridge,
        "ip": f"{ip_address}/{cidr}",
        "gw": gateway,
    }


def lxc_features_config(*, for_tofu: bool = False) -> dict:
    """Return LXC feature flags for Docker-in-LXC.

    Proxmox API tokens may set ``nesting`` at create time but not ``keyctl``
    (403 unless root@pam). Tofu provisions with nesting only; Ansible
    ``configure-lxc-features.yml`` applies keyctl via ``pct set`` on the host.
    """
    features = {"nesting": True}
    if not for_tofu:
        features["keyctl"] = True
    return features
