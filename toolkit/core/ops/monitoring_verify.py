"""Deep verification for the observability stack (beyond container health)."""

from __future__ import annotations

import base64
import json
from typing import TYPE_CHECKING

import httpx

from toolkit.core.ops.hook_verify import VerifyCheck
from toolkit.core.secrets.bootstrap_passwords import resolve_bootstrap_password

if TYPE_CHECKING:
    from pathlib import Path

    from toolkit.core.config.config import Config


def verify_monitoring_stack(
    cfg: Config,
    secrets: dict[str, str],
    root: Path,
) -> list[VerifyCheck]:
    """Prometheus targets, Grafana datasource health, Loki labels, Komodo health."""
    from toolkit.core.ansible.ansible_ssh import docker_exec_curl
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import service_address

    checks: list[VerifyCheck] = []
    grafana_pass = resolve_bootstrap_password(secrets, "GRAFANA_ADMIN_PASSWORD") or "admin"
    auth_hdr = {
        "Authorization": f"Basic {base64.b64encode(f'admin:{grafana_pass}'.encode()).decode()}",
    }
    metrics_service = load_service_catalog().require_provider("metrics").name
    prometheus_address = service_address(cfg, metrics_service) if cfg.is_multi_node else "localhost"
    grafana_address = service_address(cfg, "grafana") if cfg.is_multi_node else "localhost"
    loki_address = service_address(cfg, "loki") if cfg.is_multi_node else "localhost"
    komodo_address = service_address(cfg, "komodo-core") if cfg.is_multi_node else "localhost"

    from toolkit.services.sdk.monitoring import (
        loki_internal_url,
        prometheus_internal_url,
    )

    rc, body = docker_exec_curl(
        cfg,
        prometheus_address,
        metrics_service,
        f"{prometheus_internal_url()}/api/v1/targets",
        root=root,
    )
    if rc == 0 and body:
        try:
            active = json.loads(body).get("data", {}).get("activeTargets", [])
            down = [t for t in active if t.get("health") != "up"]
            # An empty target set is not a healthy Prometheus installation: a
            # misloaded/empty scrape configuration would otherwise report
            # ``0/0 targets up`` as a false green.
            targets_ok = bool(active) and not down
            checks.append(
                VerifyCheck(
                    metrics_service,
                    "targets",
                    targets_ok,
                    f"{len(active) - len(down)}/{len(active)} targets up" + (f" ({len(down)} down)" if down else ""),
                )
            )
        except json.JSONDecodeError:
            checks.append(VerifyCheck(metrics_service, "targets", False, "invalid targets JSON"))
    else:
        checks.append(VerifyCheck(metrics_service, "targets", False, "API unreachable"))

    for uid, label in (("prometheus", "datasource_health_prometheus"), ("loki", "datasource_health_loki")):
        rc, body = docker_exec_curl(
            cfg,
            grafana_address,
            "grafana",
            f"http://localhost:3000/api/datasources/uid/{uid}/health",
            root=root,
            headers=auth_hdr,
        )
        ok = False
        detail = body[:80] if body else "unreachable"
        if rc == 0 and body:
            try:
                ok = json.loads(body).get("status") == "OK"
                detail = "healthy" if ok else body[:80]
            except json.JSONDecodeError:
                detail = "invalid JSON"
        checks.append(VerifyCheck("grafana", label, ok, detail))

    rc, body = docker_exec_curl(
        cfg,
        loki_address,
        "loki",
        f"{loki_internal_url()}/loki/api/v1/labels",
        root=root,
    )
    if rc == 0 and body:
        try:
            labels = json.loads(body).get("data", [])
            checks.append(VerifyCheck("loki", "labels", bool(labels), f"{len(labels)} label(s)"))
        except json.JSONDecodeError:
            checks.append(VerifyCheck("loki", "labels", False, "invalid response"))
    else:
        checks.append(VerifyCheck("loki", "labels", False, "API unreachable"))

    rc, _body = docker_exec_curl(cfg, komodo_address, "komodo-core", "http://localhost:9120/", root=root)
    checks.append(VerifyCheck("komodo", "health", rc == 0, "ok" if rc == 0 else f"HTTP {rc}"))

    checks.extend(verify_grafana_alerting(cfg, root, grafana_address=grafana_address, auth_hdr=auth_hdr))
    checks.append(_verify_prometheus_reload(cfg, metrics_service, prometheus_address, root))

    return checks


