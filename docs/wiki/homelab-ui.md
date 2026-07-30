# Homelab UI

The Homelab UI is a **FastAPI + htmx + SSE** web console for configuration and controller-managed deployment. It runs as the low-authority `homelab-ui` client beside the private `homelab-controller` on the **infra** VM.

## Running the UI

### Development / local

```bash
cd homelab && uv sync --locked --all-extras
export HOMELAB_ROOT=/path/to/homelab   # repo root with config.yaml
homelab-toolkit ui                     # Default port 8080
homelab-toolkit ui --port 9090         # Custom port
python3 -m toolkit.webui               # Same entry as the container
```

### Docker Compose (production)

```bash
docker compose --profile svc-homelab-ui up -d homelab-controller homelab-ui
```

- Through Caddy (when management stack is up): `https://homelab.<your-domain>`
- The UI has no published host port in production; Caddy is the only ingress.

### First run

1. Start the setup topology and issue a one-time capability with `homelab-toolkit bootstrap token` in the controller container.
2. Complete `/setup`; the controller writes the configuration, encrypted secrets, and SSH identity transactionally.
3. Use **Deploy** to run the controller-owned workflow and follow its durable event stream.

## Pages

| Page              | Purpose                                                                |
| ----------------- | ---------------------------------------------------------------------- |
| **Dashboard**     | Deployment status, Prometheus gauges, alerts, unhealthy containers     |
| **Services**      | Service catalog, bookmarks, live Docker actions                        |
| **Deploy**        | Preflight, full deploy workflow, live step/task/wave status + SSE logs |
| **DNS**           | Cloudflare sync and cleanup                                            |
| **Secrets**       | Generate and review secret specs                                       |
| **Settings**      | Edit controller-managed desired state                                 |
| **Projects**      | Manage custom application routes through the controller               |

## Bookmarks

The **Services** page includes curated bookmarks (Authelia, Grafana, Wazuh, mesh-only media tools, host CLI links) generated from `config.yaml` via `toolkit.core.ops.portal_bookmarks`.

## Authentication

The UI authenticates against LLDAP (the same directory as Authelia SSO and SSH) — no separate UI password. Use your homelab SSO password. In production, Caddy + Authelia forward-auth provides automatic SSO; the UI reads `Remote-User` when present and skips the login form entirely. First-boot localhost access is allowed before LLDAP is bootstrapped.

## Security notes

- The UI has no repository, Docker socket, SSH material, SOPS identity, or local-operator credential. It receives only read-only `config.yaml`, a read-only controller runtime mount, and its own role-scoped token.
- Header-asserted remote/mTLS controller access is not supported. The controller accepts authenticated Unix-socket roles only.
- Caddy and Authelia are the only supported public ingress path.
- Session cookies are signed by an owner-only secret persisted in `homelab-ui-state`.

## Watchdog & maintenance

Maintenance, healing, updates, identity administration, and fleet lifecycle are not executed in the UI process. Use controller-backed CLI operations while their typed browser resources are being introduced:

```bash
homelab-toolkit watchdog check           # Run a full health scan
homelab-toolkit watchdog heal             # Run and verify eligible remedies
homelab-toolkit watchdog notify           # Send health report via ntfy
homelab-toolkit watchdog rightsize --dry-run # Review resource proposals
homelab-toolkit watchdog rightsize --apply   # Apply safe changes and queue guarded ones
homelab-toolkit approvals list               # Review guarded resource changes
homelab-toolkit maintenance run           # Docker prune, journal vacuum, log trim
homelab-toolkit maintenance metrics       # Print Prometheus maintenance/disk metrics
```

### Watchdog Daemon Mode

Run the watchdog as a continuous background monitoring service:

```bash
homelab-toolkit watchdog daemon --interval 60
```

The daemon continuously checks container health at the specified interval (in
seconds, default 60). At each check cycle it:

- Runs a full health scan of all containers and system resources
- Reports any issues with severity labels
- Auto-heals safe-to-restart containers and reports succeeded, failed, and deferred remedies
- Sends ntfy notifications for detected issues
- Discovers containers via Docker labels (`homelab.watchdog.restart-policy`,
  `homelab.watchdog.depends-on`) and merges them into the restart safety and
  dependency maps

Press `Ctrl+C` to stop.

```bash
homelab-toolkit watchdog daemon --interval 120 --dry-run
```

Use `--dry-run` to observe what the daemon would do without making changes.

Every deployed runtime node runs `homelab-maintenance.timer` daily at
`maintenance.daily_at` (default `03:00`). The timer reconciles Docker
resources, journals and toolkit logs, checks certificate expiry, and scans for
image updates. Container log growth is bounded by Compose-managed Docker
rotation (`10m` x 3 files); PostgreSQL's native autovacuum owns routine database
vacuuming. Setting
`maintenance.enabled: false` stops and disables the timer without blocking an
explicit maintenance run from the CLI or Operations page.

Every managed node also runs a five-minute, node-local watchdog timer. The
configured control node alone owns `homelab-rightsize.timer`, which evaluates
and applies verified resource changes at the interval configured under
**Services -> Homelab UI**. Saving timer enablement or schedule settings queues
guest reconciliation automatically.

## Mobile access

Use official apps with your public URLs (`https://photos.<domain>`, etc.). Prefer **Authelia SSO** where the app supports OIDC (Immich, Nextcloud, Vaultwarden). Media server apps (Jellyfin, Plex) use their own login. FMD uses its native encrypted phone protocol at `https://fmd.<domain>`; its generated registration token is stored in the managed credential catalog, while browser access is additionally protected by Authelia. For \*arr admin on Android, **nzb360** with API keys. Always join **Headscale** or use AdGuard DNS on infra so mesh-only hostnames resolve.
