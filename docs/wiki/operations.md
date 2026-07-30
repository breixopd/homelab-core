# Operations & deployment

Single guide for **what runs automatically**, **what you configure once**, and **how to deploy**.

## Automation matrix

| Area | Automated | How | You provide |
| --- | --- | --- | --- |
| Config → OpenTofu / Ansible vars | Yes | `homelab-toolkit generate` | `config.yaml` (+ optional `proxmox:`) |
| Proxmox host prep | Yes | `host-setup.yml` | SSH to hypervisor |
| LXC/VM provisioning | Yes | OpenTofu BPG provider | API token in encrypted secrets |
| Guest Docker + toolkit | Yes | `guest-setup.yml` → controller rsync/tar fallback | Controller SSH access |
| Host services + timers | Yes | Reconciled during initial bootstrap and every guest redeploy | Schedules in service settings |
| Compose profiles per LXC | Yes | `homelab-toolkit generate` | Categories in `config.yaml` |
| Guest Caddy routes | Yes | `generated-routes.yml` + route playbook | Match toolkit subdomains |
| Full loop | Yes | `homelab-toolkit deploy all` | `config.yaml` + tfvars / `TF_VAR_*` |
| DNS records | Partial | `homelab-toolkit dns sync` | Cloudflare secrets |
| New compose service | Yes | Manifest discovery + generation | Add `toolkit/services/<name>/` plugin files |

Details for Ansible playbooks: [automation.md](automation.md). IaC: [infrastructure-as-code.md](infrastructure-as-code.md).

## Essential services

Services marked `essential: true` in `service.yaml` are protected infrastructure. The current set:

`authelia`, `postgres`, `redis`, `lldap`, `caddy`, `adguard`, `prometheus`, `loki`, `vaultwarden`, `registry-mirror`, `wazuh-indexer`, `wazuh-dashboard`, `crowdsec`

Contract (enforced by deploy and ops code):

- **Non-removable** — deploy will not drop them when trimming profiles or reconciling drift.
- **Early waves** — staggered compose always starts them in the first waves on infra (see `toolkit/registry/stagger_overlays.yaml`).
- **Careful restart policy** — watchdog and heal paths treat them as dependency roots; avoid casual restarts.
- **SDK consumers** — other plugins reach them via `toolkit.services.sdk` submodules (`authelia`, `postgres`, `redis`, `ldap`, `caddy`, `adguard`, `monitoring`, `vaultwarden`, `registry`, `wazuh`, `crowdsec`).

Do not delete or heavily edit these plugins without tracing the dependency chain. See [adding-a-new-service.md](../adding-a-new-service.md).

## Prerequisites

**Proxmox host:** VE 8.x, SSH key for root, a public bridge declared by each
machine plugin, and an API token (`pveum user token add root@pam terraform`).

**Control machine:** Docker Engine with Compose v2. The release image includes
the locked toolkit, OpenTofu, Ansible collections, and framework snapshot.

```bash
curl -fsSL https://raw.githubusercontent.com/breixopd/homelab-core/main/scripts/install.sh | bash
docker compose -f /opt/homelab/docker-compose.bootstrap.yml exec controller homelab-toolkit bootstrap token
```

The installer resolves `ghcr.io/breixopd/homelab-toolkit:latest` to an immutable
digest before writing the bootstrap Compose model. An existing complete source
checkout can be used by setting `HOMELAB_ROOT`; an unknown nonempty directory is
never overwritten. Source contributors can still clone the repository and run
`uv sync --locked --all-extras` for development.

Rerun the same installer to update a managed snapshot. It stops the local
control plane, stages the new image-owned file manifest, applies it under an
exclusive lock, and rolls every managed file back before restarting the old
digest if reconciliation fails. `config.yaml`, encrypted secrets, generated
artifacts, runtime data, and every other untracked local path are preserved.

**OVH bare metal:** extra steps in [ovhcloud-setup.md](ovhcloud-setup.md).

**Credentials to gather:** Proxmox API token, domain, optional Cloudflare token + zone ID, VPN creds for media, optional Spotify. Service passwords are auto-generated via `homelab-toolkit secrets generate`.

