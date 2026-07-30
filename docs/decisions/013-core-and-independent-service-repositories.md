# ADR-013: Core Platform and Independent Service Repositories

## Status

Accepted

## Date

2026-07-28

## Context

The homelab repository is both an operator product and an integration platform.
Most infrastructure services are useful only as part of that platform, while
products such as Media Cache and Music Sync must also be installable, released,
tested, and operated without Homelab Core.

Copying independent applications into this repository would couple their
release cycles and discard useful repository boundaries. Git submodules would
move checkout and deployment complexity onto every contributor without
improving the runtime contract. Allowing each application to add custom
controller routes or browser code would make the core unsafe and difficult to
upgrade.

## Decision

Homelab Core owns orchestration and policy:

- desired state, machine inventory, dependency ordering, jobs, and audit data;
- identity, ingress, network policy, secrets projection, monitoring, backup,
  deployment, and recovery;
- the manifest schema, plugin SDK, generic management UI, and bounded controller
  read/write resources;
- a small adapter in `toolkit/services/<service>/` for every integrated
  application.

An independently useful application owns its own repository:

- runtime code, domain logic, native UI, public API, tests, documentation, and
  release pipeline;
- a stable, documented HTTP integration contract;
- versioned, multi-architecture OCI releases and immutable digests;
- standalone Compose or equivalent installation instructions that do not import
  Homelab Core.

Core integrations consume only released application contracts. Their
`service.yaml` declares the immutable image release, routes, secrets, health,
metrics, resources, actions, and host integrations. `plugin.py` translates the
application contract into the generic controller interface and may not import
source from the application repository.

Host integrations declare ordering requirements in their manifests. The core
catalog validates missing references and cycles, then reconciliation performs a
stable topological order. A service cannot rely on an undeclared endpoint,
network, secret, or earlier bootstrap step.

The repositories should be grouped in one GitHub Project, while retaining
separate Git histories. The Project may be user-owned or organization-owned;
repository ownership does not affect the runtime architecture:

| Repository | Responsibility |
| --- | --- |
| `homelab-core` | Core framework, first-party infrastructure plugins, controller, CLI, and Homelab UI |
| `media-cache` | Standalone media cache product and stable v1 API |
| `music-sync` | Standalone music synchronization product and stable HTTP contract |
| future product repositories | Independently useful products with the same release and adapter boundary |

Project fields should track repository, platform milestone, integration state,
and release readiness. Moving repositories into an organization is optional and
must not be a prerequisite for development, releases, or cross-repository work.

## Split Criteria

Create an independent repository only when the component has its own users or
UI, is useful without Homelab Core, needs an independent release cadence, or has
a substantial domain model. Keep deployment adapters, one-off infrastructure
services, and shared policy in core. Extract a shared library only after at least
two independent repositories need the same stable code API; do not share code
merely because response fields look similar.

## Current Boundary Audit

`media-cache` and `music-sync` are the only current components that meet the
split criteria. The remaining service directories are adapters, generated
configuration, or lifecycle policy around third-party software; turning each
into a repository would multiply releases and compatibility edges without
creating independently useful products.

The following are candidates to re-evaluate, not approved repository splits:

- a managed-host agent, after it has a stable daemon/API and is useful outside
  this controller rather than being an Ansible/bootstrap implementation detail;
- backup orchestration, after it owns a general domain model beyond Homelab's
  Kopia/database policies;
- a shared module SDK, only after both independent modules need the same stable
  client/server implementation rather than the current small HTTP convention.

Large core files should still be decomposed internally. Deployment phases,
host onboarding, release verification, and service-management collection are
core modules with narrower responsibilities, not separately released products.

## Consequences

- Media Cache and Music Sync remain standalone and can evolve independently.
- Homelab gains deep integration through typed adapters without becoming a
  monorepo or loading application-specific browser code.
- Application upgrades are explicit digest changes with contract tests.
- Cross-service bootstrap assumptions become catalog errors instead of runtime
  surprises.
- A future GitHub organization move is organizational rather than an
  architectural migration.
