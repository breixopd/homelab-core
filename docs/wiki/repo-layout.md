# Repository layout

Canonical map of the homelab repo. **Do not** add parallel doc trees or duplicate sync paths — update this page when structure changes.

## Top level

| Path                       | Purpose                        | Notes                                                                 |
| -------------------------- | ------------------------------ | --------------------------------------------------------------------- |
| `config.yaml`              | Single source of truth         | Drives generate, fleet, IaC, Ansible, and Compose                     |
| `docker-compose.yml`       | Canonical topology model       | Generated from service applications and fixed platform resources      |
| `docker-compose.setup.yml` | Web UI bootstrap               | Local setup without full deploy                                       |
| `stacks/`                  | Fixed Compose resources        | Shared networks and named volumes only                                |
| `renovate.json`            | Dependency updates             | Docker images, GitHub Actions                                         |
| `pyproject.toml`           | Python build + test config     | ruff, mypy, and `[tool.pytest]` settings live here                    |
| `toolkit/`                 | **Installable Python package** | CLI + core framework + flat service plugins + WebUI                   |
| `automation/ansible/`      | Provisioning & deploy          | Playbooks, roles, inventory templates                                 |
| `infrastructure/`          | OpenTofu (Proxmox LXCs)        | State/tfvars gitignored                                               |
| `config/`                  | **Static** service config      | Mounted into containers; not generated                                |
| `generated/`               | **Generated** artifacts        | Role Compose models, `.env`, configs, and limits; gitignored           |
| `scripts/`                 | Shell helpers                  | `install.sh`, `local-ci.sh`, …                                        |
| `tests/`                   | Pytest suites                  | `tests/framework/` is the core contract gate; `tests/services/` is service-owned |
| `docs/wiki/`               | **Canonical documentation**    | Operations, architecture, checklists                                  |
| `automation/`              | Ansible tree                   | See [automation/README.md](../../automation/README.md)                |
| `docs/` (other)            | Contributor guides             | `adding-a-new-service.md`                                             |
| `dev/`                     | Dev-only assets                | obsolete registry dirs gitignored                                     |
| `data/`                    | Runtime bind-mount data        | gitignored                                                            |
| `ssh/`                     | Deploy SSH keys                | gitignored                                                            |
| `.homelab-state/`          | Operational runtime state    | Deploy logs, watchdog history, recovery memory, verify cache; gitignored |
| `.github/workflows/`       | CI                             | ruff, mypy, pytest                                                    |

### Runtime-only paths

| Path         | Action                        |
| ------------ | ----------------------------- |
| `recyclarr/` | Hook-generated and gitignored |

## `toolkit/` package

```
toolkit/
  cli/           Click commands (deploy, fleet, lxc, ops, projects, maintenance, …)
  core/          Framework modules (config, deploy, generate, ops, compose, …)
  services/      Flat plugin tree — one directory per service (see below)
  services/sdk/  Shared plugin primitives (http, docker, authelia, postgres, …)
  categories/    Category grouping, validation, and Compose profile selection
  registry/      Declarative deploy overlays (e.g. stagger wave ordering)
  templates/     Framework-only templates such as invitation email layouts
  webui/         FastAPI + htmx UI
```

**Import rule:** use explicit paths, e.g. `from toolkit.core.config.config import Config`. Scripts call `homelab-toolkit` or `python -m toolkit.cli`.

**Development install:** `uv sync --locked --all-extras` from the repository root.

Independently useful products remain separate repositories and integrate through
release-pinned service adapters; see
[ADR-013](../decisions/013-core-and-independent-service-repositories.md).

### `toolkit/services/` — flat plugins

Every service is a self-contained directory:

```
toolkit/services/<name>/
  plugin.py      ServicePlugin subclass (post_start, verify, heal, …)
  service.yaml   Declarative metadata (routes, secrets, memory_tier, essential, …)
  compose.yaml   Docker Compose service block
  templates/     (optional) Jinja2 templates owned by this service
  bootstrap.py   (optional) longer post-start setup
```

The loader in `toolkit/services/__init__.py` scans this tree automatically. Adding a service = drop the directory + enable its category in `config.yaml` — no central hook table or dispatcher to edit.

Plugins import shared helpers **only** from `toolkit.services.sdk` (a package with submodules `http`, `docker`, `authelia`, `postgres`, `redis`, `monitoring`, `vaultwarden`, `adguard`, `caddy`, `wazuh`, `crowdsec`, `ldap`, `registry`, plus internal `_vmexec` re-exports). See [adding-a-new-service.md](../adding-a-new-service.md).

### `toolkit/services/sdk/`

