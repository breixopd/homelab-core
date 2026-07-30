"""caddy service plugin.

Owns verify() for route parity, forward-auth wiring, and a live HTTPS probe
on top of the base ServicePlugin defaults read from service.yaml.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin
from toolkit.services.caddy.artifacts import format_generated_caddyfile, validate_generated_caddyfile

# Pinned Cloudflare edge ranges from the authoritative list:
# https://www.cloudflare.com/ips/ . They are deliberately rendered as a
# static allow-list so arbitrary X-Forwarded-For values are never trusted.
CLOUDFLARE_PROXY_CIDRS = (
    "173.245.48.0/20",
    "103.21.244.0/22",
    "103.22.200.0/22",
    "103.31.4.0/22",
    "141.101.64.0/18",
    "108.162.192.0/18",
    "190.93.240.0/20",
    "188.114.96.0/20",
    "197.234.240.0/22",
    "198.41.128.0/17",
    "162.158.0.0/15",
    "104.16.0.0/13",
    "104.24.0.0/14",
    "172.64.0.0/13",
    "131.0.72.0/22",
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
)

if TYPE_CHECKING:
    from toolkit.core.config.config import Config
    from toolkit.core.generate.artifacts import ArtifactGenerationContext
    from toolkit.services import RuntimeLifecycleContext
    from toolkit.services.sdk import VerifyCheck


class CaddyPlugin(ServicePlugin):
    service = "caddy"
    category = "management"

    def generate_artifacts(self, context: ArtifactGenerationContext) -> None:
        from toolkit.core.manifest.catalog import load_service_catalog
        from toolkit.core.manifest.placement import manifest_node
        from toolkit.core.manifest.routes import compile_routes
        from toolkit.services.caddy.routes import compile_caddy_routes
        from toolkit.services.sdk import caddy_cross_vm_upstream

        catalog = load_service_catalog()
        authelia = catalog.require("authelia")
        authelia_route = next(route for route in authelia.routes if route.upstream)
        if authelia_route.published_port is None:
            raise ValueError("Authelia must publish its forward-auth endpoint")
        crowdsec = catalog.require("crowdsec")
        crowdsec_listener = next(listener for listener in crowdsec.network_listeners if listener.id == "host-agent-api")
        cloudflare_token = context.secrets.get("CLOUDFLARE_API_TOKEN", "")
        context.render_template(
            "generated/Caddyfile",
            "Caddyfile.j2",
            {
                "email": context.config.email,
                "domain": context.config.domain,
                "acme_ca": "",
                "dns_challenge": bool(context.config.dns.provider == "cloudflare" and cloudflare_token),
                "routes": compile_caddy_routes(context.config, compile_routes(context.config, catalog)),
                "authelia_url": "http://"
                + caddy_cross_vm_upstream(
                    context.config,
                    manifest_node(context.config, authelia),
                    str(authelia_route.published_port),
                    published_port=authelia_route.published_port,
                ),
                "crowdsec_url": "http://"
                + caddy_cross_vm_upstream(
                    context.config,
                    manifest_node(context.config, crowdsec),
                    str(crowdsec_listener.port),
                    published_port=crowdsec_listener.port,
                ),
                "crowdsec_enabled": bool(
                    context.config.category_enabled("security") and context.secrets.get("CROWDSEC_CADDY_BOUNCER_KEY")
                ),
                "cloudflare_proxy_enabled": bool(context.config.dns.proxy_enabled),
                "cloudflare_proxy_cidrs": CLOUDFLARE_PROXY_CIDRS,
            },
        )
        format_generated_caddyfile(context.root / "generated", repo_root=context.root)
        validate_generated_caddyfile(context.root / "generated", repo_root=context.root)

    def after_runtime_start(self, context: RuntimeLifecycleContext, services: tuple[str, ...]) -> None:
        reload_proc = context.compose(
            "exec",
            "-T",
            "caddy",
            "caddy",
            "reload",
            "--config",
            "/etc/caddy/Caddyfile",
            "--adapter",
            "caddyfile",
        )
        if reload_proc.returncode != 0:
            context.warn("Caddy configuration reload failed")
            context.record_failure()
        if not context.wait_until_healthy("authelia", ("authelia",)):
            context.warn("Authelia is not healthy; attempting service recovery")
            context.run_recovery("heal_authelia", "toolkit.services.authelia.bootstrap")
            if not context.wait_until_healthy("authelia-retry", ("authelia",)):
                context.record_failure()
        if context.wait_until_healthy("caddy-admin", ("caddy",)):
            return
        proc = context.compose(
            "exec",
            "-T",
            "caddy",
            "sh",
            "-c",
            "wget -qO- http://127.0.0.1:2019/config/ >/dev/null 2>&1",
        )
        if proc.returncode == 0:
            context.warn("Caddy admin API is up but health is pending while certificates converge")
        else:
            context.warn("Caddy admin API is unreachable; the authenticated edge is degraded")
            context.record_failure()

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        import re
        import shlex

        from toolkit.core.manifest.routes import compile_routes
        from toolkit.services.sdk import (
            VerifyCheck,
            container_exists_on_vm,
            docker_curl,
            docker_exec_on_vm,
            parse_curl_headers,
            ssh_on_vm,
        )

        def _skip(note: str) -> list[VerifyCheck]:
            return [
                VerifyCheck("caddy", "route_parity", True, note),
                VerifyCheck("caddy", "forward_auth", True, note),
                VerifyCheck("caddy", "live_probe", True, note),
            ]

        def _read_caddy_config() -> str:
            caddyfile = root / "generated" / "Caddyfile"
            if cfg.is_multi_node:
                rc_cfg, out_cfg, _ = ssh_on_vm(
                    cfg,
                    vm_ip,
                    f"cat -- {shlex.quote(caddyfile.as_posix())}",
                    root=root,
                    timeout=20,
                )
                if rc_cfg == 0 and (out_cfg or "").strip():
                    return out_cfg
            elif caddyfile.is_file():
                return caddyfile.read_text()
            rc, out = docker_exec_on_vm(cfg, "caddy", ["cat", "/etc/caddy/Caddyfile"], vm_ip, root)
            return out if rc == 0 else ""

        if cfg.domain == "localhost":
            return _skip("skipped (localhost)")

        if not container_exists_on_vm(cfg, vm_ip, "caddy", root):
            return [VerifyCheck("caddy", "container", False, "Caddy ingress container missing")]

        config_text = _read_caddy_config()
        if not config_text.strip():
            rc, config_text = docker_curl(cfg, vm_ip, "caddy", "http://localhost:2019/config/", root=root)
        config_text = config_text or ""
        if not config_text.strip():
            return [
                VerifyCheck("caddy", "route_parity", False, "could not read Caddy config"),
                VerifyCheck("caddy", "forward_auth", False, "could not read Caddy config"),
                VerifyCheck("caddy", "live_probe", False, "could not read Caddy config"),
            ]

        routes = compile_routes(cfg)
        expected_hosts = sorted({route.host for route in routes})
        auth_hosts = sorted(
            {route.host for route in routes if route.match is None and route.auth.mode in {"forward_auth", "split"}}
        )
        missing_routes = [host for host in expected_hosts if host not in config_text]
        route_ok = not missing_routes and bool(expected_hosts)
        route_detail = f"{len(expected_hosts) - len(missing_routes)}/{len(expected_hosts)} routes in config"
        if missing_routes:
            route_detail += f" (missing: {', '.join(missing_routes[:3])}{'…' if len(missing_routes) > 3 else ''})"

        def _site_block(host: str) -> str:
            match = re.search(rf"(?m)^\s*{re.escape(host)}\s*\{{", config_text)
            if match is None:
                return ""
            depth = 0
            for index in range(match.start(), len(config_text)):
                if config_text[index] == "{":
                    depth += 1
                elif config_text[index] == "}":
                    depth -= 1
                    if depth == 0:
                        return config_text[match.start() : index + 1]
            return config_text[match.start() :]

        missing_auth: list[str] = []
        for host in auth_hosts:
            block = _site_block(host)
            if block and "forward_auth" not in block and "import authelia" not in block.lower():
                missing_auth.append(host)
        auth_ok = not missing_auth
        auth_detail = f"{len(auth_hosts) - len(missing_auth)}/{len(auth_hosts)} protected routes wired"
        if missing_auth:
            sample = ", ".join(missing_auth[:3])
            suffix = "…" if len(missing_auth) > 3 else ""
            auth_detail += f" (missing forward_auth: {sample}{suffix})"
        if not auth_hosts:
            auth_detail = "no forward-auth routes expected"

        security_enabled = cfg.category_enabled("security")
        bouncer_ok = True
        bouncer_detail = "skipped (CrowdSec disabled)"
        access_log_ok = True
        access_log_detail = "skipped (CrowdSec disabled)"
        lapi_auth_ok = True
        lapi_auth_detail = "skipped (CrowdSec disabled)"
        if security_enabled:
            from toolkit.core.manifest.catalog import load_service_catalog
            from toolkit.core.manifest.placement import manifest_node

            crowdsec_manifest = load_service_catalog().require("crowdsec")
            crowdsec_url = f"http://{cfg.node_ip(manifest_node(cfg, crowdsec_manifest))}:8080"
            bouncer_ok = (
                "crowdsec {" in config_text
                and crowdsec_url in config_text
                and "enable_hard_fails" in config_text
                and "\n\t\tcrowdsec\n" in config_text
            )
            access_log_ok = "output file /var/log/caddy/access.log" in config_text
            bouncer_detail = (
                "CrowdSec global app and hard-fail mode configured"
                if bouncer_ok
                else "CrowdSec bouncer app is missing or not fail-closed"
            )
            access_log_detail = (
                "structured public-route access log configured"
                if access_log_ok
                else "public-route access log is missing"
            )
            auth_rc, auth_out = docker_exec_on_vm(
                cfg,
                "caddy",
                ["caddy", "crowdsec", "health"],
                vm_ip,
                root,
                timeout=15,
            )
            lapi_auth_ok = auth_rc == 0
            lapi_auth_detail = (
                "Caddy authenticated to CrowdSec LAPI"
                if lapi_auth_ok
                else f"Caddy LAPI authentication failed: {(auth_out or 'health command failed')[:120]}"
            )

        probe_host = None
        if cfg.category_enabled("management"):
            probe_host = f"grafana.{cfg.domain}"
        elif expected_hosts:
            probe_host = expected_hosts[0]

        probe_ok = False
        probe_detail = "no probe host"
        if probe_host:
            if cfg.is_multi_node:
                shell = (
                    f"curl -sk --max-time 12 --resolve {probe_host}:443:127.0.0.1 "
                    f"-H 'X-Forwarded-Proto: https' -o /dev/null -w '%{{http_code}}' "
                    f"https://{probe_host}/"
                )
                rc_probe, out_probe, _ = ssh_on_vm(cfg, vm_ip, shell, root=root, timeout=20)
                code = (out_probe or "").strip()[-3:] if (out_probe or "").strip().isdigit() else ""
                if not code and out_probe:
                    code = (out_probe or "").strip()
                try:
                    status = int(code) if code.isdigit() else None
                except ValueError:
                    status = None
                if status is None and out_probe:
                    status, _ = parse_curl_headers(out_probe)
                probe_ok = status is not None and status < 500
                probe_detail = f"HTTP {status}" if status is not None else (out_probe or "curl failed")[:80]
            else:
                try:
                    proc = subprocess.run(
                        [
                            "curl",
                            "-sk",
                            "--max-time",
                            "12",
                            "--resolve",
                            f"{probe_host}:443:127.0.0.1",
                            "-H",
                            "X-Forwarded-Proto: https",
                            "-o",
                            "/dev/null",
                            "-w",
                            "%{http_code}",
                            f"https://{probe_host}/",
                        ],
                        capture_output=True,
                        text=True,
                        timeout=15,
                        check=False,
                    )
                    code = (proc.stdout or proc.stderr or "").strip()
                    status = int(code) if code.isdigit() else None
                    probe_ok = status is not None and status < 500
                    probe_detail = f"HTTP {status}" if status is not None else (code or "curl failed")[:80]
                except (OSError, subprocess.TimeoutExpired) as exc:
                    probe_detail = str(exc)[:80]

        probe_label = f"{probe_host}: {probe_detail}" if probe_host else probe_detail
        checks = [
            VerifyCheck("caddy", "route_parity", route_ok, route_detail),
            VerifyCheck("caddy", "forward_auth", auth_ok, auth_detail),
            VerifyCheck("caddy", "live_probe", probe_ok, probe_label),
        ]
        if security_enabled:
            checks.extend(
                [
                    VerifyCheck("caddy", "crowdsec_bouncer", bouncer_ok, bouncer_detail),
                    VerifyCheck("caddy", "access_log", access_log_ok, access_log_detail),
                    VerifyCheck("caddy", "crowdsec_lapi_auth", lapi_auth_ok, lapi_auth_detail),
                ]
            )
        return checks
