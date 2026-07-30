# Configuration and management

## Single source of truth

The deployment is driven by **`config.yaml`** at the repository root. The Homelab toolkit (CLI and web UI) reads this file; OpenTofu and Ansible consume **generated** copies produced by **`homelab-toolkit generate`**.

## Core schema (current `config.yaml`)

Top-level fields (Pydantic `Config` in `toolkit/core/config/config.py`):

| Field | Purpose |
| --- | --- |
| `domain` | Base domain for service URLs and Caddy (`localhost` uses HTTP in generated URLs). |
| `email` | Admin / ACME contact. |
| `timezone` | System `TZ` (default `Europe/Madrid`). |
| `services` | Category-plugin toggles discovered from `toolkit/categories`; required infrastructure is always on. |
| `service_settings` | Typed per-service overrides declared by each service manifest. |
| `notifications` | ntfy schedules and typed internal/external SMTP delivery policy. |
| `dns` | e.g. `provider: cloudflare` (used with DNS automation). |
| `machines` | Arbitrary Proxmox LXC/VM definitions, placement labels, addresses, resources, disks, and lifecycle ownership. |
| `ssh` | Shared managed-guest key, authentication mode, and transport timeouts. Guest users and ports are declared by each machine plugin. |
| `storage` | Planning fields (ZFS, RAID, disk counts) for UI/storage pages. |
| `external_hosts` | Extra machines (NAS, cache, fleet). Each host gets `{sanitized-name}.{domain}` → host IP in Cloudflare (if token set) and AdGuard rewrites on deploy hooks. |

Example:

```yaml
domain: example.com
email: admin@example.com
timezone: UTC

services:
  management: true
  media: true
  cloud: true
  notifications: true
  email: true
  security: true

service_settings:
  media-library:
    server: jellyfin
  gluetun:
    enabled: true
    server-countries: ''
  jellyfin:
    hardware-transcode: auto
  qbittorrent:
    listen-port: 6881
  tdarr:
    enabled: true
    cpu-workers: 0
    gpu-workers: -1
  media-cache:
    enabled: false
  music-sync:
    enabled: true

notifications:
  deploy_ntfy_url: https://ntfy.example.com/homelab
  smtp:
    mode: auto
```

The keys under `services` are not defined in core Python. They come from strict
category plugins and appear automatically in CLI and Web UI settings. Unknown
keys fail validation. Service settings behave the same way: the owning
`service.yaml` declares the type, bounds, choices, and default; `config.yaml`
stores only operator overrides.

The Web UI exposes these controls on each service page. Media-server selection
is owned by `media-library`; VPN policy by `gluetun`; transcode policy by
`jellyfin` and `tdarr`; and torrent networking by `qbittorrent`. Adding a
setting to one of those manifests automatically adds it to the controller,
CLI-visible catalog, validation, and Web UI without a core schema change.

`notifications.smtp.mode: auto` sends operator email through the enabled
mailserver plugin using its manifest-declared endpoint. Use `disabled` to turn
email delivery off, or configure an external relay:

```yaml
notifications:
  smtp:
    mode: external
    host: smtp.example.com
    port: 587
    starttls: true
    username: homelab@example.com
    password_secret: HOMELAB_SMTP_PASSWORD
    from_address: homelab@example.com
```

Set the named password with `homelab-toolkit secrets set`; it remains in the
encrypted secret store and is never written to tracked configuration.

### Optional `proxmox` block (IaC sync)

The typed provider contract used to generate `infrastructure/generated.auto.tfvars` and `automation/ansible/group_vars/generated.yml`. API credentials stay in the encrypted secret store and the gitignored `terraform.tfvars` generated from it.

```yaml
proxmox:
  api_url: "https://192.168.1.100:8006/api2/json"
  control_host: "pve-admin.example.net" # optional; otherwise derived from api_url
  ssh:
    user: root
    port: 22
    key_file: ~/.ssh/proxmox # stored only in config.local.yaml
    connect_timeout: 30
    command_timeout: 120
    retries: 3
  node: "pve"
  lxc_storage: "zfs-mirror"
  lxc_template_datastore: "local"
  lxc_template_url: "http://download.proxmox.com/images/system/debian-12-standard_12.12-1_amd64.tar.zst"
  lxc_template_checksum: "ff5c55cba730fc1e93bc7de3e0ea4aecb05c692094009cfcf2999973a56f15e5"
  tls_ca_file: "" # optional operator CA; otherwise fetched over authenticated SSH
  provision_machines: true
```

