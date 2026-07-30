from __future__ import annotations

import ipaddress
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

import httpx

from toolkit.core.manifest.routes import managed_route_hosts, private_routes, public_routes, route_fqdn

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

HOMELAB_DNS_COMMENT = "homelab-toolkit managed"
HOMELAB_DNS_TAG = "managed-by:homelab-toolkit"


@dataclass
class DNSRecord:
    name: str
    type: str
    content: str
    proxied: bool = False
    record_id: str = ""
    disabled: bool = False  # When True, record is skipped during sync
    comment: str = ""
    tags: list[str] = field(default_factory=list)


logger = logging.getLogger(__name__)


def mark_managed_record(record: DNSRecord) -> DNSRecord:
    """Attach the Cloudflare ownership marker used for safe cleanup."""
    record.comment = HOMELAB_DNS_COMMENT
    return record


def is_homelab_managed_record(record: DNSRecord) -> bool:
    """Return True only for records this toolkit has explicitly claimed."""
    return HOMELAB_DNS_TAG in set(record.tags) or record.comment == HOMELAB_DNS_COMMENT


class CloudflareDNS:
    """Cloudflare DNS API client (API token auth)."""

    BASE = "https://api.cloudflare.com/client/v4"

    def __init__(self, api_token: str, zone_id: str = ""):
        self._token = api_token
        self._zone_id = zone_id

    def _request(self, method: str, path: str, data: dict | None = None) -> dict:
        """Cloudflare API call via httpx on IPv4 (urllib can hang on broken IPv6 routes)."""
        url = f"{self.BASE}{path}"
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        from toolkit.core.net.http_probe import http_client

        with http_client(timeout=30.0) as client:
            resp = client.request(method, url, headers=headers, json=data)
        if resp.status_code >= 400:
            try:
                payload = resp.json()
                errors = payload.get("errors", [])
                error_msgs = [e.get("message", str(e)) for e in errors]
                detail = "; ".join(error_msgs) or resp.text[:200]
            except json.JSONDecodeError:
                detail = resp.text[:200]
            raise RuntimeError(f"Cloudflare API error for {method} {path}: HTTP {resp.status_code} {detail}")
        result = resp.json()
        if not result.get("success"):
            errors = result.get("errors", [])
            error_msgs = [e.get("message", str(e)) for e in errors]
            raise RuntimeError(f"Cloudflare API error for {method} {path}: {'; '.join(error_msgs)}")
        return result

    def find_zone_id(self, domain: str) -> str:
        """Find zone ID for a domain."""
        result = self._request("GET", f"/zones?name={domain}")
        zones = result.get("result", [])
        if not zones:
            raise ValueError(f"No zone found for {domain}")
        self._zone_id = zones[0]["id"]
        return self._zone_id

    def list_records(self, record_type: str = "A") -> list[DNSRecord]:
        """List DNS records of a given type (paginated)."""
        records: list[DNSRecord] = []
        page = 1
        while True:
            result = self._request(
                "GET",
                f"/zones/{self._zone_id}/dns_records?type={record_type}&per_page=100&page={page}",
            )
            for r in result.get("result", []):
                records.append(
                    DNSRecord(
                        name=r["name"],
                        type=r["type"],
                        content=self._normalize_record_content(r),
                        proxied=r.get("proxied", False),
                        record_id=r["id"],
                        comment=r.get("comment", "") or "",
                        tags=list(r.get("tags") or []),
                    )
                )
            info = result.get("result_info") or {}
            total_pages = int(info.get("total_pages") or 1)
            if page >= total_pages:
                break
            page += 1
        return records

    @staticmethod
    def _normalize_record_content(record: dict) -> str:
        if record.get("type") == "MX":
            priority = record.get("priority", 0)
            target = record.get("content", "")
            return f"{priority} {target}".strip()
        return record.get("content", "")

    def list_all_managed_records(self) -> list[DNSRecord]:
        records: list[DNSRecord] = []
        for record_type in ("A", "AAAA", "CNAME", "MX", "TXT"):
            records.extend(self.list_records(record_type))
        return records

    def create_record(self, record: DNSRecord) -> str:
        """Create a DNS record. Returns record ID."""
        payload: dict[str, object] = {
            "type": record.type,
            "name": record.name,
            "content": record.content,
            "ttl": 1,
        }
        if record.type in ("A", "AAAA", "CNAME"):
            payload["proxied"] = record.proxied
        if record.type == "MX":
            priority, _, target = record.content.partition(" ")
            if priority.isdigit() and target:
                payload["priority"] = int(priority)
                payload["content"] = target
        if record.comment:
            payload["comment"] = record.comment
        if record.tags:
            payload["tags"] = record.tags
        result = self._request("POST", f"/zones/{self._zone_id}/dns_records", payload)
        return result["result"]["id"]

    def update_record(self, record: DNSRecord) -> None:
        """Update an existing DNS record."""
        payload: dict[str, object] = {
            "type": record.type,
            "name": record.name,
            "content": record.content,
            "ttl": 1,
        }
        if record.type in ("A", "AAAA", "CNAME"):
            payload["proxied"] = record.proxied
        if record.type == "MX":
            priority, _, target = record.content.partition(" ")
            if priority.isdigit() and target:
                payload["priority"] = int(priority)
                payload["content"] = target
        if record.comment:
            payload["comment"] = record.comment
        if record.tags:
            payload["tags"] = record.tags
        self._request("PUT", f"/zones/{self._zone_id}/dns_records/{record.record_id}", payload)

    @staticmethod
    def _metadata_changed(current: DNSRecord, desired: DNSRecord) -> bool:
        if current.comment != desired.comment:
            return True
        return not set(desired.tags).issubset(set(current.tags))

    def delete_record(self, record_id: str) -> None:
        """Delete a DNS record."""
        self._request("DELETE", f"/zones/{self._zone_id}/dns_records/{record_id}")

    def get_zone_setting(self, setting_id: str) -> str:
        """Return the current value of a zone setting (e.g. ssl)."""
        result = self._request("GET", f"/zones/{self._zone_id}/settings/{setting_id}")
        return str(result.get("result", {}).get("value", ""))

    def set_zone_setting(self, setting_id: str, value: str) -> None:
        """Update a zone setting."""
        self._request("PATCH", f"/zones/{self._zone_id}/settings/{setting_id}", {"value": value})

    def ensure_ssl_mode(self, mode: str = "full") -> bool:
        """Set Cloudflare SSL/TLS mode so proxied records work with origin HTTPS (Caddy)."""
        try:
            current = self.get_zone_setting("ssl")
        except RuntimeError:
            return False
        if current == mode:
            return False
        try:
            self.set_zone_setting("ssl", mode)
        except RuntimeError:
            return False
        return True

    def sync_records(self, desired: list[DNSRecord], dry_run: bool = False) -> dict[str, int]:
        """Sync DNS records: create missing, update changed. Never auto-deletes.

        Auto-deletion of stale records is intentionally NOT performed here to avoid
        accidentally removing non-homelab DNS records on the same domain. Use
        cleanup_stale_homelab_dns() for targeted cleanup of only homelab-managed records.
        """
        existing = self.list_all_managed_records()
        existing_by_key = {(r.name, r.type): r for r in existing}

        stats = {"created": 0, "updated": 0, "unchanged": 0, "deleted": 0}

        for want in desired:
            want = mark_managed_record(want)
            key = (want.name, want.type)
            current = existing_by_key.pop(key, None)
            if current is None:
                if not dry_run:
                    try:
                        self.create_record(want)
                    except RuntimeError as exc:
                        if "already exists" not in str(exc).lower():
                            raise
                stats["created"] += 1
            elif (
                current.content != want.content
                or (want.type in ("A", "AAAA", "CNAME") and current.proxied != want.proxied)
                or self._metadata_changed(current, want)
            ):
                want.record_id = current.record_id
                if not dry_run:
                    self.update_record(want)
                stats["updated"] += 1
            else:
                stats["unchanged"] += 1

        return stats


