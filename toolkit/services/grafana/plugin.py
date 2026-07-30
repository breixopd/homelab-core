"""Grafana service lifecycle and generated alert provisioning."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import OIDCClient, ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.generate.artifacts import ArtifactGenerationContext
    from toolkit.services.sdk import VerifyCheck


class GrafanaPlugin(ServicePlugin):
    """Self-contained Grafana service definition.

    ``service``, ``category``, ``placement``, and ``icon`` are populated by the
    discovery loader from ``service.yaml`` — no need to set them as class
    attributes here (the base class provides sane defaults).
    """

    service = "grafana"
    category = "management"

    def generate_artifacts(self, context: ArtifactGenerationContext) -> None:
        """Render only alert receivers backed by enabled service integrations."""
        import yaml
        from toolkit.core.manifest.catalog import load_service_catalog
        from toolkit.core.manifest.routes import service_is_enabled

        source = Path(__file__).with_name("templates") / "contact-points.yaml"
        document = yaml.safe_load(source.read_text(encoding="utf-8"))
        receivers = document["contactPoints"][0]["receivers"]
        catalog = load_service_catalog()
        enabled_receivers = {"homelab-autoheal"}
        if service_is_enabled(context.config, catalog.require("mailserver"), catalog):
            enabled_receivers.add("homelab-email")
        if service_is_enabled(context.config, catalog.require("ntfy"), catalog):
            enabled_receivers.add("homelab-ntfy")
        document["contactPoints"][0]["receivers"] = [
            receiver for receiver in receivers if receiver.get("uid") in enabled_receivers
        ]
        context.write_text(
            "generated/grafana/contact-points.yaml",
            yaml.safe_dump(document, sort_keys=False),
        )
        for name in ("policies.yaml", "rules.yaml"):
            context.write_text(
                f"generated/grafana/{name}",
                source.with_name(name).read_text(encoding="utf-8"),
            )

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Reload provisioned dashboards and validate configured datasources."""
        import urllib.error

        import httpx
        from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT
        from toolkit.core.ops.automation import resolve_docker_service_url
        from toolkit.services.grafana.bootstrap import (
            reload_dashboard_provisioning,
            verify_grafana_datasources,
            verify_grafana_provisioning,
        )

        install_root = root or Path(DEFAULT_HOMELAB_ROOT)
        base_url = resolve_docker_service_url("grafana", 3000)
        logs: list[str] = []
        network_errors = (OSError, urllib.error.URLError, httpx.HTTPError)
        try:
            logs.extend(reload_dashboard_provisioning(secrets, base_url=base_url))
            logs.extend(verify_grafana_provisioning(secrets, base_url=base_url, root=install_root))
        except network_errors as exc:
            logs.append(f"WARNING: Grafana provisioning reload not ready ({exc})")
        try:
            logs.extend(verify_grafana_datasources(secrets, base_url=base_url))
        except RuntimeError:
            raise
        except network_errors as exc:
            logs.append(f"WARNING: Grafana datasources not reachable yet ({exc})")
        return logs

    # ── OIDC ──────────────────────────────────────────────────────────────────
    @property
    def oidc_client(self) -> OIDCClient:
        """Read the OIDC client contract from the service manifest."""
        oidc = (self._yaml_data or {}).get("oidc", {})
        return OIDCClient(
            client_id=oidc.get("client_id", "grafana"),
            secret_env_var=oidc.get("secret_env_var", "GRAFANA_OIDC_SECRET"),
            native=oidc.get("native", False),
        )

    # ── verify ─────────────────────────────────────────────────────────────────
    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Datasources reachable + OIDC auth/token URLs match Authelia."""
        import json

        from toolkit.core.secrets.bootstrap_passwords import resolve_bootstrap_password
        from toolkit.services.sdk import (
            VerifyCheck,
            authelia_public_url,
            basic_auth_header,
            container_exists_on_vm,
            docker_curl,
            docker_exec_on_vm,
        )

        checks: list[VerifyCheck] = []
        if cfg.domain == "localhost":
            return [VerifyCheck("grafana", "health", True, "skipped (localhost)")]
        if not container_exists_on_vm(cfg, vm_ip, "grafana", root):
            return [VerifyCheck("grafana", "health", False, "container missing")]

        grafana_pass = resolve_bootstrap_password(secrets, "GRAFANA_ADMIN_PASSWORD") or "admin"
        expected_auth_url = f"{authelia_public_url(cfg)}/api/oidc/authorization"
        expected_token_url = "http://authelia:9091/api/oidc/token"
        auth = {"Authorization": basic_auth_header("admin", grafana_pass)}
        grafana_base = "http://localhost:3000"

        rc, body = docker_curl(cfg, vm_ip, "grafana", f"{grafana_base}/api/health", root=root, headers=auth)
        health_ok = False
        health_detail = "API unreachable"
        if rc == 0 and body:
            try:
                data = json.loads(body)
                health_ok = data.get("database") == "ok"
                health_detail = f"database={data.get('database', '?')}"
            except json.JSONDecodeError:
                health_detail = "invalid health JSON"
        checks.append(VerifyCheck("grafana", "health", health_ok, health_detail))

        # ── datasource check (multi-VM: curl inside container; single-host: httpx) ─
        ds_list: list[dict] = []
        if cfg.is_multi_node:
            rc, body = docker_curl(cfg, vm_ip, "grafana", f"{grafana_base}/api/datasources", root=root, headers=auth)
            if rc == 0 and body:
                try:
                    ds_list = json.loads(body)
                except json.JSONDecodeError:
                    checks.append(VerifyCheck("grafana", "datasources", False, "invalid datasources JSON"))
                    ds_list = []
            else:
                checks.append(VerifyCheck("grafana", "datasources", False, "API unreachable or auth failed"))
        else:
            import httpx

            try:
                resp = httpx.get(f"{grafana_base}/api/datasources", headers=auth, timeout=10, follow_redirects=True)
            except httpx.HTTPError:
                resp = None
            if resp and resp.status_code == 200:
                ds_list = resp.json()
            else:
                checks.append(VerifyCheck("grafana", "datasources", False, "API unreachable or auth failed"))

        if ds_list:
            ds_names = [d.get("name", "") for d in ds_list]
            has_prom = any("prometheus" in n.lower() for n in ds_names)
            has_loki = any("loki" in n.lower() for n in ds_names)
            prom_detail = "Prometheus datasource" if has_prom else "missing"
            checks.append(VerifyCheck("grafana", "datasource_prometheus", has_prom, prom_detail))
            checks.append(
                VerifyCheck("grafana", "datasource_loki", has_loki, "Loki datasource" if has_loki else "missing")
            )
            for want in ("prometheus", "loki"):
                match = next((d for d in ds_list if want in (d.get("name") or "").lower()), None)
                if not match:
                    continue
                uid = match.get("uid") or want
                if cfg.is_multi_node:
                    rc, body = docker_curl(
                        cfg,
                        vm_ip,
                        "grafana",
                        f"{grafana_base}/api/datasources/uid/{uid}/health",
                        root=root,
                        headers=auth,
                    )
                else:
                    import httpx

                    try:
                        resp = httpx.get(
                            f"{grafana_base}/api/datasources/uid/{uid}/health",
                            headers=auth,
                            timeout=10,
                        )
                        rc, body = (0, resp.text) if resp.status_code == 200 else (1, "")
                    except httpx.HTTPError:
                        rc, body = 1, ""
                probe_ok = False
                probe_detail = body[:80] if body else "unreachable"
                if rc == 0 and body:
                    try:
                        probe_ok = json.loads(body).get("status") == "OK"
                        probe_detail = "healthy" if probe_ok else body[:80]
                    except json.JSONDecodeError:
                        probe_detail = "invalid JSON"
                checks.append(VerifyCheck("grafana", f"datasource_health_{want}", probe_ok, probe_detail))

        rc, body = docker_curl(
            cfg, vm_ip, "grafana", f"{grafana_base}/api/search?type=dash-db", root=root, headers=auth
        )
        dash_ok = False
        dash_detail = "dashboard search unreachable"
        if rc == 0 and body:
            try:
                dashboards = json.loads(body)
                count = len(dashboards) if isinstance(dashboards, list) else 0
                dash_ok = count > 0
                dash_detail = f"{count} provisioned dashboard(s)"
            except json.JSONDecodeError:
                dash_detail = "invalid dashboard JSON"
        checks.append(VerifyCheck("grafana", "dashboards", dash_ok, dash_detail))

        # ── OIDC env check (GF_AUTH_GENERIC_OAUTH_*_URL) ──────────────────────────
        rc, out = docker_exec_on_vm(cfg, "grafana", ["env"], vm_ip, root)
        if rc != 0:
            checks.append(VerifyCheck("grafana", "oidc_issuer", False, "could not read env (container not ready)"))
            return checks
        saw_auth = saw_token = False
        for line in out.splitlines():
            if line.startswith("GF_AUTH_GENERIC_OAUTH_AUTH_URL="):
                saw_auth = True
                url = line.split("=", 1)[1].strip()
                ok = url == expected_auth_url
                auth_detail = url if ok else f"WARNING: {url} (expected {expected_auth_url})"
                checks.append(VerifyCheck("grafana", "oidc_issuer", ok, auth_detail))
            if line.startswith("GF_AUTH_GENERIC_OAUTH_TOKEN_URL="):
                saw_token = True
                url = line.split("=", 1)[1].strip()
                ok = url == expected_token_url
                token_detail = url if ok else f"WARNING: {url} (expected {expected_token_url})"
                checks.append(VerifyCheck("grafana", "oidc_token_url", ok, token_detail))
        if not saw_auth:
            checks.append(VerifyCheck("grafana", "oidc_issuer", False, "GF_AUTH_GENERIC_OAUTH_AUTH_URL not set"))
        if not saw_token:
            checks.append(VerifyCheck("grafana", "oidc_token_url", False, "GF_AUTH_GENERIC_OAUTH_TOKEN_URL not set"))
        return checks
