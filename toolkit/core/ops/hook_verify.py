"""Post-hook verification — dispatch to per-service plugins + framework cross-service checks.

Each service plugin's ``verify()`` owns its per-service checks (OIDC, health,
datasources, etc.). This module is the thin dispatcher: it runs framework-
level cross-VM / cross-service checks that don't belong to a single plugin
(SSSD, LDAP getent, forward-auth routes, repo parity, Cloudflare DNS parity,
mesh client access, Komodo periphery, mail DNS records), then dispatches to
every enabled service plugin's ``verify()`` and aggregates the results.

VerifyCheck / HookVerifyResult / format_verify_report are re-exported from
``toolkit.core.verify`` so existing callers keep working.
"""

from __future__ import annotations

import logging
import shlex
import subprocess
import time as _time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.core.verify.models import HookVerifyResult, VerifyCheck, format_verify_report

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

logger = logging.getLogger(__name__)

__all__ = [
    "HookVerifyResult",
    "VerifyCheck",
    "format_verify_report",
    "verify_hooks",
]


# ── Framework-level shared helpers ──────────────────────────────────────────


def _parse_curl_headers(output: str) -> tuple[int | None, dict[str, str]]:
    """Parse ``curl -I`` / ``curl -skI`` output into ``(status, headers)``."""
    status: int | None = None
    headers: dict[str, str] = {}
    for line in output.splitlines():
        if line.upper().startswith("HTTP/"):
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                status = int(parts[1])
        elif ":" in line:
            key, val = line.split(":", 1)
            headers[key.strip().lower()] = val.strip()
    return status, headers


# ── Framework-level cross-service checks ─────────────────────────────────────


def _check_caddy_forward_auth_route(
    cfg: Config,
    service: str,
    host: str,
    source_ip: str,
    caddy_ip: str,
    root: Path,
) -> VerifyCheck:
    """Unauthenticated request to a forward-auth route should redirect to Authelia."""
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

    auth_host = f"auth.{cfg.domain}"
    shell = (
        f"curl -skI --max-time 10 --resolve {host}:443:{caddy_ip} -H 'X-Forwarded-Proto: https' https://{host}/ 2>&1"
    )
    rc, out, _ = ssh_run_on_vm(cfg, source_ip, shell, root=root, timeout=20)
    if rc != 0 and not out:
        return VerifyCheck(service, "forward_auth", False, "curl failed")
    status, headers = _parse_curl_headers(out)
    location = headers.get("location", "")
    combined = (out or "").lower()
    if status is None and "location:" in combined:
        for line in out.splitlines():
            if line.lower().startswith("location:"):
                location = line.split(":", 1)[1].strip()
                status = 302
                break
    ok = status in (302, 307, 308) and auth_host in location
    if ok:
        detail = f"HTTP {status} → {location[:80]}"
    elif status is None:
        detail = "could not parse HTTP status"
    else:
        detail = f"HTTP {status}, location={location[:80] or '(missing)'}"
    return VerifyCheck(service, "forward_auth", ok, detail)