def email_dns_records(domain: str, mail_ip: str, *, dkim_txt: str = "") -> list[DNSRecord]:
    """Generate MX, SPF, DKIM, and DMARC DNS records for email."""
    records = [
        DNSRecord(name=domain, type="MX", content=f"10 mail.{domain}", proxied=False),
        DNSRecord(name=f"mail.{domain}", type="A", content=mail_ip, proxied=False),
        DNSRecord(name=domain, type="TXT", content=f"v=spf1 ip4:{mail_ip} mx -all", proxied=False),
        DNSRecord(
            name=f"_dmarc.{domain}",
            type="TXT",
            content=f"v=DMARC1; p=quarantine; rua=mailto:dmarc@{domain}",
            proxied=False,
        ),
        DNSRecord(name=f"autoconfig.{domain}", type="CNAME", content=f"mail.{domain}", proxied=False),
        DNSRecord(name=f"autodiscover.{domain}", type="CNAME", content=f"mail.{domain}", proxied=False),
    ]
    if dkim_txt and "placeholder" not in dkim_txt.lower():
        records.append(
            DNSRecord(
                name=f"mail._domainkey.{domain}",
                type="TXT",
                content=dkim_txt if dkim_txt.startswith("v=DKIM1") else f"v=DKIM1; {dkim_txt}",
                proxied=False,
            )
        )
    else:
        logger.info("DKIM TXT omitted until DMS bootstrap publishes a key (run deploy hooks on apps)")
    return records