The control private-key path, shared guest private-key path, and guest public
key are stored only in gitignored `config.local.yaml`. The Web UI Settings page
manages both explicit identities without exposing key contents. OpenTofu
downloads the default LXC template, verifies its SHA-256, and uses the resulting
provider resource ID; machine plugins can select an existing template with
`template_file_id` instead.

Machine addresses and resources in OpenTofu come from top-level `machines`.
The committed `machines:` map in `config.yaml` is the authoritative live
topology: operators may add, remove, disable, rename, resize, or change any
LXC/VM definition without editing core code. The packaged three-machine
catalog is only a starter template for scaffolding and programmatic defaults,
not a hidden deployment source of truth. Shipped machine templates live at
`toolkit/machines/<template-id>/machine.yaml`.
Project-owned templates live at `machines/<template-id>/machine.yaml` and are
discovered without Python changes. Configured instances are stored under the
top-level `machines` desired-state map. LXC
plugins derive the `root` SSH login unless `ssh_user` is set. Managed VM
plugins own their cloud-init login and immutable image source:

```yaml
kind: vm
provider: proxmox
enabled: true
managed: true
hostname: worker-01
address: 10.10.10.20
gateway: 10.10.10.1
vmid: 820
labels: [compute]
admin_user: debian
ssh_port: 22
cloud_image_datastore: local # Proxmox Import content must be enabled
cloud_image_format: qcow2
cloud_image_url: https://cloud.debian.org/images/cloud/bookworm/20250630-2176/debian-12-generic-amd64-20250630-2176.qcow2
cloud_image_sha256: <lowercase-sha256>
```

OpenTofu verifies the digest, imports the image into the guest datastore, and
adds the serial device required by resized Debian and Ubuntu cloud images. The
framework has no global VM username or unverified image fallback.

Use `homelab-toolkit machines list`, `machines add`, `machines edit`, and
`machines remove` to manage definitions without editing YAML. The Web UI
**Machines** page exposes the same validated templates and fields. Definition
removal is intentionally limited to machines that have never been provisioned
or are not framework-managed.

An existing managed guest uses the separate retirement workflow:

```bash
homelab-toolkit machines retire worker-east
```

Retirement is available only when the machine is enabled, managed, is not the
control node, and hosts no service or project. It requires a recent verified
restore-drill checkpoint, displays the bound checkpoint and config revision,
requires an exact confirmation phrase, and removes the definition only after
OpenTofu state and Proxmox inventory independently prove the guest is gone.
The Web UI provides the same two-step plan and approval flow. Full-environment
destruction is never exposed through the browser.

Each machine may also hold generated or operator-approved per-runtime limits:

```yaml
machines:
  apps:
    # address, VMID, capacity, disks, and labels omitted
    resource_limits:
      grafana:
        memory_mb: 768
        cpus: 0.75
```

These overrides are validated against the target machine capacity and the
owning service manifest's memory/CPU floors. Generation applies them to
`generated/<machine>/compose.limits.yml`; generated overlays remain disposable
and must not be edited directly.

Mesh allocation pools and global edge exposure are typed independently. Pool
subnets must remain inside Headscale's supported Tailscale ranges; the private
LAN, gateway, and bridges come from the machine plugin that owns Headscale.

```yaml
network:
  expose_via_internet: true
  mesh_ipv4_cidr: 100.64.0.0/10
  mesh_ipv6_cidr: fd7a:115c:a1e0::/48
  container_ipv4_cidr: 172.31.0.0/17
  container_network_prefix: 28
  mail_public_access: true
  dns_public_access: true
```

The compiler deterministically allocates every plugin and relationship bridge
from `container_ipv4_cidr`. Choose an RFC1918 pool that does not overlap any
machine network. Smaller per-bridge prefixes provide more addresses per plugin;
the default `/28` provides 14 usable addresses and 2,048 isolated bridges.

Security agents (Wazuh, CrowdSec, Fail2ban) run on the **infra** host outside Compose where noted in playbooks.

## Homelab UI