def _check_caddy_split_native_paths(
    cfg: Config,
    service: str,
    host: str,
    paths: tuple[str, ...],
    vm_ip: str,
    root: Path,
    *,
    probe_statuses: tuple[int, ...] = (),
    probe_method: str = "GET",
) -> list[VerifyCheck]:
    """Verify every split-auth passthrough reaches the application, not Authelia."""
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

    commands: list[str] = []
    for index, path in enumerate(paths):
        url = shlex.quote(f"https://{host}{path}")
        resolve = shlex.quote(f"{host}:443:127.0.0.1")
        commands.append(
            "(result=$(curl -sk --max-time 12 "
            f"-X {shlex.quote(probe_method)} "
            f"--resolve {resolve} -o /dev/null -w '%{{http_code}}\\t%{{redirect_url}}' {url} "
            "2>/dev/null || true); "
            f"printf '__HOMELAB_SPLIT_{index}__\\t%s\\n' \"$result\") &"
        )
        if (index + 1) % 8 == 0:
            commands.append("wait")
    if not commands or commands[-1] != "wait":
        commands.append("wait")
    rc, out, err = ssh_run_on_vm(cfg, vm_ip, "\n".join(commands), root=root, timeout=30)

    observed: dict[int, tuple[int | None, str]] = {}
    for line in (out or "").splitlines():
        marker, separator, payload = line.partition("\t")
        if not separator or not marker.startswith("__HOMELAB_SPLIT_") or not marker.endswith("__"):
            continue
        index_text = marker.removeprefix("__HOMELAB_SPLIT_").removesuffix("__")
        if not index_text.isdigit():
            continue
        status_text, _, location = payload.partition("\t")
        status = int(status_text) if status_text.isdigit() else None
        observed[int(index_text)] = (status, location)

    auth_host = f"auth.{cfg.domain}"
    checks: list[VerifyCheck] = []
    for index, path in enumerate(paths):
        status, location = observed.get(index, (None, ""))
        status_ok = status in probe_statuses if probe_statuses else status is not None and 100 <= status < 500
        ok = status_ok and auth_host not in location
        if status is not None:
            detail = f"{path} HTTP {status}"
            if location:
                detail += f" → {location[:80]}"
        else:
            detail = f"{path} probe failed: {(err or f'remote exit {rc}')[:80]}"
        checks.append(VerifyCheck(service, f"native_path:{path}", ok, detail))
    return checks


def _check_forward_auth_routes(cfg: Config, root: Path, *, vm_role: str | None = None) -> list[VerifyCheck]:
    """Verify manifest-declared forward auth and split-auth passthrough paths."""
    if not cfg.category_enabled("management"):
        return []
    from toolkit.core.manifest.catalog import provider_service_name
    from toolkit.core.manifest.placement import service_address, service_node
    from toolkit.core.manifest.routes import compile_routes

    checks: list[VerifyCheck] = []
    seen: set[tuple[str, str]] = set()
    skip_hosts = {f"auth.{cfg.domain}", f"vpn.{cfg.domain}"}
    ingress_service = provider_service_name("ingress")
    caddy_node = service_node(cfg, ingress_service)
    caddy_ip = service_address(cfg, ingress_service) if cfg.is_multi_node else "127.0.0.1"
    if cfg.is_multi_node:
        peer_role = next((node for node in cfg.enabled_nodes if node != caddy_node), caddy_node)
        probe_source_ip = cfg.node_ip(peer_role)
    else:
        probe_source_ip = "127.0.0.1"

    for route in compile_routes(cfg):
        if vm_role and route.node != vm_role:
            continue
        if route.match is not None or route.host in skip_hosts:
            continue
        if route.auth.mode not in {"forward_auth", "split"}:
            continue
        key = (route.service, route.host)
        if key in seen:
            continue
        seen.add(key)
        checks.append(
            _check_caddy_forward_auth_route(
                cfg,
                route.service,
                route.host,
                probe_source_ip,
                caddy_ip,
                root,
            )
        )
        if route.auth.mode == "split":
            checks.extend(
                _check_caddy_split_native_paths(
                    cfg,
                    route.service,
                    route.host,
                    route.auth.passthrough_paths,
                    caddy_ip,
                    root,
                    probe_statuses=route.auth.probe_statuses,
                    probe_method=route.auth.probe_method,
                )
            )
    return checks


def _check_sssd_active(cfg: Config, vm: str, vm_ip: str, root: Path) -> VerifyCheck:
    """L1: SSSD must be active for LDAP SSH on managed machines."""
    if not cfg.is_multi_node:
        return VerifyCheck("sssd", vm, True, "single-host skip")
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm

    # SSSD may be briefly inactive right after a deploy restarts it. Retry for
    # ~30s before declaring failure — avoids false negatives in post-deploy verify.
    out = ""
    rc = 1
    for _attempt in range(6):
        rc, out, _ = ssh_run_on_vm(cfg, vm_ip, "systemctl is-active sssd", root=root, timeout=20)
        if rc == 0 and (out or "").strip() == "active":
            return VerifyCheck("sssd", vm, True, "active")
        _time.sleep(5)
    active = rc == 0 and (out or "").strip() == "active"
    return VerifyCheck("sssd", vm, active, "active" if active else (out or "inactive").strip()[:80])