def external_host_dns_label(host_name: str) -> str:
    """DNS-safe label for an external/fleet host (e.g. nas-01)."""
    label = re.sub(r"[^a-zA-Z0-9-]+", "-", host_name.lower()).strip("-")
    return label or "host"


def external_host_fqdn(host_name: str, domain: str) -> str:
    return f"{external_host_dns_label(host_name)}.{domain}"


def external_hosts_dns_records(cfg) -> list[DNSRecord]:
    """Public A records for fleet/external hosts (direct to host IP, not proxied)."""
    records: list[DNSRecord] = []
    seen: set[str] = set()
    for host in getattr(cfg, "external_hosts", None) or []:
        fqdn = external_host_fqdn(host.name, cfg.domain)
        if fqdn in seen:
            continue
        seen.add(fqdn)
        records.append(DNSRecord(name=fqdn, type="A", content=host.ip, proxied=False))
    return [mark_managed_record(r) for r in records]


def external_hosts_private_rewrites(cfg) -> dict[str, str]:
    """LAN/mesh AdGuard rewrites: external host FQDN → host IP."""
    return {external_host_fqdn(host.name, cfg.domain): host.ip for host in getattr(cfg, "external_hosts", None) or []}


def dns_public_access_enabled(cfg) -> bool:
    """True when AdGuard DNS should be reachable from the public internet."""
    return bool(cfg.network.dns_public_access)


def dns_resolver_fqdn(cfg) -> str:
    """FQDN for AdGuard DNS (dns.<domain>) — used for filtering on home devices."""
    return route_fqdn("dns", cfg.domain)


def dns_public_a_record(cfg, public_ip: str) -> DNSRecord | None:
    """Unproxied A record so home routers/phones can use dns.<domain> for filtering."""
    if not dns_public_access_enabled(cfg):
        return None
    return mark_managed_record(DNSRecord(name=dns_resolver_fqdn(cfg), type="A", content=public_ip, proxied=False))


def private_route_fqdns(cfg) -> set[str]:
    """Service FQDNs that must not appear in public Cloudflare DNS."""
    out = {route.host for route in private_routes(cfg)}
    if dns_public_access_enabled(cfg):
        out.add(dns_resolver_fqdn(cfg))
    return out


def private_cloudflare_exceptions(cfg) -> set[str]:
    """Private-route FQDNs that intentionally have a public DNS record."""
    exc: set[str] = set()
    if dns_public_access_enabled(cfg):
        exc.add(dns_resolver_fqdn(cfg))
    if cfg.category_enabled("email"):
        exc.add(f"mail.{cfg.domain}")
    return exc


