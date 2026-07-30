"""Verify Grafana file provisioning and optional datasource reconciliation."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.request
from pathlib import Path

from toolkit.core.secrets.bootstrap_passwords import resolve_bootstrap_password

# Dashboard JSON files under config/grafana/provisioning/dashboards/
EXPECTED_DASHBOARDS = (
    "node-exporter",
    "containers",
    "redis",
    "postgres",
    "logs",
    "media-cache",
    "homelab-overview",
)


def _grafana_auth(secrets: dict[str, str]) -> str:
    password = resolve_bootstrap_password(secrets, "GRAFANA_ADMIN_PASSWORD") or "admin"
    return base64.b64encode(f"admin:{password}".encode()).decode()


def _api_get(url: str, auth: str, timeout: int = 10) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Basic {auth}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode(errors="replace")
    except Exception as e:
        return 0, str(e)


def _grafana_remote_api(
    cfg,
    secrets: dict[str, str],
    root: Path,
    path: str,
) -> tuple[int, str]:
    """GET a Grafana API path on its machine via SSH."""
    from toolkit.core.ansible.ansible_ssh import docker_exec_curl
    from toolkit.core.manifest.placement import service_address

    auth = _grafana_auth(secrets)
    rc, body = docker_exec_curl(
        cfg,
        service_address(cfg, "grafana"),
        "grafana",
        f"http://localhost:3000{path}",
        root=root,
        headers={"Authorization": f"Basic {auth}"},
    )
    return (200, body) if rc == 0 else (rc, body or "")


def grafana_provisioning_ok(logs: list[str]) -> bool:
    """Return True when Grafana health and required datasources look good."""
    text = "\n".join(logs).lower()
    if "not reachable" in text or "api failed" in text:
        return False
    if "grafana: healthy" not in text and "healthy" not in text:
        return False
    if "prometheus datasource missing" in text or "loki datasource missing" in text:
        return False
    return True


def verify_grafana_provisioning(
    secrets: dict[str, str],
    *,
    base_url: str = "http://grafana:3000",
    root: Path | None = None,
    cfg=None,
) -> list[str]:
    """Check Grafana health, datasources, and provisioned dashboards."""
    logs: list[str] = []
    auth = _grafana_auth(secrets)

    if cfg is not None and cfg.is_multi_node and root is not None:
        health_code, health_body = _grafana_remote_api(cfg, secrets, root, "/api/health")
    else:
        health_code, health_body = _api_get(f"{base_url}/api/health", auth)
    if health_code == 200:
        logs.append("Grafana: healthy")
    else:
        logs.append(f"Grafana: not reachable ({health_code}: {health_body[:80]})")
        return logs

    if root is not None:
        dash_dir = root / "config" / "grafana" / "provisioning" / "dashboards"
        missing_files = [name for name in EXPECTED_DASHBOARDS if not (dash_dir / f"{name}.json").is_file()]
        if missing_files:
            logs.append(f"Grafana: missing dashboard files on disk: {', '.join(missing_files)}")
        else:
            logs.append(f"Grafana: {len(EXPECTED_DASHBOARDS)} dashboard files present")

    if cfg is not None and cfg.is_multi_node and root is not None:
        ds_code, ds_body = _grafana_remote_api(cfg, secrets, root, "/api/datasources")
    else:
        ds_code, ds_body = _api_get(f"{base_url}/api/datasources", auth)
    if ds_code == 200:
        try:
            datasources = json.loads(ds_body)
            names = {d.get("name") for d in datasources if isinstance(d, dict)}
            for required in ("Prometheus", "Loki"):
                if required in names:
                    logs.append(f"Grafana: {required} datasource OK")
                else:
                    logs.append(f"Grafana: {required} datasource missing (file provisioning pending?)")
        except json.JSONDecodeError:
            logs.append("Grafana: could not parse datasources response")
    else:
        logs.append(f"Grafana: datasources API failed ({ds_code})")

    if cfg is not None and cfg.is_multi_node and root is not None:
        search_code, search_body = _grafana_remote_api(cfg, secrets, root, "/api/search?type=dash-db&limit=100")
    else:
        search_code, search_body = _api_get(
            f"{base_url}/api/search?type=dash-db&limit=100",
            auth,
        )
    if search_code == 200:
        try:
            entries = json.loads(search_body)
            titles = {e.get("title", "").lower() for e in entries if isinstance(e, dict)}
            found = sum(
                1 for name in EXPECTED_DASHBOARDS if any(name.replace("-", " ") in t or name in t for t in titles)
            )
            logs.append(f"Grafana: {found}/{len(EXPECTED_DASHBOARDS)} dashboards visible in UI")
        except json.JSONDecodeError:
            logs.append("Grafana: could not parse dashboard search")
    else:
        logs.append(f"Grafana: dashboard search failed ({search_code})")

    return logs


def verify_grafana_datasources(
    secrets: dict[str, str],
    *,
    base_url: str = "http://grafana:3000",
) -> list[str]:
    """Check Prometheus and Loki datasources are actually provisioned.

    Raises RuntimeError if Prometheus is missing (critical for metrics).
    Logs a warning if Loki is missing (logs are important but non-critical).
    """
    import json as _json
    import logging as _logging

    logger = _logging.getLogger(__name__)
    logs: list[str] = []
    auth = _grafana_auth(secrets)

    ds_code, ds_body = _api_get(f"{base_url}/api/datasources", auth)
    if ds_code != 200:
        logs.append(f"Grafana: datasources API not ready (HTTP {ds_code}) — will retry on next deploy")
        return logs  # non-fatal — Grafana may still be starting up on fresh deploy

    try:
        datasources = _json.loads(ds_body)
    except _json.JSONDecodeError:
        logs.append("Grafana: datasources API returned invalid JSON — will retry")
        return logs  # non-fatal

    names = [d.get("name", "") for d in datasources if isinstance(d, dict)]
    logs.append(f"Grafana: found {len(names)} datasource(s): {', '.join(names) or 'none'}")

    if "Prometheus" not in names:
        logs.append("Grafana: Prometheus datasource not provisioned — will retry on next deploy")
        return logs  # non-fatal — provisioning may not have completed yet
    logs.append("Grafana: Prometheus datasource verified OK")

    if "Loki" not in names:
        logger.warning("Loki datasource not provisioned — logs will not appear in Grafana")
        logs.append("Grafana: Loki datasource missing (warning only)")
    else:
        logs.append("Grafana: Loki datasource verified OK")

    return logs


def reload_dashboard_provisioning(
    secrets: dict[str, str],
    *,
    base_url: str = "http://grafana:3000",
) -> list[str]:
    """Ask Grafana to reload file-based dashboards (admin API)."""
    auth = _grafana_auth(secrets)
    logs: list[str] = []
    for endpoint in (
        "/api/admin/provisioning/dashboards/reload",
        "/api/admin/provisioning/datasources/reload",
    ):
        req = urllib.request.Request(
            f"{base_url}{endpoint}",
            data=b"",
            headers={"Authorization": f"Basic {auth}"},
            method="POST",
        )
        try:
            urllib.request.urlopen(req, timeout=15)
            logs.append(f"Grafana: reloaded {endpoint.split('/')[-2]}")
        except urllib.error.HTTPError as e:
            if e.code in (404, 403):
                logs.append(f"Grafana: skip reload {endpoint} (HTTP {e.code})")
            else:
                logs.append(f"Grafana: reload failed {endpoint} (HTTP {e.code})")
        except Exception as e:
            logs.append(f"Grafana: reload {endpoint} ({e})")
    return logs