Use the **[Homelab UI](homelab-ui.md)** (`homelab-ui` service, port **8080**) for interactive editing, wizard, and deploy actions — not port 8501.

## Compose layout

Each service owns a standalone application at
**`toolkit/services/<name>/compose.yaml`**. `homelab-toolkit generate` validates
and assembles the **root** `docker-compose.yml` with shared resources from
`stacks/platform.yaml`. Categories enable **Compose profiles** (for example
`management`, `media`, `cloud`, `dev`, `email`, `security`). On a guest LXC,
generation writes a minimal `generated/<role>/compose.yaml` and its matching
`generated/<role>/.env`.

The **cloud** category activates both `cloud` and `dev` Compose profiles on its
configured node (Nextcloud, Immich, Vaultwarden, FMD, Gitea, and their data
services). `dev` is an internal Compose profile, not a separate configuration
category.

```bash
cd /opt/homelab
docker compose -f generated/apps/compose.yaml --env-file generated/apps/.env ps
```

Multi-node deploys use **one role-scoped Compose file per host**. A guest does
not parse another role's services, dependencies, resources, or environment
references.

### Environment variables

| Variable | Purpose |
| --- | --- |
| `HOMELAB_ROOT` | Repo root (default `/opt/homelab`). Used by the CLI, UI, and `scripts/install.sh`. |
| `HOMELAB_NODE` | On a **guest** VM in a multi-node setup, selects the matching `generated/<role>/compose.yaml` and `.env`. |
| `HOMELAB_SERVICES` | Comma-separated category IDs used only when `deploy all` creates a missing configuration. Category IDs are discovered at runtime. |
| `HOMELAB_SETTING_<SERVICE>_<SETTING>` | Typed first-run service setting override for noninteractive setup. Hyphens become underscores, for example `HOMELAB_SETTING_MEDIA_LIBRARY_SERVER=plex`. Only manifest settings with `setup: true` are accepted. |
| `HOMELAB_SECRET_<NAME>` | Service credential supplied to preset install or self-deploy, for example `HOMELAB_SECRET_NORDVPN_TOKEN`. Inactive or undeclared service credentials are ignored. |
| `DEPLOY_NTFY_URL` | Optional ntfy topic URL for deploy completion notifications (also set via `notifications.deploy_ntfy_url` in config or secrets). |

Platform bootstrap credentials keep their canonical names:
`PROXMOX_API_TOKEN_ID`, `PROXMOX_API_TOKEN_SECRET`,
`CLOUDFLARE_API_TOKEN`, and `CLOUDFLARE_ZONE_ID`. Proxmox also accepts the
standard OpenTofu fallbacks `TF_VAR_proxmox_api_token_id` and
`TF_VAR_proxmox_api_token_secret`. Service plugins do not add bespoke core
environment variables; their manifest setup contracts provide the normalized
names automatically.

### Remote deploy from laptop

When `proxmox.provision_machines: true`, preflight and deploy tuning query **Proxmox host** CPU/RAM/load via SSH (not your laptop). Override in `config.yaml`:

```yaml
host_capacity:
  use_proxmox_host: true   # default
  proxmox_host: "192.0.2.10"  # example; defaults to dns.public_ip or generated.yml
  cpu_cores: 8             # optional manual override
  mem_total_mb: 32000
  load_threshold: 16
```

Staggered compose on each LXC uses **that guest's** local resources (`HOMELAB_NODE` is set during Ansible deploy).

`deploy all --dry-run` is intentionally read-only and offline: it requires an
existing `config.yaml`, does not create secrets or generated artifacts, and
does not contact Proxmox. Set both `host_capacity.cpu_cores` and
`host_capacity.mem_total_mb` when the plan should compare declared guest
resources against a host capacity estimate.

### Cloudflare DNS

```yaml
dns:
  provider: cloudflare
  public_ip: "192.0.2.10"  # example TEST-NET address; replace with your public IP
  proxy_enabled: true   # orange-cloud; requires Cloudflare SSL mode Full + Caddy TLS on origin
  verification_resolvers: ["1.1.1.1", "8.8.8.8"]  # all must agree; [] uses the system resolver

images:
  registry: ghcr.io/breixopd
  tag: auto                   # clean checkout: sha-<commit>; dirty checkout: local-<content hash>
  source: auto                 # auto | registry | local
  # Optional for a private registry. Store the token with `secrets set`.
  auth:
    username: breixopd
    token_secret: GHCR_READ_TOKEN
```

