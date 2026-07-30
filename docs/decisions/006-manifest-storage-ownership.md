# ADR-006: Manifest-Owned Persistent Storage

## Status

Accepted

## Date

2026-07-11

## Context

Persistent-data metadata was split between category YAML and a few service
manifests. Most entries described container paths without identifying the host
source, and nothing checked them against Compose. Several paths were wrong and
the metadata was not consumed by backup or restore automation.

## Decision

Every stateful container service declares its persistent assets in its strict
service manifest. Each asset has a stable name, exactly one host source
environment variable or named volume, an absolute container target, an
estimated size, and an explicit snapshot policy.

Catalog loading validates each asset against the owning service folder's
Compose model. A stateful service without an asset, an asset on a service not
marked stateful, an ambiguous source, or a declaration without a matching
mount is rejected. Category-level storage metadata is removed.

## Consequences

- Service folders are the only source for container storage ownership.
- Backup inventory can resolve concrete host paths from generated role
  environments instead of guessing from container paths.
- Rebuildable caches remain visible but can opt out of snapshots.
- Invalid storage declarations stop generation before deployment.
- Host-managed data, such as Wazuh manager state, needs a separate host-service
  storage contract rather than being represented as container data.
