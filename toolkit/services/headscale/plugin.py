"""headscale service plugin.

Owns its verify() (OIDC issuer/scope/PKCE/discovery/cli-fallback + mesh
nodes/subnet-router/ACL) and post_start() (mesh OIDC provider + subnet
router) on top of the base ServicePlugin defaults read from service.yaml.

``check_nodes`` and ``check_subnet_router`` are also exposed as module-level
functions for callers that need a single mesh status snapshot without running
the full plugin dispatch (e.g. ``toolkit.core.registry.mesh_status``).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

import yaml
from toolkit.services import FleetOnboardingContribution, ServicePlugin

if TYPE_CHECKING:
    from toolkit.core.config.config import Config, ExternalHost
    from toolkit.core.generate.artifacts import ArtifactGenerationContext
    from toolkit.services import FleetOnboardingContext
    from toolkit.services.sdk import VerifyCheck


class HeadscalePlugin(ServicePlugin):
    service = "headscale"
    category = "security"

    def generate_artifacts(self, context: ArtifactGenerationContext) -> None:
        from toolkit.core.manifest.catalog import load_service_catalog
        from toolkit.core.manifest.placement import service_address
        from toolkit.core.registry.mesh import headscale_acl_tags, headscale_tag_owner, mesh_lan_cidr, mesh_router_tag
        from toolkit.services.sdk import authelia_oidc_issuer

        oidc = load_service_catalog().require(self.service).oidc
        if oidc is None:
            raise RuntimeError("headscale manifest must declare OIDC configuration")
        context.render_template(
            "generated/headscale/config.yaml",
            "headscale.yml.j2",
            {
                "domain": context.config.domain,
                "oidc_issuer": authelia_oidc_issuer(context.config),
                "oidc_client_id": oidc.client_id,
                "oidc_client_secret": context.secrets.get(oidc.secret_env_var, ""),
                "infra_dns_ip": service_address(context.config, "adguard"),
                "mesh_ipv4_cidr": context.config.network.mesh_ipv4_cidr,
                "mesh_ipv6_cidr": context.config.network.mesh_ipv6_cidr,
            },
        )
        context.render_template(
            "generated/headscale/acl.hujson",
            "headscale_acl.hujson.j2",
            {
                "fleet_tags": headscale_acl_tags(context.config),
                "tag_owner": headscale_tag_owner(context.config),
                "mesh_router_tag": mesh_router_tag(context.config),
                "mesh_lan_cidr": mesh_lan_cidr(context.config),
            },
        )

    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        from toolkit.core.ops.automation import resolve_docker_service_url
        from toolkit.services.headscale.bootstrap import (
            bootstrap_headscale_preauth,
            ensure_headscale_oidc_provider,
        )
        from toolkit.services.headscale.mesh import bootstrap_infra_subnet_router
        from toolkit.services.sdk import wait_for_http

        install_root = root or Path.cwd()
        health_url = f"{resolve_docker_service_url('headscale', 8080)}/health"
        logs = [
            "Headscale: API reachable"
            if wait_for_http(health_url, timeout=60, interval=5)
            else "WARNING: Headscale: not ready yet"
        ]
        logs.extend(ensure_headscale_oidc_provider(cfg, install_root))
        preauth_logs = bootstrap_headscale_preauth(tags=list(cfg.fleet.headscale_tags or ["tag:fleet-external"]))
        logs.extend(preauth_logs)
        if any("preauth key create failed" in line for line in preauth_logs):
            raise RuntimeError("Headscale preauth key creation failed")
        if cfg.is_multi_node and cfg.fleet.mesh_subnet_router:
            logs.extend(bootstrap_infra_subnet_router(cfg, install_root))
        return logs

    def controller_access_checks(self, cfg: Config, root: Path) -> list[VerifyCheck]:
        from toolkit.services.headscale.mesh import controller_mesh_access_checks

        return controller_mesh_access_checks(cfg, root)

    def prepare_fleet_onboarding(
        self,
        cfg: Config,
        host: ExternalHost,
        root: Path,
    ) -> FleetOnboardingContribution:
        from toolkit.core.manifest.routes import compile_routes
        from toolkit.services.headscale.bootstrap import headscale_preauth_key

        route = next(route for route in compile_routes(cfg) if route.service == self.service and route.match is None)
        tags = tuple(host.headscale_tags or cfg.fleet.headscale_tags or ())
        key = headscale_preauth_key(tags=list(tags) or None)
        logs: list[str] = []
        if not key:
            logs.append("Headscale: no reusable preauth key available - mesh join will be skipped")
        elif tags:
            logs.append(f"Headscale: preauth key prepared with tags {', '.join(tags)}")
        variables: dict[str, object] = {"headscale_url": f"https://{route.host}"}
        if key:
            variables["headscale_auth_key"] = key
        if tags:
            variables["headscale_tags"] = list(tags)
        return FleetOnboardingContribution(variables=variables, logs=tuple(logs))

    def after_fleet_onboarding(self, context: FleetOnboardingContext) -> None:
        from toolkit.services.headscale.mesh import fleet_node_online

        active = fleet_node_online(context.config, context.root, context.host.name)
        has_auth_key = bool(context.variables.get("headscale_auth_key"))
        if active is not True and has_auth_key:
            context.log("Headscale: fleet node is not on the mesh yet - retrying the VPN integration")
            if not context.retry_integrations(("vpn-client",)):
                context.log("Headscale: VPN integration retry failed")
            active = fleet_node_online(context.config, context.root, context.host.name)
        if active is True:
            context.log(f"Headscale: {context.host.name} online - internal services reachable via mesh routes")
        elif active is False:
            context.log(f"Headscale: {context.host.name} registered but offline - check tailscale on host")
        else:
            context.log("Headscale: could not confirm mesh status - run homelab-toolkit fleet status")

    def reconcile_host_integration(
        self,
        integration: str,
        cfg: Config,
        host: ExternalHost,
        root: Path,
        *,
        selected: bool,
    ) -> list[str]:
        if integration != "vpn-client":
            raise ValueError(f"unsupported Headscale host integration: {integration}")
        if selected:
            return []
        return self.cleanup_host_integration(integration, cfg, host, root)

    def cleanup_host_integration(
        self,
        integration: str,
        cfg: Config,
        host: ExternalHost,
        root: Path,
    ) -> list[str]:
        if integration != "vpn-client":
            raise ValueError(f"unsupported Headscale host integration: {integration}")
        from toolkit.core.manifest.placement import service_address
        from toolkit.services.sdk import docker_exec_on_vm

        vm_ip = service_address(cfg, self.service)
        rc, output = docker_exec_on_vm(
            cfg,
            "headscale",
            ["headscale", "nodes", "list", "--output", "json"],
            vm_ip,
            root,
            timeout=30,
        )
        if rc != 0:
            raise RuntimeError(f"could not list Headscale nodes: {(output or 'command failed')[:120]}")
        try:
            payload = json.loads(output or "[]")
        except json.JSONDecodeError as exc:
            raise RuntimeError("Headscale returned invalid node inventory JSON") from exc
        if not isinstance(payload, list):
            raise RuntimeError("Headscale returned an invalid node inventory")

        identifiers = sorted(
            {
                int(node["id"])
                for node in payload
                if isinstance(node, dict)
                and node.get("id") is not None
                and host.name in {str(node.get("name") or ""), str(node.get("given_name") or "")}
            }
        )
        for identifier in identifiers:
            rc, detail = docker_exec_on_vm(
                cfg,
                "headscale",
                ["headscale", "nodes", "delete", "--identifier", str(identifier), "--force"],
                vm_ip,
                root,
                timeout=30,
            )
            if rc != 0:
                failure = (detail or "command failed")[:120]
                raise RuntimeError(f"could not revoke Headscale node {identifier}: {failure}")
        if not identifiers:
            return [f"Headscale: no registered mesh node remains for {host.name}"]
        return [f"Headscale: revoked {len(identifiers)} mesh node(s) for {host.name}"]

    def host_integration_status(
        self,
        integration: str,
        cfg: Config,
        host: ExternalHost,
        root: Path,
    ) -> tuple[bool | None, str] | None:
        if integration != "vpn-client":
            raise ValueError(f"unsupported Headscale host integration: {integration}")
        from toolkit.services.headscale.mesh import fleet_node_online

        active = fleet_node_online(cfg, root, host.name)
        if active is True:
            return True, "registered and online"
        if active is False:
            return False, "registered but offline"
        return None, "could not query Headscale"

    def status(self, cfg: Config, secrets: dict[str, str], root: Path) -> dict[str, object]:
        from toolkit.core.manifest.placement import service_address

        vm_ip = service_address(cfg, self.service)
        nodes = _headscale_nodes(cfg, vm_ip, root)
        users = _headscale_users(cfg, vm_ip, root)
        return {
            "registered_nodes": len(nodes),
            "online_nodes": sum(1 for node in nodes if _mesh_node_online(node)),
            "users": len(users),
        }

    def resources(
        self,
        cfg: Config,
        secrets: dict[str, str],
        root: Path,
    ) -> dict[str, list[dict[str, object]]]:
        from toolkit.core.manifest.placement import service_address

        nodes = _headscale_nodes(cfg, service_address(cfg, self.service), root)
        return {
            "mesh_nodes": [
                {
                    "name": str(node.get("given_name") or node.get("name") or "Unknown"),
                    "user": _node_user(node),
                    "addresses": _node_addresses(node),
                    "state": "Online" if _mesh_node_online(node) else "Offline",
                    "last_seen": _node_last_seen(node),
                }
                for node in nodes
                if isinstance(node, dict)
            ]
        }

    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Check Headscale OIDC issuer, scopes, and in-cluster token route to Authelia."""
        from toolkit.services.headscale.bootstrap import headscale_oidc_cli_fallback
        from toolkit.services.sdk import (
            VerifyCheck,
            authelia_oidc_issuer,
            docker_exec_on_vm,
            oidc_check_auth_discovery_route,
            ssh_on_vm,
        )

        checks: list[VerifyCheck] = []
        expected = authelia_oidc_issuer(cfg)
        rc, out = docker_exec_on_vm(
            cfg, "headscale", ["/bin/busybox", "cat", "/etc/headscale/config.yaml"], vm_ip, root
        )
        if rc != 0:
            checks.append(VerifyCheck("headscale", "oidc_issuer", False, "could not read config (container not ready)"))
            return checks
        try:
            data = yaml.safe_load(out) or {}
        except yaml.YAMLError:
            checks.append(VerifyCheck("headscale", "oidc_issuer", False, "config parse error"))
            return checks
        oidc_cfg = data.get("oidc", {}) or {}
        issuer = (oidc_cfg.get("issuer") or "").strip()
        match = issuer == expected
        detail = issuer if match else f"WARNING: {issuer} (expected {expected})"
        checks.append(VerifyCheck("headscale", "oidc_issuer", match, detail))

        scopes = oidc_cfg.get("scope") or []
        has_groups = "groups" in scopes
        checks.append(
            VerifyCheck(
                "headscale",
                "oidc_scope_groups",
                has_groups,
                "scope includes groups" if has_groups else f"scope={scopes!r} missing groups",
            )
        )

        pkce = oidc_cfg.get("pkce") or {}
        pkce_ok = bool(pkce.get("enabled")) and (pkce.get("method") or "").upper() == "S256"
        checks.append(
            VerifyCheck("headscale", "oidc_pkce", pkce_ok, "pkce S256 enabled" if pkce_ok else f"pkce={pkce!r}")
        )

        checks.append(oidc_check_auth_discovery_route(cfg, "headscale", root))

        rc_log, log_out, _ = ssh_on_vm(cfg, vm_ip, "docker logs headscale 2>&1 | tail -80", root=root, timeout=20)
        cli_fallback = headscale_oidc_cli_fallback(log_out or "")
        checks.append(
            VerifyCheck(
                "headscale",
                "oidc_provider",
                not cli_fallback,
                "OIDC provider active"
                if not cli_fallback
                else "OIDC unavailable at startup (CLI fallback) — restart headscale after authelia/caddy",
            )
        )

        # ── api_health — DB-backed health endpoint ────────────────────────────
        if cfg.domain == "localhost":
            checks.append(VerifyCheck("headscale", "api_health", True, "skipped (localhost)"))
        else:
            rc_h, health_out = docker_exec_on_vm(
                cfg,
                "headscale",
                ["/bin/busybox", "wget", "-qO-", "http://localhost:8080/health"],
                vm_ip,
                root,
                timeout=12,
            )
            health_ok = False
            health_detail = (health_out or "unreachable")[:120]
            if rc_h == 0 and health_out:
                try:
                    import json

                    payload = json.loads(health_out)
                    health_ok = payload.get("status") == "pass"
                    health_detail = f"status={payload.get('status', '?')}"
                except json.JSONDecodeError:
                    health_ok = "pass" in (health_out or "").lower()
                    health_detail = health_out[:80]
            checks.append(VerifyCheck("headscale", "api_health", health_ok, health_detail))

        # ── users — OIDC-backed user namespace exists ─────────────────────────
        checks.append(check_users(cfg, vm_ip, root))

        # ── mesh checks: nodes, subnet router, ACL ───────────────────────────
        checks.append(check_nodes(cfg, vm_ip, root))
        checks.append(check_subnet_router(cfg, vm_ip, root))
        checks.append(check_acl(cfg, root))
        return checks


