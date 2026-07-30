# Networking guide

## Architecture

Each machine plugin declares its own bridges, address, gateway, and prefix:

| Interface | Configuration | Purpose |
| --- | --- | --- |
| Public | `machines.<id>.public_bridge` | Optional WAN ingress on the selected ingress machine |
| Private | `machines.<id>.private_bridge` | Managed-machine traffic and service routing |

The built-in topology is only a default. Operators can add, remove, rename, or
renumber machine plugins without changing framework code:

| Machine | Default IP |
| --- | --- |
| infra-01 | 10.10.10.10 |
| media-01 | 10.10.10.11 |
| apps-01 | 10.10.10.12 |

## DNS and reverse proxy

Caddy terminates TLS and routes **`{subdomain}.{base_domain}`** to services. Every route is declared once in **`toolkit/services/<name>/service.yaml`** and compiled into Caddy, DNS, verification, and portal projections. Managed projects use the same compiler.

### Apex and public subdomains (typical)

| Host | Service | Default owner |
| --- | --- | --- |
| `{base_domain}` (apex) | Portal (bookmarks) | infra |
| `auth` | Authelia | infra |
| `homelab` | Homelab UI | infra |
| `ntfy` | Notifications | infra |
| `vpn` | Headscale | infra |
| `prometheus` | Prometheus (often internal exposure) | infra |
| `qbt` | qBittorrent | media |
| `prowlarr`, `sonarr`, `radarr`, `bazarr` | *arr stack | media |
| `jellyfin`, `plex`, `music`, `requests` | Media | media |
| `music-sync` | music-sync (internal exposure by default) | media |
| `cloud` | Nextcloud | apps |
| `photos` | Immich | apps |
| `vault` | Vaultwarden | apps |
| `s3` | SeaweedFS S3 API | apps |
| `files` | SeaweedFS Filer (web UI) | apps |
| `fmd` | Find My Device (native phone API; Authelia-protected browser UI) | apps |
| `mail` | Email | apps |
| `code`, `git` | Dev tools on apps (Gitea OCI `/v2` + PyPI on `git`) | apps |

### Internal-only (mesh / split DNS)

Examples include `grafana`, `komodo`, `siem`, `music-sync`, and `dns`. Private routes derive from strict service manifests and resolve through AdGuard or Headscale split DNS, not public Cloudflare.

### Cloudflare

With Cloudflare credentials in secrets, run **`homelab-toolkit dns sync`** (or use the UI DNS page). Cloudflare records are generated only from services whose effective exposure is `public`; internal services are intentionally omitted from public DNS.

The toolkit marks every Cloudflare record it creates with a `homelab-toolkit managed` comment. Cleanup only deletes stale records that still carry that marker, so unrelated zone records no longer require a hand-maintained protected subdomain list. Existing `managed-by:homelab-toolkit` tags are also recognized on plans that support DNS record tags.

## Adding routes

Add or edit the owning service manifest. For an independent digest-pinned container, use the Projects UI or `homelab-toolkit projects add`; reconciliation generates and validates all routing automatically.

## AdGuard and port 53

When `network.dns_public_access: true` (default), AdGuard remains bound to the
manifest-selected machine's private address and receives an **unproxied** Cloudflare
A record for `dns.<domain>`. Point home routers, phones, and TVs at that hostname or
your public IP for ad/tracker blocking.

The listener manifest automatically compiles Proxmox DNAT and guest-firewall rules for
WAN **53/tcp+udp**. LAN and mesh DNS remain available when public DNS is disabled. The
AdGuard **web UI** at `https://dns.<domain>` stays forward-auth protected via Caddy;
only the DNS protocol is exposed.

If AdGuard binds host port 53, resolve conflicts with `systemd-resolved` on the host (see deployment checklist).

### AdGuard as the single source of truth for DNS

Three rewrite categories are synced automatically on every deploy (`deploy hooks --node infra`):

| Rewrite suffix | Resolves to | Source of truth |
| --- | --- | --- |
| `*.example.com` (operator domain) | infra IP (10.10.10.10) | `toolkit/core/ops/dns.py: sync_mesh_service_rewrites()` driven by enabled categories + routes |
| `*.mesh.example.com` | Tailscale IP per node | `toolkit/core/bootstrap/mesh_bootstrap.py: list_mesh_nodes()` picks up Headscale node list + writes `node.mesh.<domain>` → Tailscale IP |
| `_internal.*.example.com` | infra IP (postgres / authelia / lldap / redis) | `sync_internal_dns()` for service-to-service use without Cloudflare |
| `host.<domain>` for external targets | remote host public IP | `sync_external_hosts_rewrites()` when fleet nodes are added |

Every LXC's Docker `daemon.json` points its DNS at AdGuard, so containers never need per-service DNS overrides. Tailscale runs with `--accept-dns=false` so it doesn't fight AdGuard for resolution. MagicDNS stays enabled as a fallback for mesh-only clients, but AdGuard is authoritative on LAN + inside containers.

```text
client → AdGuard:53 → 10.10.10.10 (infra Caddy terminates TLS)
                       (no Cloudflare hop on LAN/mesh/containers)
              ↑
              └─ *.example.com, *.mesh.example.com, _internal.*.example.com
```

## Firewall

CrowdSec / Fail2ban on infra — see [Security stack](security-stack.md).

## SSO

Authelia forward-auth + optional OIDC — see [Configuration](configuration.md).

## Public vs private

- **Caddy** terminates TLS for all service routes on its manifest-selected machine.
- **Cloudflare** receives only routes whose owning service manifest declares `exposure: public`.
- **AdGuard** rewrites private service FQDNs to the ingress IP for LAN and mesh clients.

```yaml
network:
  expose_via_internet: true
  mesh_ipv4_cidr: 100.64.0.0/10
  mesh_ipv6_cidr: fd7a:115c:a1e0::/48
  container_ipv4_cidr: 172.31.0.0/17
  container_network_prefix: 28
```

Change a route's exposure in its service plugin. The global internet switch
also controls Proxmox DNAT and guest firewall rules, so disabling it closes the
host edge as well as public DNS.

Plugin-local and declared relationship bridges receive deterministic subnets
from the container pool. This avoids Docker's small default address-pool limit
as the service catalog grows while retaining per-plugin isolation.

| Exposure | Examples |
| --- | --- |
| **Public** (optional Cloudflare) | Authelia, Seerr, Vaultwarden, FMD, Jellyfin, Nextcloud, Immich, Gitea, Homelab UI |
| **Private / LAN and mesh** (AdGuard only) | *arr, qBittorrent, Tdarr, Grafana, Komodo, AdGuard UI |

Point clients at AdGuard on infra or Headscale; avoid orange-clouding admin UIs in Cloudflare unless required.