def cloudflare_client_from_secrets(secrets: dict[str, str], domain: str) -> CloudflareDNS:
    """Authenticated Cloudflare client with zone resolved."""
    client = CloudflareDNS(
        api_token=secrets["CLOUDFLARE_API_TOKEN"],
        zone_id=secrets.get("CLOUDFLARE_ZONE_ID", ""),
    )
    if not client._zone_id:
        client.find_zone_id(domain)
    return client


def prune_leaked_private_cloudflare_records(cfg, client: CloudflareDNS, public_ip: str) -> int:
    """Remove public A records for mesh/LAN-only hostnames pointing at the homelab IP."""
    private = private_route_fqdns(cfg) - private_cloudflare_exceptions(cfg)
    if not private:
        return 0
    deleted = 0
    for record in client.list_records("A"):
        name = record.name.rstrip(".")
        if name in private and record.content == public_ip:
            client.delete_record(record.record_id)
            deleted += 1
    return deleted


def desired_records_from_config(cfg, public_ip: str) -> list[DNSRecord]:
    """Build desired DNS records from compiled routes, mail, and external hosts."""
    proxy = bool(getattr(cfg.dns, "proxy_enabled", True))
    records = []
    seen = set()
    for route in public_routes(cfg):
        if dns_public_access_enabled(cfg) and route.service == "adguard":
            continue
        if route.host not in seen:
            seen.add(route.host)
            records.append(DNSRecord(name=route.host, type="A", content=public_ip, proxied=proxy))

    if cfg.category_enabled("email"):
        dkim_txt = ""
        try:
            from toolkit.services.mailserver.bootstrap import fetch_dms_dkim_txt

            dkim_txt = fetch_dms_dkim_txt(cfg.domain, cfg=cfg)
        except Exception:
            logger.debug("Could not fetch DKIM TXT from DMS — will be omitted until bootstrap runs")
        records.extend(email_dns_records(cfg.domain, public_ip, dkim_txt=dkim_txt))

    for record in external_hosts_dns_records(cfg):
        if record.name not in seen:
            seen.add(record.name)
            records.append(record)

    public_dns = dns_public_a_record(cfg, public_ip)
    if public_dns and public_dns.name not in seen:
        seen.add(public_dns.name)
        records.append(public_dns)

    # Filter out disabled records (e.g. DKIM placeholder not yet ready), then
    # collapse competing declarations for one DNS name/type.  Specialized
    # records appended later (mail's DNS-only A record, for example) override
    # the generic route declaration while retaining deterministic ordering.
    records_by_key: dict[tuple[str, str], DNSRecord] = {}
    for record in records:
        if record.disabled:
            continue
        records_by_key[(record.name.rstrip("."), record.type)] = record

    return [mark_managed_record(record) for record in records_by_key.values()]


def cleanup_stale_homelab_dns(
    root: Path,
    *,
    on_log: Callable[[str], None] | None = None,
) -> int:
    """Delete stale Cloudflare records only when they carry this project's marker."""
    from toolkit.core.config.config import load_config
    from toolkit.core.config.storage import config_path, secrets_path
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    log = on_log or (lambda _msg: None)
    cfg = load_config(config_path(root))
    secrets = load_secrets_plaintext(secrets_path(root))
    token = secrets.get("CLOUDFLARE_API_TOKEN", "")
    if not token:
        log("CLOUDFLARE_API_TOKEN not set — skipping DNS cleanup")
        return 0

    client = CloudflareDNS(api_token=token, zone_id=secrets.get("CLOUDFLARE_ZONE_ID", ""))
    if not client._zone_id:
        client.find_zone_id(cfg.domain)

    return _cleanup_stale_homelab_dns_records(cfg, client, log)