The automatic tag selects the exact `sha-<git commit>` artifact published by
CI for a clean checkout. Modified source receives a deterministic local tag,
which intentionally misses the registry and exercises the local-build fallback.
`auto` source mode pulls each service-owned image directly inside its target
guest; the Proxmox host does not store application images. Published custom
images are multi-platform OCI indexes, so Docker automatically selects the
guest's architecture. If a repository is unavailable, the controller detects
the guest platform, builds only that architecture, and transfers it over SSH.
Use `registry` to require published images or `local` for an offline deployment.
Public GHCR packages need no guest credentials. For a
private registry, the toolkit reads the named token from the encrypted secret
store, creates a temporary Docker credential directory on the guest, pulls the
image, and removes the credential immediately. Use a dedicated read-only
package token, never a broad account or repository token. GitHub Packages
currently requires a [classic PAT with `read:packages`](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry#authenticating-with-a-personal-access-token-classic)
for private guest pulls; public GHCR packages are anonymously pullable.

Run `homelab-toolkit dns sync` after deploy (or let the deploy workflow sync automatically).
The sync target is derived from the route/exposure model: only `public` services get Cloudflare records. Internal and mesh-only services are served by Caddy on infra and resolved through AdGuard/mesh DNS. Cloudflare cleanup is marker-based and only deletes stale records previously created by the toolkit.

## Generated artifacts

| Script / output | Purpose |
| --- | --- |
| `homelab-toolkit generate` | Writes `infrastructure/generated.auto.tfvars` and `automation/ansible/group_vars/generated.yml` from `config.yaml`. |
| `homelab-toolkit generate` | Writes per-LXC `.env`, `generated/Caddyfile`, etc. |
| `homelab-toolkit secrets generate` | Generates and persists secrets (SOPS) from `config.yaml` and defaults. |

Do not hand-edit generated files; change `config.yaml` and re-run `homelab-toolkit generate`.
Verified automatic rightsizing updates `machines.<id>.resource_limits` under the
configuration lock and regenerates the overlay before reconciling the target.

## Deployment entry points

**Recommended (laptop, no Python venv):**

```bash
docker compose -f docker-compose.setup.yml up -d --build
docker compose -f docker-compose.setup.yml exec controller homelab-toolkit bootstrap token
# or one-shot CLI:
docker compose -f docker-compose.setup.yml run --rm toolkit deploy all --skip-infra
```

**Developer path (local venv):**

```bash
uv sync --locked
homelab-toolkit deploy all
```

```bash
# Ansible-only site stack (inventory must list proxmox_hosts + guests)
ansible-playbook -i automation/ansible/inventory/hosts.yml automation/ansible/site.yml
```

`homelab-toolkit deploy all` runs **`homelab-toolkit generate`** before OpenTofu so `generated.auto.tfvars` matches `config.yaml`.

## Custom apps

- **One-off proxy route:** `homelab-toolkit` / install helpers — see [adding-a-new-service.md](../adding-a-new-service.md).
- **First-class service:** create `toolkit/services/<name>/` (`plugin.py` + `service.yaml` + `compose.yaml`) — see [adding-a-new-service.md](../adding-a-new-service.md) → `homelab-toolkit generate` → redeploy with `homelab-toolkit deploy all`.

## Multi-VM database connectivity

Service manifests declare their database provider, database, role, credential, and namespaced connection environment. The generator resolves each declared host and port from placement: co-located consumers use Docker DNS, while cross-machine consumers use the provider machine address. Redis dependencies use the same placement-aware internal routing and are never published unless a cross-machine consumer requires them.

### `PRIVATE_IP` binding

DB ports bind to `${PRIVATE_IP:-127.0.0.1}`. On infra, set `PRIVATE_IP` to the private LAN IP so other VMs can reach the services.

## Authelia OIDC

When `AUTHELIA_OIDC_HMAC_SECRET` is set, Authelia acts as OIDC IdP for supported apps. Forward-auth via Caddy covers the rest. See Authelia and Caddy generated configs under `generated/`.