def _check_ldap_getent(cfg: Config, vm: str, vm_ip: str, root: Path) -> VerifyCheck:
    """Verify SSSD points at the infra LLDAP."""
    if not cfg.is_multi_node:
        return VerifyCheck("ldap", vm, True, "single-host skip")
    from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
    from toolkit.services.sdk.ldap import base_dn, ldap_url

    expected_base = base_dn(cfg)
    infra_ldap = ldap_url(cfg)
    shell = f"grep -q '{infra_ldap}' /etc/sssd/sssd.conf && grep -q '{expected_base}' /etc/sssd/sssd.conf"
    rc, out, err = ssh_run_on_vm(cfg, vm_ip, shell, root=root, timeout=20)
    ok = rc == 0
    detail = "LLDAP uri/base ok" if ok else (err or out or "sssd/ldap misconfigured")[:80]
    return VerifyCheck("ldap", vm, ok, detail)


def _check_repo_parity(cfg: Config, root: Path) -> list[VerifyCheck]:
    """C6: each guest's stamped + live commit hash must match the controller."""
    if not cfg.is_multi_node:
        return [VerifyCheck("repo-parity", "single-host", True, "single-host skip")]
    from toolkit.core.deploy.repo_parity import verify_repo_parity

    checks: list[VerifyCheck] = []
    for r in verify_repo_parity(root, cfg=cfg):
        checks.append(
            VerifyCheck(
                "repo-parity",
                r.vm,
                r.in_parity,
                (
                    f"controller {r.controller_sha[:12]} expected {r.expected_sha[:12]} "
                    f"guest {r.guest_sha[:12]}" + (f" — {r.detail}" if r.detail else "")
                ).strip(),
            )
        )
    return checks


def _check_cloudflare_public_dns_parity(cfg: Config, secrets: dict[str, str]) -> VerifyCheck:
    """Public Cloudflare A records must match desired routes + dns remote record."""
    if cfg.dns.provider.lower() != "cloudflare":
        return VerifyCheck("cloudflare", "public_dns", True, "not applicable (DNS provider is not Cloudflare)")
    if not secrets.get("CLOUDFLARE_API_TOKEN", "").strip():
        # Cloudflare is the configured DNS authority.  Treating a missing
        # credential as a successful skip makes a deploy appear healthy while
        # DNS drift (or a completely missing zone) remains undetectable.
        return VerifyCheck("cloudflare", "public_dns", False, "required CLOUDFLARE_API_TOKEN is unavailable")
    from toolkit.core.ops.dns import (
        cloudflare_client_from_secrets,
        desired_records_from_config,
        resolve_public_dns_ip,
    )

    public_ip, _ = resolve_public_dns_ip(cfg)
    if not public_ip:
        return VerifyCheck("cloudflare", "public_dns", False, "public IP unknown")

    desired_a = {
        (r.name.rstrip("."), r.type): (r.content, r.proxied)
        for r in desired_records_from_config(cfg, public_ip)
        if r.type == "A"
    }
    if not desired_a:
        return VerifyCheck("cloudflare", "public_dns", True, "no public A records expected")

    try:
        client = cloudflare_client_from_secrets(secrets, cfg.domain)
        existing = {
            (r.name.rstrip("."), r.type): (r.content, r.proxied)
            for r in client.list_all_managed_records()
            if r.type == "A"
        }
    except Exception as exc:
        return VerifyCheck("cloudflare", "public_dns", False, str(exc)[:120])

    missing = sorted(key for key in desired_a if key not in existing)
    mismatched = sorted(key for key in desired_a if key in existing and existing[key] != desired_a[key])
    ok = not missing and not mismatched
    detail = f"{len(desired_a) - len(missing)}/{len(desired_a)} public A records"
    if missing:
        detail += f" (missing: {missing[:3]}{'…' if len(missing) > 3 else ''})"
    if mismatched:
        detail += f" (mismatch: {mismatched[:2]}{'…' if len(mismatched) > 2 else ''})"
    return VerifyCheck("cloudflare", "public_dns", ok, detail)