def _cleanup_stale_homelab_dns_records(
    cfg: Config,
    client: CloudflareDNS,
    log: Callable[[str], None],
) -> int:
    """Prune only stale records explicitly tagged as managed by this project."""
    existing = client.list_all_managed_records()
    desired = desired_records_from_config(cfg, "0.0.0.0")
    desired_keys = {(r.name, r.type) for r in desired}

    deleted = 0
    skipped = 0
    for record in existing:
        if not (record.name == cfg.domain or record.name.endswith(f".{cfg.domain}")):
            continue
        if (record.name, record.type) in desired_keys:
            continue
        # DKIM discovery depends on the running mail container. Preserve the
        # last published key while email remains enabled if that dependency is
        # temporarily unavailable; selectors from older configurations are
        # still pruned because only the active selector is protected.
        active_dkim = f"mail._domainkey.{cfg.domain}"
        if cfg.category_enabled("email") and record.name == active_dkim and record.type == "TXT":
            log(f"  keeping TXT {record.name} (current DKIM key unavailable)")
            continue
        if not is_homelab_managed_record(record):
            log(f"  keeping {record.type} {record.name} (not homelab-managed)")
            skipped += 1
            continue
        client.delete_record(record.record_id)
        log(f"  deleted {record.type} {record.name}")
        deleted += 1

    if skipped:
        log(f"  {skipped} unmarked record(s) left untouched")
    return deleted


def resolve_public_dns_ip(cfg, override: str | None = None) -> tuple[str, str]:
    """Resolve the public IPv4 address to use for external DNS records.

    Resolution order:
    1. explicit override
    2. cfg.dns.public_ip
    3. IPv4 literal parsed from cfg.proxmox.api_url hostname
    """

    def _normalize_ipv4(value: str) -> str:
        try:
            candidate = ipaddress.ip_address(value.strip())
        except ValueError:
            return ""
        return str(candidate) if candidate.version == 4 else ""

    if override:
        normalized = _normalize_ipv4(override)
        if normalized:
            return normalized, "override"

    configured = _normalize_ipv4(getattr(cfg.dns, "public_ip", ""))
    if configured:
        return configured, "config"

    try:
        from toolkit.core.infra.autodetect import detect_public_ip

        autodetected = _normalize_ipv4(detect_public_ip())
        if autodetected:
            return autodetected, "autodetect"
    except Exception as exc:
        logger.warning("Failed to auto-detect public IP: %s", exc)

    try:
        from urllib.parse import urlparse

        parsed_url = urlparse(getattr(cfg.proxmox, "api_url", ""))
        fallback = _normalize_ipv4(parsed_url.hostname or "")
        if fallback:
            return fallback, "proxmox-url"
    except Exception as exc:
        logger.warning("Failed to parse proxmox URL for IP fallback: %s", exc)

    return "", "missing"