# ── Mesh check functions (also callable standalone, e.g. for mesh_status) ────


def _headscale_list(
    cfg: Config,
    infra_ip: str,
    root: Path,
    resource: str,
    *,
    timeout: int,
) -> list[dict[str, object]]:
    from toolkit.services.sdk import docker_exec_on_vm

    rc, out = docker_exec_on_vm(
        cfg,
        "headscale",
        ["headscale", "-o", "json", resource, "list"],
        infra_ip,
        root,
        timeout=timeout,
    )
    if rc != 0 or not (out or "").strip():
        raise RuntimeError(f"Headscale {resource} inventory is unavailable")
    try:
        payload = json.loads(out)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Headscale returned invalid {resource} JSON") from exc
    if payload is None:
        return []
    if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
        raise RuntimeError(f"Headscale returned an invalid {resource} inventory")
    return payload


def _headscale_nodes(cfg: Config, infra_ip: str, root: Path) -> list[dict[str, object]]:
    return _headscale_list(cfg, infra_ip, root, "nodes", timeout=30)


def _headscale_users(cfg: Config, infra_ip: str, root: Path) -> list[dict[str, object]]:
    return _headscale_list(cfg, infra_ip, root, "users", timeout=20)


def _mesh_node_online(node: dict[str, object]) -> bool:
    import time

    if isinstance(node.get("online"), bool):
        return bool(node["online"])
    if isinstance(node.get("connected"), bool):
        return bool(node["connected"])
    last = node.get("last_seen")
    if isinstance(last, dict):
        sec = last.get("seconds")
        if isinstance(sec, int) and sec > 0:
            return (time.time() - sec) <= 180
    return False


