# ADR-004: Role-Scoped Compose Models

## Status

Accepted

## Date

2026-07-11

## Context

The root Compose model represents the complete homelab topology. Multi-node
deployments previously copied and parsed that full model on every guest, then
used profiles to suppress foreign services. This widened secret and environment
reference exposure, allowed auxiliary overlays to reintroduce services, and
forced validation to enable unrelated infra profiles on media and apps nodes.

An ordinary service must remain folder-scoped, while a small number of
monitoring applications need agents on more than one node.

## Decision

`toolkit/services/<name>/compose.yaml` remains the sole runtime application
source. Every runtime service inherits the `placement` declared by its strict
`service.yaml` manifest. A manifest may add a `runtimes` entry only for runtime
services owned by the same application. That entry owns secondary placement,
optional Compose profile activation, one-shot execution semantics, and required
host resources.

Generation writes two projections:

- `docker-compose.yml` is the canonical, complete topology model used by the
  controller and repository-wide analysis.
- `generated/<role>/compose.yaml` is the configuration-resolved runtime model
  used by Compose on that node.

Role compilation includes only enabled applications and projects assigned to
the role. It removes cross-node `depends_on` entries, rejects omitted
`service:` namespace references, and includes only referenced networks,
volumes, configs, and secrets. Role environment bundles and resource-limit
overlays derive their boundary from the same model.

Guest synchronization delivers the selected role model. Media and apps guests
remove the complete topology model and models for other roles. Infra retains
the complete model because it hosts the controller.

Single-node deployments continue to execute the complete model because every
enabled service shares one Docker daemon.

## Consequences

- A guest cannot activate a foreign service by selecting another profile.
- Compose validation matches the exact document deployed on each node.
- Adding a normal service requires no placement configuration beyond `vm`.
- Distributed companions are explicit, validated, and visible in one manifest.
- Deployment, recovery, shutdown, secret selection, and resource limits must
  resolve their Compose path through the shared deployment-model selector.
