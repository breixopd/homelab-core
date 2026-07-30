"""Komodo Core API helpers for fleet onboarding (server group assignment)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from toolkit.core.config.storage import secrets_path
from toolkit.core.ops.automation import docker_exec
from toolkit.core.secrets.secrets import load_secrets_plaintext

_KOMODO_API_BASE = "http://127.0.0.1:9120"
_PERIPHERY_POLL_SECONDS = 60
_PERIPHERY_POLL_INTERVAL = 5


def _komodo_request(
    root: Path,
    endpoint: str,
    payload: dict,
    *,
    jwt: str = "",
) -> tuple[int, dict | list | None]:
    """Call Komodo Core API via docker exec on infra.

    Authenticates with X-Api-Key + X-Api-Secret (both required by Komodo Core).
    Returns (rc, parsed_json_or_None).
    """
    from toolkit.core.net.curl_config import render_curl_config

    secrets = load_secrets_plaintext(secrets_path(root))
    api_key = secrets.get("KOMODO_API_KEY", "").strip()
    api_secret = secrets.get("KOMODO_API_SECRET", "").strip()
    if not api_key or not api_secret:
        return 0, None

    headers = {
        "Content-Type": "application/json",
        "X-Api-Key": api_key,
        "X-Api-Secret": api_secret,
    }
    if jwt:
        headers["Authorization"] = jwt
    request_config = render_curl_config(
        f"{_KOMODO_API_BASE}{endpoint}",
        method="POST",
        headers=headers,
        body=json.dumps(payload, separators=(",", ":")),
        timeout=30,
    )

    rc, out = docker_exec("komodo-core", ["curl", "--disable", "--config", "-"], stdin=request_config)
    if rc != 0:
        return rc, None
    try:
        return 0, json.loads(out or "{}")
    except json.JSONDecodeError:
        return rc, None


def _ensure_server_group(root: Path, group_name: str, jwt: str = "") -> str | None:
    """Return the group ID for `group_name`, creating it if missing. None on failure."""
    rc, groups = _komodo_request(root, "/read/ListServerGroups", {"type": "ListServerGroups"}, jwt=jwt)
    if rc == 0 and isinstance(groups, list):
        for row in groups:
            if str(row.get("name", "")) == group_name:
                return str(row.get("id") or "")
    # CreateServerGroup payload: { "name": group_name }
    rc, resp = _komodo_request(
        root,
        "/write",
        {"type": "CreateServerGroup", "params": {"name": group_name}},
        jwt=jwt,
    )
    if rc == 0 and isinstance(resp, dict):
        return str(resp.get("id") or resp.get("group_id") or "")
    return None


def _find_server_id(root: Path, server_name: str, jwt: str = "") -> str | None:
    rc, servers = _komodo_request(root, "/read/ListServers", {"type": "ListServers"}, jwt=jwt)
    if rc != 0 or not isinstance(servers, list):
        return None
    for row in servers:
        if str(row.get("name", "")) == server_name or str(row.get("id", "")) == server_name:
            return str(row.get("id") or row.get("name") or "")
    return None


def _wait_for_server(root: Path, server_name: str, timeout: int = _PERIPHERY_POLL_SECONDS) -> str | None:
    """Poll Komodo Core until Periphery registers, or timeout. Returns server_id or None."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        server_id = _find_server_id(root, server_name)
        if server_id:
            return server_id
        time.sleep(_PERIPHERY_POLL_INTERVAL)
    return None


def assign_server_cluster_group(root: Path, server_name: str, group_name: str) -> list[str]:
    """Assign a Komodo server to a server group by name (best-effort API).

    Polls for Periphery registration (up to 60s), creates the group if missing,
    then updates the server config with the group ID. Komodo's ServerConfig uses
    `server_groups: Vec<String>` (a list of group IDs), so we set it as a list.
    """
    logs: list[str] = []
    group_name = (group_name or "").strip()
    server_name = (server_name or "").strip()
    if not group_name or not server_name:
        return logs

    secrets = load_secrets_plaintext(secrets_path(root))
    if not secrets.get("KOMODO_API_KEY", "").strip() or not secrets.get("KOMODO_API_SECRET", "").strip():
        logs.append(
            "Komodo: API key not provisioned yet — run `homelab-toolkit deploy hooks` "
            "or set KOMODO_API_KEY/KOMODO_API_SECRET via `secrets set`"
        )
        return logs

    server_id = _wait_for_server(root, server_name)
    if not server_id:
        logs.append(
            f"Komodo: server '{server_name}' not registered after "
            f"{_PERIPHERY_POLL_SECONDS}s — assign group '{group_name}' in UI after Periphery connects"
        )
        return logs

    group_id = _ensure_server_group(root, group_name)
    if not group_id:
        logs.append(f"Komodo: could not create/find group '{group_name}' — assign in UI")
        return logs

    # Komodo ServerConfig.server_groups is Vec<String> of group IDs.
    update_payload = {
        "type": "UpdateServer",
        "params": {
            "id": server_id,
            "config": {"server_groups": [group_id]},
        },
    }
    rc, _ = _komodo_request(root, "/write/UpdateServer", update_payload)
    if rc == 0:
        logs.append(f"Komodo: assigned server '{server_name}' to group '{group_name}'")
    else:
        logs.append(f"Komodo: API assign failed — set group '{group_name}' for '{server_name}' in Komodo UI")
    return logs