**Custom images:** The configured `images.source` policy determines delivery:

| Method | When to use |
| --- | --- |
| **`auto` (default)** | Pull on each target; retry transient failures, then build and transfer only unavailable images |
| **`registry`** | Require published images and fail closed when any pull fails |
| **`local`** | Build required images on the controller and load them over SSH for offline installs |

Run `homelab-toolkit images sync --source <policy>` for an explicit one-off
reconciliation. Registry, tag, and default source live under `images` in
`config.yaml`; generated `.env` files receive the service-owned image refs.
Limit a repair to one or more plugin images with repeatable selectors, for
example `homelab-toolkit images sync --node infra --image caddy --source local`.
Use the same selectors with `images verify` to check only the repaired images.
With the default `tag: auto`, clean checkouts use the matching CI-published
`sha-<git commit>` images; modified trees use a deterministic local tag and
therefore build only the changed checkout when no matching artifact exists.
Published custom images are OCI indexes for every platform declared by their
service plugin (both `linux/amd64` and `linux/arm64` by default). Docker selects
the matching image automatically. Pulls run inside the target guest, so the
Proxmox hypervisor never stores or builds application images. Local fallback
detects each guest's Docker platform and builds only the architecture it needs.
Verify with `homelab-toolkit images verify` or `deploy verify --qa`.
For private GHCR packages, configure `images.auth.username` and
`images.auth.token_secret`, then set the named secret with
`homelab-toolkit secrets set`. Pull credentials are guest-ephemeral and are not
written to persistent Docker configuration.

**External runtime images:** Every plugin-owned registry image includes both a
reviewed version tag and OCI index digest. After adding or deliberately changing
a version, run `homelab-toolkit images lock --write`; use `--plugin <name>` to
scope it or `--refresh` to audit existing locks. The command reports each
registry result, retries transient failures, and caches successful lookups for
one hour so an interrupted audit resumes without repeating completed requests.
The service catalog blocks generation while any non-built runtime is mutable.

## Deploy paths

### A — Fully automated (recommended)

Run `homelab-toolkit deploy all` from your **control machine** (not on Proxmox). This single command syncs config, generates secrets if missing, provisions LXCs via OpenTofu, runs Ansible guest setup (Docker, toolkit clone, generate, staggered compose, routes, agents), and executes post-deploy hooks and verification.

```bash
homelab-toolkit deploy all -y
```

Then open Homelab UI at `https://homelab.<domain>` (or the generated private
address of the machine selected by its service manifest) and run **Verify** if needed.

### B — Web UI

```bash
uv sync --locked --all-extras
homelab-toolkit ui    # http://localhost:8080
```

Use **Settings** → **Secrets** → **Deploy** (live progress + `.homelab-state/deploy-*.log`). Same workflow as CLI `homelab-toolkit deploy all`.

See [homelab-ui.md](homelab-ui.md).

### C — Manual steps

| Task | Command |
| --- | --- |
| Regenerate configs | `homelab-toolkit generate` |
| Hooks only | `homelab-toolkit deploy hooks` |
| Verify | `homelab-toolkit deploy verify` |
| Cancel stuck deploy | `homelab-toolkit deploy lock --clear` |
| Destroy a host | `homelab-toolkit deploy destroy-host <name>` |
| Retire an empty managed guest | `homelab-toolkit machines retire <machine-id>` |

To re-deploy a single guest after a config change, run `homelab-toolkit deploy all` — it is idempotent and skips unchanged steps.

Machine retirement is narrower than host destruction. The controller rejects
control machines, external or disabled machines, and machines with service or
project placement. It binds the plan to a verified recovery checkpoint and the
current desired-state revision, then streams the destructive job through the
normal structured progress surface. The operation fails closed if OpenTofu or
the Proxmox LXC/QEMU inventory cannot prove absence. See
[ADR-009](../decisions/009-checkpoint-bound-machine-retirement.md).

## DNS secrets

