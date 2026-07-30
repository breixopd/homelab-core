# ADR 011: Service-Owned Host Bind Sources

## Status

Accepted.

## Context

Compose bind paths were assembled by a central Python function keyed by
service-specific environment variables. Adding, renaming, or removing a
service therefore required a core-framework edit, and Compose fallbacks could
drift from generated deployment paths. Several Immich, music-sync, Recyclarr,
and SeaweedFS paths had already diverged.

## Decision

Each host bind environment variable has exactly one `host_sources` owner in a
strict service manifest. The contract stores a normalized path relative to the
configured install root and may select an alternate path through validated
config or service-setting predicates.

Catalog loading checks every Compose environment bind against these contracts.
It rejects missing or duplicate owners, unused declarations, fallback paths
that differ from the canonical path, and consumers whose placement differs
from the owner. Shared resources such as the media library keep one embedded
owner and are consumed by colocated services.

Role environment generation compiles enabled owners and their runtime
placements from the catalog. The central host-path map and its unused duplicate
helper are removed.

## Consequences

- Adding or removing a service bind path requires changes only in its service
  directory.
- Generated and direct Compose execution use the same canonical layout.
- Unsafe traversal and ambiguous conditional path selection fail before
  deployment.
- A path cannot be shared across machines implicitly; distributed storage must
  use an explicit network or replicated-storage service contract.
- The former underscore-style directory names are not supported.