Leaf and cfg-aware primitives extracted from duplicated plugin logic. Single import surface:

```python
from toolkit.services.sdk import http_check, docker_exec_on_vm, authelia_oidc_issuer
```

### `toolkit/categories/` - strict category plugins

Categories own cross-service grouping and optional pure configuration behavior:

| Path | Role |
| --- | --- |
| `<cat>/category.yaml` | Strict ordering, placement, dependency, profile, and host-service contract |
| `<cat>/plugin.py` | Optional category-owned validation and Compose-profile selectors |
| `schema.py` | Pydantic contract that rejects unknown keys and unsafe identifiers |
| `yaml_loader.py` | Discovers category folders, validates manifests, and imports only their local plugin callbacks |

Service membership, labels, dependencies, enablement, variables, host bind
sources, restart policy, memory tier, secrets, routes, and management
capabilities are projected from strict service manifests. Do not add
per-service data or Python under `categories/`; lifecycle behavior belongs to
the owning service plugin.

Category **names** (`media`, `cloud`, `management`, …) are still toggled in `config.yaml` → `services:` and map to Compose profiles.

### `toolkit/core/` subpackages

| Package           | Role (examples)                                                    |
| ----------------- | -------------------------------------------------------------------- |
| `config/`         | `config`, `storage`, `validators`, `credential_catalog`, `service_metadata` |
| `secrets/`        | `secrets`, `bitwarden_crypto`, `bootstrap_passwords`               |
| `deploy/`         | `deploy_workflow`, `staggered_compose`, `hook_runner`, …           |
| `ansible/`        | `ansible_inventory`, `ansible_ssh`, `ansible_routes`                 |
| `infra/`          | `iac_sync`, `proxmox`, `hosts`, `fleet`, `host_capacity`           |
| `compose/`        | `docker`, `registry`, `registry_mirror`, `port_conflict`            |
| `generate/`       | `generate`, `compose_assemble`, `validate`, `resources`              |
| `bootstrap/`      | Shared bootstrap helpers (postgres, grafana, wazuh, …)               |
| `ops/`            | `verify`, `dns`, `watchdog`, `automation`, `preflight`, `hook_verify` |
| `identity/`       | LLDAP client, invite tokens, user provision                         |
| `verify/`        | Post-deploy container, endpoint, SSO, and service-hook checks         |

## `stacks/` and Compose

Every runtime service owns a standalone application at
`toolkit/services/<name>/compose.yaml`. The platform directory contains only
fixed shared resources:

- `stacks/platform.yaml` — shared networks and named volumes

`homelab-toolkit generate` strictly validates every application, rejects duplicate
ownership, and writes the canonical root `docker-compose.yml`. For multi-node
deployments it also writes a minimal runtime model at
`generated/<role>/compose.yaml` for every enabled node. Runtime operations use
only that node model; the root model is a controller topology artifact and is
not an editing surface.

Every Compose service in an application inherits the manifest `placement`.
Distributed companions and special runtimes declare `runtimes` metadata in
`service.yaml`, including an optional `compose_profile` when the runtime needs
independent activation. Generation rejects declarations that name services or
profiles not owned by that folder. Cross-node `depends_on` entries and unused
platform resources are removed from role models.

Deploy wave ordering is declared in `toolkit/registry/stagger_overlays.yaml` (also mirrored under `stacks/`).

## `automation/ansible/`

```
automation/ansible/
  playbooks/       deploy-server-toolkit, bootstrap, fleet onboard, …
  roles/           komodo_periphery, vpn_client, …
  tasks/           Shared task snippets synced from controller
  templates/       Ansible Jinja (AdGuard, etc.)
  inventory/       hosts.yml (generated from machine plugins, gitignored)
  group_vars/      all.example → all.yml (generated, gitignored)
  lib/             Shell helpers (generated-vars.sh)
```

Generated vars: `group_vars/generated.yml`, `generated-routes.yml` — written by toolkit, gitignored.

Service-specific host automation is owned by each plugin under
`toolkit/services/<name>/ansible/`. The generator projects its declared phase
files into `group_vars/generated.yml`; core playbooks dispatch those lists and
retain only platform policy such as Docker firewall and guest bootstrap.

## `config/` vs `generated/`

|           | `config/`                      | `generated/`               |
| --------- | ------------------------------ | -------------------------- |
| Edited by | Humans                         | `homelab-toolkit generate` |
| In git    | Yes (examples + static)        | No (gitignored)            |
| Examples  | `config/proxmox/*.example.env` | N/A                        |

## `infrastructure/`

OpenTofu for Proxmox LXCs. `generated.auto.tfvars` and state files are local-only.

## Service-owned images

