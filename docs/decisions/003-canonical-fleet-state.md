# ADR-003: Canonical Fleet Desired State

## Status

Accepted

## Date

2026-07-11

## Context

Fleet nodes were persisted in two collections. DNS, Ansible inventory, monitoring, backups, media cache, and deployment consumed one collection, while onboarding status and mesh tags consumed the other. Every create, edit, or delete therefore required two writes plus immediate external side effects. Partial failures could leave the inventories inconsistent.

## Decision

`config.yaml` `external_hosts` is the only fleet and external-host desired-state collection. Each entry has a `kind` of `plain` or `fleet` and reconciliation status, and the controller derives an entity fingerprint for safe mutations; fleet entries additionally carry cluster, directory, and mesh-tag metadata.

Registration and edits use revision-guarded controller resources and update desired state only. Reconciliation applies DNS, media-cache, backup, mesh, directory, Komodo, monitoring, and security integration through a durable controller job with progress events. Completion uses an entity compare-and-set so a job cannot mark concurrently edited state healthy. Removal is a separate fingerprint-bound job that performs bounded cleanup before deleting the unchanged canonical record.

The secondary collection and its loaders are removed.

## Consequences

- UI, CLI, generation, deploy hooks, and automation read one inventory.
- A failed onboarding job leaves desired state intact and can be retried.
- CLI and UI mutations share one cross-process configuration lock and backup-target invariant.
- Managed-host add/edit/remove is available through typed controller resources and durable jobs.
- Per-node Headscale tags are derived directly from the canonical configuration.
