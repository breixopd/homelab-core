# Ansible Automation

Starter playbooks for a Proxmox-first homelab deployment (Docker Compose + LXC containers).

This directory is the execution layer for the LXC-based deployment.

**Docs:** [docs/wiki/automation.md](../../docs/wiki/automation.md) and [docs/wiki/operations.md](../../docs/wiki/operations.md).

## Layout

- `inventory/hosts.yml` - generated from machine plugins and `config.yaml`
- `group_vars/all.example` - defaults copied to the generated local `all.yml`
- `host-setup.yml` — main entrypoint for Proxmox host preparation
- `guest-setup.yml` — main entrypoint for LXC guest bootstrapping
- `playbooks/deploy-server-toolkit.yml`
- `playbooks/verify-services.yml`
- `site.yml`

## Usage

```bash
homelab-toolkit generate

# One command to deploy everything:
homelab-toolkit deploy all -y
```

## Configuration ownership

Do not edit inventory or generated group variables. Define machines under
`toolkit/machines/<machine-id>/machine.yaml`, select them in `config.yaml`, and
run `homelab-toolkit generate`. Domains and provider choices live in
`config.yaml`; credentials are managed through `homelab-toolkit secrets` or the
UI. Service routes and checks come from service manifests.

## What the playbooks do

- `host-setup.yml`
  Prepares the Proxmox host: no-subscription repo, packages, the machine-declared private bridge, kernel modules for Docker-in-LXC, sysctl tuning, ZFS mirror pool, and LXC template download.
- `guest-setup.yml`
  Orchestrates LXC bootstrap: base packages + Docker, Docker daemon config, storage, toolkit deployment, security agents, final hooks, and service verification.
- `deploy-server-toolkit.yml`
  Clones the repo, installs `homelab-toolkit` in a venv, runs `config init` / `secrets generate` / `generate`, then `docker compose up` with role-specific `COMPOSE_PROFILES`.
- `verify-services.yml`
  Confirms Docker and expected routes respond.
- `site.yml`
  Runs the full host-setup + guest-setup flow in order.

## Validation

Validate playbooks through the toolkit before a rollout:

```bash
homelab-toolkit deploy qa
```

## DNS credential provisioning

The toolkit automates DNS record creation (CNAME, A, TXT) for published subdomains. Two providers are supported:

### Cloudflare (primary)

| Variable | Where to set | How to get |
| --- | --- | --- |
| `cloudflare_zone_id` | Toolkit secrets | Dashboard → Domain → Overview → API section (right sidebar) |

The toolkit CLI (`homelab-toolkit dns sync`) and UI DNS page decrypt the Cloudflare token only for the bounded DNS operation. It is not projected into persistent Ansible group variables or guest environment files.

Service plugins own any credential required by an Ansible role. The shared toolkit runner decrypts those values immediately before execution, writes a mode-`0600` ephemeral extra-vars file, and removes it when the playbook exits. Direct `ansible-playbook` use is not a supported deployment path.

### Secret rotation

1. **Update the token** with `homelab-toolkit secrets set CLOUDFLARE_API_TOKEN` or the UI.
2. **Re-run deploy all**: `homelab-toolkit deploy all -y`.
3. **Or update via the Homelab UI**: Secrets page → edit `CLOUDFLARE_API_TOKEN` → Save.

For OVHcloud-specific DNS configuration (credentials, PTR records, vRack) see [docs/wiki/ovhcloud-setup.md](../docs/wiki/ovhcloud-setup.md).

## CI/CD use

The Proxmox deployment pipeline uses **GitHub Actions**. CI is not hosted on the
target fleet because deployment could restart or update the same server that is
executing the pipeline. Local checks run through the locked `make ci` entrypoint.

The practical pattern is:

1. runner has SSH access to the target environment
2. runner installs ansible-core
3. runner calls `homelab-toolkit deploy all`

`deploy all` is idempotent — it skips unchanged steps, so you can safely re-run it for targeted updates.