Locally built image source lives beside its owner under
`toolkit/services/<service>/image/`. The service's `image_build` contract
declares its build, verification, audit, and publication policy.

Independently released services instead declare `image_release` with their
Compose service, registry repository, version, and OCI index digest. Their
Compose application pulls that immutable multi-architecture release directly.
Update discovery scans its version tag, then resolves and records a new digest
only after operator approval. Every other registry-backed runtime is likewise
tagged and digest-pinned directly in its plugin-owned Compose application.
`homelab-toolkit images lock --write` resolves missing locks without a central
image list; catalog loading rejects mutable references before generation.

## Service-owned host paths

Host bind roots are declared under `host_sources` in the owning service
manifest and compiled below the configured install root. Compose fallbacks must
match the manifest's canonical relative path exactly. Shared sources have one
owner and may be consumed only by services on the same placement; catalog
loading rejects missing, duplicate, unused, unsafe, or cross-placement
contracts. There is no central host-path map in the generator.

## Service-owned generated artifacts

Runtime configuration files and their templates live beside the consuming
service. The manifest declares every `generated_artifacts` path and its type,
sensitivity, and executable policy; `plugin.py` produces them through a bounded
atomic writer. Catalog validation ties generated host sources and direct
Compose mounts to those owners. Initial generation and node-side repair use the
same plugin hook, so core contains no service-name generator branches.

## `scripts/`

| Script                    | Role                                                           |
| ------------------------- | -------------------------------------------------------------- |
| `local-ci.sh`             | Locked local lint, Ansible, type, test, security, and IaC gates |
| `pytest-safe.sh`          | Pytest wrapper safe for controller shells                      |
| `coverage-chunk.sh`       | Coverage chunk reporting for incremental test runs             |
| `verify-phase2.sh`        | Verify workflow (compose, hooks, ops)                            |
| `recover-drill.sh`        | Recovery drill runner for ops simulation                       |
| `install.sh`              | Preset install and smoke test                                  |
| `bump-versions.py`        | Discover conservative image update candidates                    |
| `check-framework-updates.py` | Scan source-controlled framework dependencies for updates   |

## `tests/`

- `tests/framework/` — framework contracts, manifest/schema/loader tests, controller/CLI behavior, and safe unit coverage. This is the only test tree collected by the default framework CI gate.
- `tests/services/<service>/` — optional implementation tests owned by a service plugin. These are deliberately excluded from framework CI because the service's declarative manifest and live `post_start`/`verify`/`heal` hooks are the runtime source of truth.
- `tests/services/service-catalog/` — discovery, metadata, resource, and essential-service policy tests for the plugin catalog.
- `tests/services/_cross/<area>/` — optional coordination tests spanning more than one service plugin.
- `tests/e2e/` — UI smoke tests, collected explicitly by the E2E job.

When a service needs tests, place them beside its service-owned implementation under
`tests/services/<service>/` (or in the service's standalone repository). Keep
framework tests generic: test loader/manifest contracts and hook dispatch, not a
second hard-coded copy of a service's runtime behavior. Concrete tests that
coordinate several plugins belong under `tests/services/_cross/<area>/`.

## Documentation map

| Need                   | Read                                                       |
| ---------------------- | ---------------------------------------------------------- |
| First deploy           | [operations.md](operations.md)                             |
| Config reference       | [configuration.md](configuration.md)                       |
| Web UI                 | [homelab-ui.md](homelab-ui.md)                             |
| Fleet / external hosts | [fleet-and-external-hosts.md](fleet-and-external-hosts.md) |
| Add a service          | [../adding-a-new-service.md](../adding-a-new-service.md)   |
| Doc index              | [../README.md](../README.md)                               |

## Reorg guidelines

1. **One canonical path** per concern. Internal agent plans are local-only; durable decisions and operator guidance belong in the wiki or an ADR.
2. **Generated output** only under `generated/` or gitignored paths — never commit `.env` or inventory.
3. **New services:** `toolkit/services/<name>/` (`service.yaml` + standalone `compose.yaml` + optional `plugin.py`, `bootstrap.py`, and `image/`), enable its category in `config.yaml`, and add a wiki section when user-facing. See [adding-a-new-service.md](../adding-a-new-service.md).
4. **Do not commit agent workspace artifacts** such as `.superpowers/` or `docs/superpowers/`.
5. **WebUI** mirrors CLI: Deploy, Operations (watchdog, install, update), Manual steps, Hosts/Fleet, Settings — see `toolkit/webui/routers/`.
6. **Runtime state** under `.homelab-state/` (`deploy-*.log`, `last-verify.json`, `last-hooks.json`, `https-probe-cache.json`) — gitignored.