def _verify_prometheus_reload(
    cfg: Config,
    metrics_service: str,
    prometheus_address: str,
    root: Path,
) -> VerifyCheck:
    """Prometheus config reload endpoint accepts POST (lifecycle enabled)."""
    from toolkit.services.sdk import docker_exec_on_vm as docker_exec
    from toolkit.services.sdk.monitoring import prometheus_reload_url

    post_cmd = [
        "/bin/busybox",
        "wget",
        "-q",
        "-O",
        "/dev/null",
        "--post-data",
        "",
        prometheus_reload_url(internal=True),
    ]
    if cfg.is_multi_node:
        rc, out = docker_exec(cfg, metrics_service, post_cmd, prometheus_address, root, timeout=20)
        ok = rc == 0
        return VerifyCheck(metrics_service, "reload", ok, "reload ok" if ok else (out or "reload failed")[:80])
    try:
        resp = httpx.post(prometheus_reload_url(internal=True), timeout=10)
        ok = resp.status_code in (200, 202, 204)
        return VerifyCheck(metrics_service, "reload", ok, f"HTTP {resp.status_code}")
    except httpx.HTTPError as exc:
        return VerifyCheck(metrics_service, "reload", False, str(exc)[:80])


def verify_grafana_alerting(
    cfg: Config,
    root: Path,
    *,
    grafana_address: str,
    auth_hdr: dict[str, str],
) -> list[VerifyCheck]:
    """Verify provisioned Grafana contact points and alert rule groups (G70)."""
    from toolkit.core.ansible.ansible_ssh import docker_exec_curl

    checks: list[VerifyCheck] = []
    grafana_base = "http://localhost:3000"

    cp_body = ""
    for path in (
        "/api/v1/provisioning/alerting/contact-points",
        "/api/v1/provisioning/contact-points",
    ):
        rc, body = docker_exec_curl(
            cfg,
            grafana_address,
            "grafana",
            f"{grafana_base}{path}",
            root=root,
            headers=auth_hdr,
        )
        if rc == 0 and body:
            cp_body = body
            break

    has_ntfy = False
    if cp_body:
        try:
            contact_points = json.loads(cp_body)
            if isinstance(contact_points, list):
                has_ntfy = any(isinstance(cp, dict) and cp.get("uid") == "homelab-ntfy" for cp in contact_points)
            detail = "homelab-ntfy receiver loaded" if has_ntfy else "homelab-ntfy receiver missing"
        except json.JSONDecodeError:
            detail = "invalid contact-points JSON"
    else:
        detail = "contact-points API unreachable"
    checks.append(VerifyCheck("grafana", "alerting_contact_point", has_ntfy, detail))

    rules_body = ""
    for path in (
        "/api/v1/provisioning/alerting/alert-rules",
        "/api/v1/provisioning/alert-rules",
    ):
        rc, body = docker_exec_curl(
            cfg,
            grafana_address,
            "grafana",
            f"{grafana_base}{path}",
            root=root,
            headers=auth_hdr,
        )
        if rc == 0 and body:
            rules_body = body
            break

    has_core_group = False
    if rules_body:
        try:
            rules = json.loads(rules_body)
            if isinstance(rules, list):
                core_count = sum(1 for r in rules if isinstance(r, dict) and r.get("ruleGroup") == "homelab-core")
                has_core_group = core_count >= 1
                detail = f"homelab-core: {core_count} rule(s)" if has_core_group else "homelab-core group missing"
            else:
                detail = "unexpected alert-rules shape"
        except json.JSONDecodeError:
            detail = "invalid alert-rules JSON"
    else:
        detail = "alert-rules API unreachable"
    checks.append(VerifyCheck("grafana", "alerting_rules", has_core_group, detail))

    # G70: delivery test — POST to ntfy grafana-alerts topic via infra container
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

    pub_cmd = (
        "docker exec ntfy wget -qO- --post-data='hook verify probe' "
        "--header='Title: homelab-verify-grafana' --header='Priority: min' "
        "http://127.0.0.1/grafana-alerts"
    )
    from toolkit.core.manifest.placement import service_address

    ntfy_address = service_address(cfg, "ntfy") if cfg.is_multi_node else "localhost"
    rc, _out, _ = ssh_run_on_vm(cfg, ntfy_address, pub_cmd, root=root, timeout=20)
    sent = rc == 0
    checks.append(
        VerifyCheck(
            "grafana",
            "alerting_ntfy_delivery",
            sent,
            "ntfy grafana-alerts publish ok" if sent else "ntfy publish failed",
        )
    )

    return checks