class AdGuardDNS:
    """AdGuard Home DNS rewrite API client for internal DNS."""

    def __init__(self, base_url: str = "http://adguard:3000", username: str = "admin", password: str = ""):
        self._base = base_url.rstrip("/")
        self._auth = (username, password)

    def _request(self, method: str, path: str, data: dict | None = None) -> dict | list:
        url = f"{self._base}{path}"
        from toolkit.core.net.http_probe import http_client

        with http_client(timeout=10.0) as client:
            resp = client.request(method, url, auth=self._auth, json=data)
        if resp.status_code >= 400:
            raise RuntimeError(f"AdGuard API error {method} {path}: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json() if resp.content else {}

    def list_rewrites(self) -> list:
        """List all DNS rewrites."""
        result = self._request("GET", "/control/rewrite/list")
        if not isinstance(result, list):
            raise RuntimeError("AdGuard rewrite list returned an unexpected response")
        if any(not isinstance(row, dict) for row in result):
            raise RuntimeError("AdGuard rewrite list contains an unexpected row")
        return result

    def add_rewrite(self, domain: str, answer: str) -> None:
        """Add a DNS rewrite rule."""
        self._request("POST", "/control/rewrite/add", {"domain": domain, "answer": answer})

    def delete_rewrite(self, domain: str, answer: str) -> None:
        """Delete a DNS rewrite rule."""
        self._request("POST", "/control/rewrite/delete", {"domain": domain, "answer": answer})

    def _rewrite_stats(
        self,
        desired: dict[str, str],
        *,
        managed_prefix: str | None = None,
        managed_domains: frozenset[str] | None = None,
    ) -> dict[str, int]:
        existing = {}
        for row in self.list_rewrites():
            domain = row.get("domain", "")
            answer = row.get("answer", "")
            if not isinstance(domain, str) or not isinstance(answer, str):
                raise RuntimeError("AdGuard rewrite list contains invalid domain or answer values")
            if managed_prefix and not domain.endswith(managed_prefix):
                continue
            if managed_domains is not None and domain not in managed_domains:
                continue
            existing[domain] = answer

        stats = {"created": 0, "updated": 0, "unchanged": 0, "removed": 0}
        for domain, answer in desired.items():
            if domain in existing:
                if existing[domain] == answer:
                    stats["unchanged"] += 1
                else:
                    self.delete_rewrite(domain, existing[domain])
                    self.add_rewrite(domain, answer)
                    stats["updated"] += 1
            else:
                self.add_rewrite(domain, answer)
                stats["created"] += 1

        if managed_prefix or managed_domains is not None:
            for domain, answer in list(existing.items()):
                if domain not in desired:
                    self.delete_rewrite(domain, answer)
                    stats["removed"] += 1
        return stats

    def sync_internal_dns(self, cfg, fallback_ip: str = "") -> dict[str, int]:
        """Sync manifest-declared internal service aliases to their owners."""
        from toolkit.core.manifest.catalog import load_service_catalog
        from toolkit.core.manifest.placement import service_address
        from toolkit.core.manifest.routes import service_is_enabled

        catalog = load_service_catalog()
        managed_domains = {
            f"{alias}.internal.{cfg.domain}" for manifest in catalog.manifests for alias in manifest.internal_aliases
        }
        desired: dict[str, str] = {}
        for manifest in catalog.manifests:
            if not manifest.internal_aliases or not service_is_enabled(cfg, manifest, catalog):
                continue
            try:
                address = service_address(cfg, manifest.name)
            except (KeyError, ValueError):
                address = fallback_ip
            if not address:
                continue
            for alias in manifest.internal_aliases:
                desired[f"{alias}.internal.{cfg.domain}"] = address
        return self._rewrite_stats(desired, managed_domains=frozenset(managed_domains))

    def sync_mesh_service_rewrites(self, cfg, infra_ip: str) -> dict[str, int]:
        """Rewrite private service FQDNs to infra Caddy for LAN and mesh clients."""
        desired = {route.host: infra_ip for route in private_routes(cfg)}
        return self._rewrite_stats(desired, managed_domains=managed_route_hosts(cfg))

    def sync_mesh_node_rewrites(self, cfg, mesh_nodes: list[tuple[str, str]]) -> dict[str, int]:
        """Rewrite Headscale mesh node names → Tailscale IPs under mesh.<domain>.

        Makes AdGuard the single source of truth for mesh names: LAN clients,
        containers (via daemon.json DNS), and mesh peers all resolve
        ``<node>.mesh.<domain>`` without relying on Tailscale MagicDNS.
        Stale rewrites under the mesh suffix are pruned when nodes disappear.
        """
        desired: dict[str, str] = {}
        mesh_suffix = f".mesh.{cfg.domain}"
        for name, ip in mesh_nodes:
            label = re.sub(r"[^a-zA-Z0-9-]+", "-", name.lower()).strip("-")
            if label and ip:
                desired[f"{label}{mesh_suffix}"] = ip
        return self._rewrite_stats(desired, managed_prefix=mesh_suffix)

    def remove_host_rewrite(self, fqdn: str) -> int:
        """Delete every rewrite rule for an external host FQDN. Returns count removed."""
        removed = 0
        for row in self.list_rewrites():
            if row.get("domain", "") == fqdn:
                self.delete_rewrite(fqdn, row.get("answer", ""))
                removed += 1
        return removed

    def sync_external_hosts_rewrites(self, cfg) -> dict[str, int]:
        """Point external/fleet host FQDNs at their real IPs (upsert only)."""
        desired = external_hosts_private_rewrites(cfg)
        if not desired:
            return {"created": 0, "updated": 0, "unchanged": 0, "removed": 0}
        stats = {"created": 0, "updated": 0, "unchanged": 0, "removed": 0}
        existing = {}
        for row in self.list_rewrites():
            domain = row.get("domain", "")
            answer = row.get("answer", "")
            if not isinstance(domain, str) or not isinstance(answer, str):
                raise RuntimeError("AdGuard rewrite list contains invalid domain or answer values")
            if domain in desired:
                existing[domain] = answer
        for domain, answer in desired.items():
            if domain in existing:
                if existing[domain] == answer:
                    stats["unchanged"] += 1
                else:
                    self.delete_rewrite(domain, existing[domain])
                    self.add_rewrite(domain, answer)
                    stats["updated"] += 1
            else:
                self.add_rewrite(domain, answer)
                stats["created"] += 1
        return stats


def sync_external_hosts_dns(
    root: Path,
    *,
    on_log: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Sync Cloudflare A records for external_hosts (no-op without API token)."""
    log = on_log or (lambda _msg: None)
    from toolkit.core.config.config import load_config
    from toolkit.core.config.storage import config_path, secrets_path
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    cfg = load_config(config_path(root))
    if not cfg.external_hosts:
        return {"created": 0, "updated": 0, "unchanged": 0}

    secrets = load_secrets_plaintext(secrets_path(root))
    token = secrets.get("CLOUDFLARE_API_TOKEN", "")
    if not token:
        log("External hosts DNS: CLOUDFLARE_API_TOKEN not set — LAN rewrites apply on deploy hooks")
        return {"created": 0, "updated": 0, "unchanged": 0, "skipped": 1}

    try:
        _, client = cloudflare_client_from_root(root)
        desired = external_hosts_dns_records(cfg)
        stats = client.sync_records(desired)
        log(f"External hosts DNS: {stats['created']} created, {stats['updated']} updated ({len(desired)} host(s))")
        return stats
    except (ValueError, RuntimeError, httpx.HTTPError) as exc:
        log(f"External hosts DNS: sync failed ({exc})")
        return {"created": 0, "updated": 0, "unchanged": 0, "error": 1}


def cloudflare_client_from_root(root: Path) -> tuple:
    """Load config and authenticated CloudflareDNS client from repo root."""
    from toolkit.core.config.config import load_config
    from toolkit.core.config.storage import config_path, secrets_path
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    cfg = load_config(config_path(root))
    secrets = load_secrets_plaintext(secrets_path(root))
    token = secrets.get("CLOUDFLARE_API_TOKEN", "")
    if not token:
        raise ValueError("CLOUDFLARE_API_TOKEN not set — run: homelab-toolkit secrets generate")
    return cfg, cloudflare_client_from_secrets(secrets, cfg.domain)


def adguard_client_from_root(root: Path, *, base_url: str = "http://adguard:3000") -> AdGuardDNS:
    """Build an AdGuard Home client using the stored admin password."""
    from toolkit.core.config.storage import secrets_path
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    secrets = load_secrets_plaintext(secrets_path(root))
    return AdGuardDNS(base_url=base_url, password=secrets.get("ADGUARD_ADMIN_PASSWORD", ""))


def remove_external_host_dns(
    root: Path,
    name: str,
    *,
    on_log: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Delete the Cloudflare A record and AdGuard rewrite for a removed external host.

    Network failures are logged as warnings and never raise — host removal must not
    hard-fail because Cloudflare or AdGuard is unreachable.
    """
    log = on_log or (lambda _msg: None)
    from toolkit.core.config.config import load_config
    from toolkit.core.config.storage import config_path

    cfg = load_config(config_path(root))
    fqdn = external_host_fqdn(name, cfg.domain)
    stats = {"cloudflare_deleted": 0, "adguard_deleted": 0}

    try:
        _, client = cloudflare_client_from_root(root)
        for record in client.list_records("A"):
            if record.name == fqdn:
                client.delete_record(record.record_id)
                stats["cloudflare_deleted"] += 1
        if stats["cloudflare_deleted"]:
            log(f"External host DNS: deleted Cloudflare A {fqdn}")
    except (ValueError, RuntimeError, httpx.HTTPError, OSError) as exc:
        log(f"External host DNS: Cloudflare cleanup skipped ({exc})")

    try:
        stats["adguard_deleted"] = adguard_client_from_root(root).remove_host_rewrite(fqdn)
        if stats["adguard_deleted"]:
            log(f"External host DNS: deleted AdGuard rewrite {fqdn}")
    except (ValueError, RuntimeError, httpx.HTTPError, OSError) as exc:
        log(f"External host DNS: AdGuard cleanup skipped ({exc})")

    return stats


def verify_dns_propagation(
    domain: str,
    expected_ip: str,
    max_retries: int = 10,
    interval: int = 30,
    *,
    proxied: bool = False,
) -> bool:
    """Poll DNS until *domain* resolves as expected or retries are exhausted.

    Unproxied records must resolve to ``expected_ip``.  Proxied records resolve
    through Cloudflare, so they are considered propagated once at least one
    globally routable IPv4 address is returned.
    """
    import socket
    import time

    expected = ipaddress.ip_address(expected_ip)
    if expected.version != 4:
        raise ValueError("expected_ip must be an IPv4 address")

    for attempt in range(1, max_retries + 1):
        try:
            resolved = socket.getaddrinfo(domain, None)
            ipv4s: set[ipaddress.IPv4Address] = set()
            for addr in resolved:
                if not (isinstance(addr, tuple) and len(addr) >= 5 and isinstance(addr[4], tuple) and addr[4]):
                    continue
                try:
                    address = ipaddress.ip_address(str(addr[4][0]))
                except ValueError:
                    continue
                if isinstance(address, ipaddress.IPv4Address):
                    ipv4s.add(address)
            if any(address.is_global for address in ipv4s) if proxied else expected in ipv4s:
                return True
            if attempt < max_retries:
                time.sleep(interval)
        except socket.gaierror:
            if attempt < max_retries:
                time.sleep(interval)
    return False


def sync_cloudflare_dns(
    root: Path,
    *,
    dry_run: bool = False,
    public_ip: str | None = None,
    on_log: Callable[[str], None] | None = None,
) -> dict[str, int]:
    """Sync public DNS records from config to Cloudflare (SSL mode + A/CNAME)."""
    log = on_log or (lambda _msg: None)
    cfg, client = cloudflare_client_from_root(root)
    ip, source = resolve_public_dns_ip(cfg, public_ip)
    if not ip:
        raise ValueError("No public DNS IPv4 configured")
    log(f"Public IP: {ip} ({source})")
    if dry_run:
        desired = desired_records_from_config(cfg, ip)
        return {"created": 0, "updated": 0, "unchanged": len(desired), "dry_run": 1}
    if getattr(cfg.dns, "proxy_enabled", True):
        try:
            current = client.get_zone_setting("ssl")
            if current == "full":
                log("Cloudflare SSL mode: full")
            elif client.ensure_ssl_mode("full"):
                log("Cloudflare SSL/TLS mode set to Full")
            else:
                log(f"Cloudflare SSL mode: {current or 'unknown'} (not updated via API)")
        except Exception:
            log("Cloudflare SSL: unverified (zone settings not readable via API)")
    desired = desired_records_from_config(cfg, ip)
    stats = client.sync_records(desired)
    stale = _cleanup_stale_homelab_dns_records(cfg, client, log)
    pruned = prune_leaked_private_cloudflare_records(cfg, client, ip)
    if stale:
        log(f"DNS pruned: {stale} stale managed record(s) removed")
    if pruned:
        log(f"DNS pruned: {pruned} internal A record(s) removed from Cloudflare")
    log(f"DNS synced: {stats['created']} created, {stats['updated']} updated")
    return {**stats, "pruned": stale + pruned}
