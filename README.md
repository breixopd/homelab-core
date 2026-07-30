# Homelab Toolkit

Automated, config-driven deployment of self-hosted services to Proxmox machines, with a
FastAPI web UI and a CLI. One local, gitignored `config.yaml` drives OpenTofu (LXC/VM
lifecycle), Ansible (OS + Docker), and a staggered Docker Compose rollout, with single
sign-on, monitoring, alerting, and backups wired up automatically. The reusable framework
is public; operator credentials and deployment topology stay on the operator's system.

The [Homelab Platform project](https://github.com/users/breixopd/projects/1) tracks this
core repository alongside the independently releasable
[Media Cache](https://github.com/breixopd/media-cache) and
[Music Sync](https://github.com/breixopd/music-sync) products.

## Architecture

The default topology uses three role-separated Proxmox LXCs. The `machines` map
can add, remove, resize, disable, or replace them with VMs; service manifests
select nodes by labels rather than a fixed host count.

| LXC       | Services                                                                                                                                                                               |
| --------- | -------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| **infra** | Caddy (TLS + SSO proxy), Authelia, LLDAP, AdGuard, Postgres, Redis, Komodo, Kopia, ntfy, Prometheus, Loki, Alloy, Grafana, exporters, cAdvisor, Headscale, Wazuh (SIEM), Homelab UI |
| **media** | gluetun (VPN), qBittorrent, Prowlarr, Sonarr, Radarr, Bazarr, Seerr, Recyclarr, Flaresolverr, Jellyfin, Plex, Tautulli, Navidrome, music-sync, Tdarr                                   |
| **apps**  | Nextcloud, Immich, Vaultwarden, FMD, SeaweedFS, Docker-Mailserver, Gitea, dev Postgres/Redis                                                                                  |

Single sign-on: **Authelia** is the only login. Apps that support OIDC (Grafana,
Nextcloud, Immich, Vaultwarden, Headscale) use it directly; Gitea
uses Authelia reverse-proxy headers; everything else is gated by Caddy
`forward_auth`. Identity lives in LLDAP.

Repository layout and where each kind of file lives: **[docs/wiki/repo-layout.md](docs/wiki/repo-layout.md)**.

## Quick start

### Web UI (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/breixopd/homelab-core/main/scripts/install.sh | bash
docker compose -f /opt/homelab/docker-compose.bootstrap.yml exec controller homelab-toolkit bootstrap token
```

Open `http://localhost:8080/setup` and paste the one-time capability. The setup
listener is bound to loopback only; the session cookie remains `Secure`,
`HttpOnly`, and `SameSite=Strict`.

The installer pulls the multi-platform toolkit image, resolves its registry tag
to an immutable digest, and seeds the matching framework snapshot into
`/opt/homelab`; it does not build application images on the Proxmox host. Set
`HOMELAB_ROOT` for another install directory or `TOOLKIT_IMAGE` for a fork or an
explicit release digest. Private registries must already be authenticated with
Docker before running the installer. Rerunning the installer transactionally
updates image-managed framework files, preserves local configuration and data,
and restores the previous snapshot if the update cannot be completed. Git
checkouts remain source-managed and are never overwritten.

Deploy and recover stream live progress to the terminal and to `.homelab-state/deploy-*.log` (path printed at start). The Deploy page shows step, VM, ansible task, compose wave, and a progress bar.

Sign in with your homelab SSO password (the same LLDAP credentials used for
SSH and Authelia), or transparently via Authelia SSO when running behind the
reverse proxy.

### CLI

```bash
homelab-toolkit --help                  # all commands
# `deploy all` is THE single deploy command — it auto-detects missing config
# and secrets, then provisions LXCs, runs Ansible, and rolls out Docker Compose.
homelab-toolkit deploy all -y           # one command: generate → provision → deploy → hooks → verify
homelab-toolkit deploy all --dry-run    # read-only offline plan; requires config.yaml
homelab-toolkit deploy manual-steps         # post-deploy steps that still need a human
homelab-toolkit deploy verify --qa      # post-deploy QA checks
homelab-toolkit dns sync                # sync Cloudflare DNS
homelab-toolkit fleet add <name> <ip>   # register a fleet node
homelab-toolkit fleet onboard <name>    # install agents + join Headscale mesh (tagged)
homelab-toolkit projects list           # list all registered projects
homelab-toolkit projects add --subdomain <name> --image <digest> --placement apps  # register a portable project
homelab-toolkit projects remove <name>  # remove a project
homelab-toolkit projects generate       # regenerate Caddy routes for projects
homelab-toolkit projects deploy <name>  # deploy a Docker project to its target LXC
homelab-toolkit projects stop|start|restart <name>  # manage project container lifecycle
homelab-toolkit projects logs <name>    # view project container logs
homelab-toolkit projects status <name>  # show project container status
homelab-toolkit maintenance run         # run full maintenance (Docker prune, journal vacuum)
homelab-toolkit maintenance metrics     # print Prometheus maintenance/disk metrics
homelab-toolkit watchdog check          # health-check all containers
homelab-toolkit watchdog heal           # auto-fix detected issues
homelab-toolkit watchdog notify         # send health report via ntfy
homelab-toolkit watchdog daemon         # run watchdog as a continuous monitoring service
homelab-toolkit update check            # check for available updates (Docker images + system)
homelab-toolkit update apply <service>  # snapshot, pin digest, deploy, verify, and auto-rollback
homelab-toolkit update diff <service>   # show the reviewed target and release notes
homelab-toolkit update rollback         # restore and verify the previous release
homelab-toolkit images lock --write      # resolve new plugin image tags to immutable OCI digests
```

## Fresh Proxmox install

1. **Launch the UI** (`homelab-toolkit ui`, or the setup compose file).
2. **Enter the few required inputs**: domain, admin email, Proxmox API token,
   Cloudflare token + zone (for public DNS/TLS), and any VPN/Spotify creds you want.
   Everything else is auto-detected or auto-generated.
3. **Deploy** from the Deploy page (or `homelab-toolkit deploy all -y` — auto-detects missing config/secrets and creates from env vars). The toolkit:
   - generates configs + secrets,
   - downloads checksum-pinned images and provisions declared machines with OpenTofu,
   - configures OS, networking, and Docker via Ansible,
   - runs a staggered, health-gated Compose rollout per LXC,
   - executes post-start hooks that wire services together (API keys, OIDC, DB users),
   - syncs Cloudflare DNS to the Proxmox public IP.
4. **Sign in** at `https://auth.<domain>`; your LLDAP user and group membership are
   created automatically from `config.yaml`.

> The Proxmox API token must have privilege separation disabled so it can create
> LXCs and storage. See [docs/wiki/infrastructure-as-code.md](docs/wiki/infrastructure-as-code.md).

## Web UI pages

| Page              | Purpose                                                                                                             |
| ----------------- | ------------------------------------------------------------------------------------------------------------------- |
| **Dashboard**     | State, resource gauges, Prometheus targets, alerts, unhealthy containers, category summary                          |
| **Services**      | Service list by category with live Docker status and start/stop/restart                                             |
| **Deploy**        | Generate / deploy / recover / hooks / QA with live step status, ansible task/wave progress (SSE), and streamed logs |
| **Projects**      | Custom subdomain project management with Docker container support                                                   |
| **Hosts / Fleet** | External hosts and fleet nodes; add-node onboarding                                                                 |
| **DNS**           | Preview and sync Cloudflare records                                                                                 |
| **Secrets**       | View, edit, generate, and rotate secrets (SOPS)                                                                     |
| **Settings**      | Edit global, machine, SSH, Proxmox, and network desired state, then generate/validate                              |
| **Portal**        | Root domain landing page with bookmarks to all services                                                             |
| **Webhooks**      | Grafana alert webhook endpoint for auto-heal                                                                        |
| **Setup wizard**  | Guided first-run configuration wizard                                                                               |

Metrics and logs are served by the built-in **Grafana** (dashboards, alerts).

## Documentation

- [Wiki index](docs/wiki/README.md) — start here
- [Operations & deployment](docs/wiki/operations.md)
- [Configuration](docs/wiki/configuration.md)
- [Automation (Ansible)](docs/wiki/automation.md)
- [Repository layout](docs/wiki/repo-layout.md)
- [Adding a service](docs/adding-a-new-service.md)
- [Projects (subdomain management)](docs/wiki/projects.md)

## Project structure

See [docs/wiki/repo-layout.md](docs/wiki/repo-layout.md) for the full tree. Summary:

```
config.yaml              # Local desired state (generated, gitignored)
toolkit/                 # Python toolkit package (installable)
  core/                  # config/, deploy/, generate/, ops/, ansible/, infra/, …
  services/              # Flat plugins: <name>/{service.yaml, compose.yaml, image/, optional plugin.py}
  services/sdk/          # Shared plugin primitives (http, docker, authelia, …)
  cli/                   # CLI commands
  webui/                 # FastAPI + htmx web UI
  categories/            # Strict category plugins: <name>/{category.yaml, optional plugin.py}
  registry/              # Stagger overlays and other declarative deploy data
  templates/             # Jinja2 templates for generated configs
stacks/                  # Fixed platform networks and named volumes
automation/              # Ansible playbooks + roles (see automation/README.md)
infrastructure/          # OpenTofu IaC for declared Proxmox LXC/VM machines
config/                  # Static service config (mounted into containers)
generated/               # Generated .env and configs (gitignored)
docs/wiki/               # Canonical documentation
```