def _check_private_fqdns_not_in_cloudflare(cfg: Config, secrets: dict[str, str]) -> VerifyCheck:
    """Private FQDNs must not have public Cloudflare records."""
    if cfg.dns.provider.lower() != "cloudflare":
        return VerifyCheck("cloudflare", "private_dns", True, "not applicable (DNS provider is not Cloudflare)")
    if not secrets.get("CLOUDFLARE_API_TOKEN", "").strip():
        return VerifyCheck("cloudflare", "private_dns", False, "required CLOUDFLARE_API_TOKEN is unavailable")
    from toolkit.core.ops.dns import (
        cloudflare_client_from_secrets,
        private_cloudflare_exceptions,
        private_route_fqdns,
    )

    private = private_route_fqdns(cfg) - private_cloudflare_exceptions(cfg)
    if not private:
        return VerifyCheck("cloudflare", "private_dns", True, "no private routes")

    try:
        client = cloudflare_client_from_secrets(secrets, cfg.domain)
        published = set()
        for r in client.list_records("A"):
            name = r.name.rstrip(".")
            if name not in private:
                continue
            published.add(name)
        published.update(r.name.rstrip(".") for r in client.list_records("CNAME") if r.name.rstrip(".") in private)
    except Exception as exc:
        return VerifyCheck("cloudflare", "private_dns", False, str(exc)[:120])

    leaked = sorted(published)
    ok = not leaked
    detail = (
        "no private FQDNs in Cloudflare" if ok else f"leaked: {', '.join(leaked[:4])}{'…' if len(leaked) > 4 else ''}"
    )
    return VerifyCheck("cloudflare", "private_dns", ok, detail)


def _check_mail_dns_records(cfg: Config, secrets: dict[str, str], root: Path) -> VerifyCheck:
    """G75: MX/SPF/DKIM/DMARC records present for mail domain."""
    if not cfg.category_enabled("email"):
        return VerifyCheck("mail", "dns", True, "email not enabled")
    domain = cfg.domain
    from toolkit.core.ops.dns import email_dns_records, resolve_public_dns_ip
    from toolkit.services.mailserver.bootstrap import fetch_dms_dkim_txt

    public_ip, _ = resolve_public_dns_ip(cfg)
    if not public_ip:
        return VerifyCheck("mail", "dns", False, "public IP unknown — cannot verify mail A record")
    dkim = fetch_dms_dkim_txt(domain, cfg=cfg)
    expected = {(r.name, r.type) for r in email_dns_records(domain, public_ip, dkim_txt=dkim)}

    if cfg.dns.provider.lower() == "cloudflare" and secrets.get("CLOUDFLARE_API_TOKEN"):
        from toolkit.core.ops.dns import cloudflare_client_from_secrets

        try:
            client = cloudflare_client_from_secrets(secrets, domain)
            existing = {(r.name.rstrip("."), r.type) for r in client.list_all_managed_records()}
            missing = sorted(expected - existing)
            ok = len(missing) == 0
            detail = "MX/SPF/DKIM/DMARC present" if ok else f"missing: {missing[:4]}"
            return VerifyCheck("mail", "dns", ok, detail)
        except Exception as exc:
            return VerifyCheck("mail", "dns", False, str(exc)[:120])

    # Fallback: dig/host checks for critical records
    checks_ok = 0
    for name, rtype in sorted(expected):
        try:
            proc = subprocess.run(["host", "-t", rtype, name], capture_output=True, text=True, timeout=10, check=False)
            if proc.returncode == 0 and (proc.stdout or proc.stderr):
                checks_ok += 1
        except OSError:
            pass
    # Every generated record is required.  Allowing one missing record makes a
    # broken MX/SPF/DMARC entry look healthy and undermines the mail gate.
    ok = checks_ok == len(expected)
    return VerifyCheck("mail", "dns", ok, f"{checks_ok}/{len(expected)} record types resolve")