Cloudflare: token with `Zone:DNS:Edit` + `Zone:Zone:Read`, stored as `CLOUDFLARE_API_TOKEN` and `CLOUDFLARE_ZONE_ID` in `secrets.enc.yaml` (SOPS/age). Rotate via CLI `homelab-toolkit secrets rotate` or UI **Secrets** page.

## Post-deploy

1. `homelab-toolkit deploy verify` — containers + HTTPS probes (cached in `.homelab-state/last-verify.json`).
2. `homelab-toolkit deploy manual-steps` — one-time app setup hints.
3. [Troubleshooting](troubleshooting.md) if a service stays unhealthy.

## Self-healing and operations dashboard

The homelab runs continuous monitoring and exposes operator-facing pages through the WebUI:

| Surface | CLI | WebUI | Notes |
| --- | --- | --- | --- |
| **Watchdog timer** | `watchdog heal --notify` | `/operations/watchdog` | Installed on every managed node; runs node-local health and verified remedies every five minutes and alerts for unresolved issues |
| **Maintenance timer** | `maintenance run --node <id>` | `/operations/maintenance` | Reconciled on every managed node from the manifest-owned enable flag and configured daily schedule |
| **Audit log** | `watchdog history --limit 20` | `/operations/audit` | Append-only JSONL at `.homelab-state/audit.log`. Captures deploy/verify/heal/reconcile/secret_rotate |
| **Drift detection** | `deploy reconcile [--apply] [--dry-run]` | `/operations/maintenance` | Compares config snapshot to `last-reconcile.json`. Apply runs `generate` + redeploy drifted VMs |
| **External uptime** | `deploy verify --external` | `/operations/maintenance` | Probes public HTTPS endpoints via DNS → Cloudflare → Caddy (real user path) |
| **DB safety snapshots** | `maintenance dump` / `maintenance list-dumps` / `maintenance restore-db <path>` | `/operations/maintenance` | `pg_dumpall` gzipped on infra LXC, retains latest 7, auto-rotates |
| **Verified image updates** | `update check` / `update apply <service>` / `update rollback` | `/operations#updates-heading` | Same-major and same-flavor candidates only; stateful services snapshot first; deploys digest pins and automatically rolls back failed verification |
| **Container rightsizing** | `watchdog rightsize --dry-run` / `watchdog rightsize --apply` | Service settings | Runs on the control node at the configured interval. Uses node-scoped cAdvisor p95 demand and enforced Docker limits; safe stateless reductions deploy in bounded steps while growth and stateful changes enter the durable approval queue |
| **Rightsizing approvals** | `approvals list` / `approvals approve <id>` / `approvals reject <id>` | — | Approval immediately applies, verifies, and rolls back on failure. `approvals execute <id>` resumes an approved request after interruption |
| **Vaultwarden sync** | `maintenance sync-vault` | — | Pushes 17 service credentials to admin user's vault (idempotent) |
| **Secret rotation E2E** | `secrets rotate --name X --apply` | — | Rotate → dump → generate → deploy all VMs → run hooks → verify; prints rollback path on failure |
| **Post-deploy soak** | (automatic in `deploy all`) | — | Waits 60s after verify, re-checks container health per VM |

All events write to the unified audit log. Guest deployment reconciles the unit
files, enablement, and schedules, so configuration changes converge without
manual `systemctl` work. Each guest keeps the automation public key; only the
configured control node receives the private identity needed for cross-node
orchestration. Node-local watchdog and maintenance commands never route back
through the Proxmox host.

Automatic node recovery is a last resort after plugin heals and bounded
container restarts. Decisions use only the current health report, are evaluated
independently for each configured node, and never run a destructive recovery.
Configure the kill switch, per-node cooldown, critical-service threshold, and
exhausted-remedy threshold from **Services -> Homelab UI**. Failed recovery
attempts also start the cooldown so a broken control path cannot create a retry
storm.

Resource tuning policy is also declared by **Services -> Homelab UI**: enable or
disable automatic tuning, choose its 6-168 hour control-node schedule, headroom,
maximum reduction step, telemetry window and density, and per-service cooldown.
Cooldown and approvals live in crash-safe `.homelab-state` files. A corrupt
state file blocks automatic changes and is surfaced to the operator instead of
silently resetting safety history.