def _node_user(node: dict[str, object]) -> str:
    user = node.get("user")
    if isinstance(user, dict):
        return str(user.get("display_name") or user.get("name") or user.get("email") or "Unknown")
    return str(user or "Unknown")


def _node_addresses(node: dict[str, object]) -> str:
    addresses = node.get("ip_addresses") or node.get("ipAddresses")
    if isinstance(addresses, list):
        return ", ".join(str(address) for address in addresses[:4])
    return str(addresses or "")


def _node_last_seen(node: dict[str, object]) -> str:
    value = node.get("last_seen")
    if isinstance(value, dict) and isinstance(value.get("seconds"), int):
        return datetime.fromtimestamp(value["seconds"], tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
    return str(value or "Never")


def check_users(cfg: Config, infra_ip: str, root: Path) -> VerifyCheck:
    """Headscale users list — at least one user namespace exists."""
    from toolkit.services.sdk import VerifyCheck

    if cfg.domain == "localhost":
        return VerifyCheck("headscale", "users", True, "skipped (localhost)")

    try:
        users = _headscale_users(cfg, infra_ip, root)
    except RuntimeError as exc:
        return VerifyCheck("headscale", "users", False, str(exc)[:120])
    count = len(users)
    return VerifyCheck(
        "headscale",
        "users",
        count >= 1,
        f"{count} user(s)" if count else "no users — OIDC login or `headscale users create` required",
    )


def check_nodes(cfg: Config, infra_ip: str, root: Path) -> VerifyCheck:
    """Headscale mesh inventory — online node count."""
    from toolkit.services.sdk import VerifyCheck

    try:
        nodes = _headscale_nodes(cfg, infra_ip, root)
    except RuntimeError as exc:
        return VerifyCheck("headscale", "nodes", False, str(exc)[:120])
    if not nodes:
        return VerifyCheck(
            "headscale",
            "nodes",
            False,
            "no mesh nodes — enroll laptops via `mesh join` (OIDC); fleet VPS via `fleet add`",
        )

    online = sum(1 for n in nodes if _mesh_node_online(n))
    return VerifyCheck("headscale", "nodes", online >= 1, f"{online}/{len(nodes)} node(s) online")


def check_subnet_router(cfg: Config, infra_ip: str, root: Path) -> VerifyCheck:
    """Infra host tailscale advertises homelab LAN routes for mesh clients."""
    from toolkit.services.sdk import VerifyCheck, VerifyStatus

    if not cfg.is_multi_node or not getattr(cfg.fleet, "mesh_subnet_router", True):
        return VerifyCheck(
            "headscale",
            "subnet_router",
            True,
            "not required",
            status=VerifyStatus.NOT_APPLICABLE,
        )
    import json

    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
    from toolkit.core.registry.mesh import mesh_lan_cidr

    cidr = mesh_lan_cidr(cfg)
    cmd = "tailscale status --json"
    if cfg.is_multi_node:
        rc, out, _ = ssh_run_on_vm(cfg, infra_ip, cmd, root=root, timeout=30)
    else:
        import subprocess

        proc = subprocess.run(
            ["tailscale", "status", "--json"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        rc, out = proc.returncode, proc.stdout or ""
    if rc != 0 or not (out or "").strip():
        return VerifyCheck("headscale", "subnet_router", False, "tailscale not on infra host")
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return VerifyCheck("headscale", "subnet_router", False, "invalid tailscale status")
    routes = data.get("Self", {}).get("PrimaryRoutes") or data.get("Self", {}).get("Routes") or []
    allowed = str(data.get("Self", {}).get("AllowedIPs") or "")
    ok = cidr in routes or cidr in allowed
    return VerifyCheck(
        "headscale",
        "subnet_router",
        ok,
        f"advertising {cidr}" if ok else "run `homelab-toolkit mesh router` on infra",
    )


def check_acl(cfg: Config, root: Path) -> VerifyCheck:
    """Tags-only ACL policy rendered with tagOwners + no allow-all rule."""
    from toolkit.services.sdk import VerifyCheck, VerifyStatus

    if not cfg.category_enabled("security"):
        return VerifyCheck(
            "headscale",
            "acl",
            True,
            "security not enabled",
            status=VerifyStatus.NOT_APPLICABLE,
        )
    acl_file = root / "generated" / "headscale" / "acl.hujson"
    if not acl_file.is_file():
        return VerifyCheck("headscale", "acl", False, "generated/headscale/acl.hujson missing (run generate)")
    body = acl_file.read_text()
    if "tagOwners" not in body:
        return VerifyCheck("headscale", "acl", False, "tagOwners section missing")
    if "autoApprovers" not in body:
        return VerifyCheck("headscale", "acl", False, "autoApprovers section missing")
    if '"src": ["*"]' in body:
        return VerifyCheck("headscale", "acl", False, "allow-all rule still present (src=*)")
    if "autogroup:member" not in body:
        return VerifyCheck("headscale", "acl", False, "autogroup:member missing (owner rule)")
    return VerifyCheck("headscale", "acl", True, "tags-only ACL rendered (tagOwners + autogroup:member)")