# ── Dispatcher ──────────────────────────────────────────────────────────────


def verify_hooks(
    cfg: Config,
    secrets: dict[str, str],
    root: Path | None = None,
    *,
    vm: str | None = None,
    on_progress: Callable[[str], None] | None = None,
    only_services: frozenset[str] | None = None,
) -> HookVerifyResult:
    """Run framework cross-service checks, then dispatch every enabled service plugin's verify()."""
    root = root or Path.cwd()
    vm_role = (vm or "").strip().lower() or None

    def _runs_on(target: str) -> bool:
        return vm_role is None or vm_role == target

    from toolkit.core.manifest.placement import service_node

    def _runs_service(service: str) -> bool:
        return _runs_on(service_node(cfg, service))

    result = HookVerifyResult()

    from toolkit.services import enabled_service_plugins

    plugin_entries = enabled_service_plugins(cfg, node=vm_role)
    if only_services is not None:
        plugin_entries = [entry for entry in plugin_entries if entry[1].service in only_services]

    # ── Framework-level cross-service checks (not owned by any single plugin) ──
    # SSSD + LDAP getent: per-guest, run on every enabled VM role.
    for vm_name in cfg.enabled_nodes:
        if not _runs_on(vm_name):
            continue
        vm_ip = cfg.node_ip(vm_name) if cfg.is_multi_node else "localhost"
        result.checks.append(_check_sssd_active(cfg, vm_name, vm_ip, root))
        result.checks.append(_check_ldap_getent(cfg, vm_name, vm_ip, root))

    if cfg.category_enabled("management"):
        result.checks.extend(_check_forward_auth_routes(cfg, root, vm_role=vm_role))

    if cfg.category_enabled("management") and _runs_service("prometheus"):
        from toolkit.core.ops.monitoring_verify import verify_monitoring_stack

        result.checks.extend(verify_monitoring_stack(cfg, secrets, root))

    if vm_role is None:
        result.checks.extend(_check_repo_parity(cfg, root))

    if cfg.category_enabled("management") and vm_role is None:
        result.checks.append(_check_cloudflare_public_dns_parity(cfg, secrets))
        result.checks.append(_check_private_fqdns_not_in_cloudflare(cfg, secrets))
        for _, plugin in plugin_entries:
            result.checks.extend(plugin.controller_access_checks(cfg, root))

    if cfg.category_enabled("email") and _runs_service("mailserver"):
        result.checks.append(_check_mail_dns_records(cfg, secrets, root))

    # ── Plugin-dispatched per-service checks ─────────────────────────────────
    # Each enabled service plugin's verify() returns list[VerifyCheck]; route
    # each plugin to the address resolved from its service manifest.
    # A service that emits its own "health" check via verify() dedups against the
    # generic sweep.
    def _verify_plugin(entry) -> tuple[str, list[VerifyCheck]]:
        _category, plugin = entry
        try:
            return plugin.service, list(plugin.verify(cfg, secrets, plugin.runtime_address(cfg), root))
        except Exception as exc:
            return plugin.service, [
                VerifyCheck(plugin.service, "plugin", False, f"plugin verify error: {str(exc)[:80]}")
            ]

    total_plugins = len(plugin_entries)
    for index, entry in enumerate(plugin_entries, start=1):
        if on_progress is not None:
            on_progress(f"Verifying {entry[1].service} ({index}/{total_plugins})")
        _service, checks = _verify_plugin(entry)
        existing = {(check.service, check.check) for check in result.checks}
        for check in checks:
            if (check.service, check.check) not in existing:
                result.checks.append(check)
                existing.add((check.service, check.check))

    return result
