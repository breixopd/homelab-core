"""adguard service plugin.

The base ``ServicePlugin`` defaults (compose_service, env_vars,
secrets_needed, credentials) read from ``service.yaml``; this file overrides
only what needs custom Python logic (``verify``).

``verify()`` probes AdGuard's rewrite/control API and the WAN DNS listener.
Multi-VM HTTP-into-container curls use :func:`docker_curl` from
:mod:`toolkit.services.sdk`; Basic auth uses
:func:`basic_auth_header` from :mod:`toolkit.services.sdk`. Each check
reaches its target whether the homelab is single-host or multi-VM.
"""

from __future__ import annotations

import json
import subprocess
import time
import urllib.error
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.services import ServicePlugin
from toolkit.services.sdk import adguard_list_rewrites, ssh_on_vm

if TYPE_CHECKING:
    from toolkit.core.config.config import Config, ExternalHost
    from toolkit.services.sdk import VerifyCheck


class AdguardPlugin(ServicePlugin):
    service = "adguard"
    category = "management"

    def host_integration_status(
        self,
        integration: str,
        cfg: Config,
        host: ExternalHost,
        root: Path,
    ) -> tuple[bool | None, str] | None:
        if integration != "dns-client":
            raise ValueError(f"unsupported AdGuard host integration: {integration}")
        from toolkit.services.sdk import systemd_unit_active

        active = systemd_unit_active(root, host, "systemd-resolved")
        if active is True:
            return True, "systemd-resolved active"
        if active is False:
            return False, "systemd-resolved inactive"
        return None, "could not query systemd-resolved"

    # ── post_start ───────────────────────────────────────────────────────────
    def post_start(self, cfg: Config, secrets: dict[str, str], *, root: Path | None = None) -> list[str]:
        """Complete AdGuard Home first-run wizard so the rewrite API serves DNS.

        Raises on first-run setup failure so the mesh and DNS rewrite
        reconciliation cannot report success against an unconfigured AdGuard.
        """
        import importlib

        import httpx
        from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT
        from toolkit.core.manifest.placement import service_address
        from toolkit.core.ops.automation import resolve_docker_service_url
        from toolkit.core.ops.dns import AdGuardDNS

        bootstrap = importlib.import_module("toolkit.services.adguard.bootstrap")
        logs = bootstrap.bootstrap_adguard(cfg, secrets)
        password = secrets.get("ADGUARD_ADMIN_PASSWORD", "")
        if password and any("setup failed" in line.lower() for line in logs):
            raise RuntimeError("AdGuard first-run setup failed")

        install_root = root or Path(DEFAULT_HOMELAB_ROOT)
        address = service_address(cfg, "adguard")
        retry_errors = (
            OSError,
            urllib.error.URLError,
            httpx.HTTPError,
            RuntimeError,
            json.JSONDecodeError,
            KeyError,
        )
        for attempt in range(1, 13):
            try:
                client = AdGuardDNS(
                    base_url=resolve_docker_service_url("adguard", 3000),
                    password=password,
                )
                mesh_stats = client.sync_mesh_service_rewrites(cfg, address)
                internal_stats = client.sync_internal_dns(cfg, address)
                external_stats = client.sync_external_hosts_rewrites(cfg)
                node_stats = {"created": 0, "updated": 0, "removed": 0}
                try:
                    from toolkit.services.headscale.bootstrap import list_mesh_nodes

                    nodes = list_mesh_nodes(cfg, install_root)
                    if nodes:
                        node_stats = client.sync_mesh_node_rewrites(cfg, nodes)
                except retry_errors as exc:
                    logs.append(f"AdGuard: mesh node sync skipped ({str(exc)[:80]})")
                logs.append(
                    f"AdGuard: mesh +{mesh_stats['created']} ~{mesh_stats['updated']} "
                    f"(unchanged {mesh_stats['unchanged']}), "
                    f"mesh-nodes +{node_stats['created']} ~{node_stats['updated']} "
                    f"(removed {node_stats['removed']}), "
                    f"internal +{internal_stats['created']} ~{internal_stats['updated']}, "
                    f"external +{external_stats['created']} ~{external_stats['updated']}"
                )
                return logs
            except retry_errors as exc:
                if attempt == 12:
                    raise RuntimeError(f"AdGuard DNS rewrite sync failed after retries: {exc}") from exc
                time.sleep(10)
        raise RuntimeError("AdGuard DNS rewrite sync exhausted without a result")

    # ── verify ──────────────────────────────────────────────────────────────
    def verify(self, cfg: Config, secrets: dict[str, str], vm_ip: str, root: Path) -> list[VerifyCheck]:
        """Verify AdGuard DNS rewrites and public DNS listener.

        Checks:
          * ``dns_rewrites``   — rewrite list non-empty
          * ``fqdn_set``       — every enabled service FQDN has a rewrite
          * ``external_hosts`` — fleet external_hosts rewrites match config
          * ``dns_public``     — private port 53 and public DNS record when WAN DNS is on
        """
        from toolkit.services.sdk import VerifyCheck

        checks: list[VerifyCheck] = []

        rewrites, err = adguard_list_rewrites(cfg, vm_ip, root, secrets)
        if rewrites is None:
            checks.append(VerifyCheck("adguard", "dns_rewrites", False, err))
            checks.append(VerifyCheck("adguard", "fqdn_set", False, err))
            checks.append(VerifyCheck("adguard", "external_hosts", False, err))
        else:
            # dns_rewrites
            count = len(rewrites)
            checks.append(VerifyCheck("adguard", "dns_rewrites", count > 0, f"{count} rewrites"))

            # fqdn_set — rewrites must cover every private service FQDN.
            from toolkit.core.manifest.routes import private_routes

            desired = {route.host for route in private_routes(cfg)}
            present = {r.get("domain", "") for r in rewrites if isinstance(r, dict)}
            missing = sorted(desired - present)
            ok = not missing
            detail = (
                f"{len(desired) - len(missing)}/{len(desired)} private service FQDNs"
                if desired
                else "no private service FQDNs"
            )
            if missing:
                detail += f" (missing: {', '.join(missing[:3])}{'…' if len(missing) > 3 else ''})"
            checks.append(VerifyCheck("adguard", "fqdn_set", ok, detail))

            # external_hosts rewrites must match config (FQDN → IP)
            from toolkit.core.ops.dns import external_hosts_private_rewrites

            desired_hosts = external_hosts_private_rewrites(cfg)
            if not desired_hosts:
                checks.append(VerifyCheck("adguard", "external_hosts", True, "no external_hosts configured"))
            else:
                present_hosts = {
                    r.get("domain", ""): r.get("answer", "")
                    for r in rewrites
                    if isinstance(r, dict) and r.get("domain")
                }
                h_missing = sorted(fqdn for fqdn in desired_hosts if fqdn not in present_hosts)
                h_wrong = sorted(fqdn for fqdn, ip in desired_hosts.items() if present_hosts.get(fqdn) != ip)
                h_ok = not h_missing and not h_wrong
                h_detail = f"{len(desired_hosts) - len(h_missing)}/{len(desired_hosts)} external host rewrites"
                if h_missing:
                    h_detail += f" (missing: {', '.join(h_missing[:3])}{'…' if len(h_missing) > 3 else ''})"
                if h_wrong:
                    h_detail += f" (wrong IP: {', '.join(h_wrong[:2])}{'…' if len(h_wrong) > 2 else ''})"
                checks.append(VerifyCheck("adguard", "external_hosts", h_ok, h_detail))

        checks.append(self._check_dns_public(cfg, vm_ip, root))
        checks.extend(self._check_protection_status(cfg, vm_ip, root, secrets))
        checks.append(self._check_dns_resolve(cfg, vm_ip, root))
        return checks

    def _check_protection_status(
        self, cfg: Config, vm_ip: str, root: Path, secrets: dict[str, str]
    ) -> list[VerifyCheck]:
        """AdGuard running with filtering enabled (not paused)."""
        from toolkit.services.sdk import VerifyCheck, adguard_control_url, basic_auth_header, docker_curl

        if cfg.domain == "localhost":
            return [VerifyCheck("adguard", "protection_status", True, "skipped (localhost)")]

        password = secrets.get("ADGUARD_ADMIN_PASSWORD", "")
        if not password:
            return [VerifyCheck("adguard", "protection_status", False, "ADGUARD_ADMIN_PASSWORD not set")]

        auth = {"Authorization": basic_auth_header("admin", password)}
        rc, body = docker_curl(
            cfg,
            vm_ip,
            "adguard",
            f"{adguard_control_url(internal=True)}/status",
            root=root,
            headers=auth,
        )
        if rc != 0 or not body:
            return [VerifyCheck("adguard", "protection_status", False, "status API unreachable")]
        try:
            import json

            status = json.loads(body)
        except json.JSONDecodeError:
            return [VerifyCheck("adguard", "protection_status", False, "invalid status JSON")]
        running = bool(status.get("running"))
        protected = bool(status.get("protection_enabled"))
        paused = int(status.get("protection_disabled_duration") or 0)
        ok = running and protected and paused == 0
        detail = (
            "running + filtering enabled"
            if ok
            else f"running={running} protection_enabled={protected} paused={paused}s"
        )
        return [VerifyCheck("adguard", "protection_status", ok, detail)]

    def _check_dns_resolve(self, cfg: Config, infra_ip: str, root: Path) -> VerifyCheck:
        """Functional DNS: auth.<domain> resolves via infra resolver."""
        from toolkit.services.sdk import VerifyCheck, ssh_on_vm

        if cfg.domain == "localhost":
            return VerifyCheck("adguard", "dns_resolve", True, "skipped (localhost)")

        fqdn = f"auth.{cfg.domain}"
        shell = f"dig +short @{infra_ip} {fqdn} A 2>/dev/null | head -1"
        rc, out, _ = ssh_on_vm(cfg, infra_ip, shell, root=root, timeout=12)
        answer = (out or "").strip().splitlines()[0] if (out or "").strip() else ""
        ok = rc == 0 and bool(answer) and answer[0].isdigit()
        return VerifyCheck(
            "adguard",
            "dns_resolve",
            ok,
            f"{fqdn} → {answer}" if ok else f"{fqdn} did not resolve via {infra_ip}",
        )

    def _check_dns_public(self, cfg: Config, infra_ip: str, root: Path) -> VerifyCheck:
        """AdGuard DNS must listen privately and publish its resolver record when enabled."""
        from toolkit.core.ops.dns import dns_public_access_enabled, dns_resolver_fqdn, resolve_public_dns_ip
        from toolkit.services.sdk import VerifyCheck, VerifyStatus

        if not dns_public_access_enabled(cfg):
            return VerifyCheck(
                "adguard",
                "dns_public",
                True,
                "dns_public_access disabled",
                status=VerifyStatus.NOT_APPLICABLE,
            )

        rc, out, _ = ssh_on_vm(
            cfg,
            infra_ip,
            "ss -H -lun '( sport = :53 )' 2>/dev/null | head -5",
            root=root,
            timeout=20,
        )
        listening = rc == 0 and ":53" in (out or "")
        bind_ok = listening and (f"{infra_ip}:53" in out or "0.0.0.0:53" in out or "[::]:53" in out or "*:53" in out)
        if not bind_ok:
            return VerifyCheck(
                "adguard",
                "dns_public",
                False,
                (out or f"port 53 not listening on {infra_ip}")[:120],
            )

        public_ip, _ = resolve_public_dns_ip(cfg)
        fqdn = dns_resolver_fqdn(cfg)
        if public_ip:
            try:
                proc = subprocess.run(
                    ["host", "-t", "A", fqdn],
                    capture_output=True,
                    text=True,
                    timeout=12,
                    check=False,
                )
                resolves_public = proc.returncode == 0 and public_ip in (proc.stdout or "")
            except OSError:
                resolves_public = False
            if resolves_public:
                return VerifyCheck("adguard", "dns_public", True, f"port 53 open; {fqdn} → {public_ip}")
            return VerifyCheck(
                "adguard",
                "dns_public",
                False,
                f"port 53 open but public A for {fqdn} is missing (expected {public_ip})",
                status=VerifyStatus.NOT_READY,
            )
        return VerifyCheck(
            "adguard",
            "dns_public",
            False,
            "port 53 open but public resolver IP is unavailable",
            status=VerifyStatus.NOT_READY,
        )
