# ADR 012: Service-Owned Generated Artifacts

## Status

Accepted.

## Context

The central generator rendered Caddy, identity, monitoring, VPN, exporter,
storage, and media configuration through service-name branches. Templates also
lived in one shared directory. Adding or removing a service therefore required
core edits, generated paths could drift from Compose mounts, and node-side
repair bypassed the normal security and validation rules.

## Decision

Each service manifest declares every generated runtime artifact it owns,
including file or symlink type and sensitivity or executable policy. Its plugin
generates the complete set through an `ArtifactGenerationContext` bounded to
those declarations. Service Jinja templates live in that service's `templates/`
directory and use strict undefined values.

The context provides atomic, idempotent writes, path containment, fixed file
modes, immutable secret input, storage-owner application during privileged
repair, and completeness checks. The catalog rejects duplicate artifact owners
and any generated host source or direct Compose mount without a compatible
owner and placement.

Before Compose startup, the guest discovers artifact owners for active runtime
services. Missing or wrong-type outputs are regenerated using the same plugin
hooks; failed repair blocks startup with a specific error.

## Consequences

- A service and all of its generated configuration can be added, changed, or
  removed within one directory.
- Core generation dispatch contains no service-specific rendering logic.
- Initial generation and self-repair enforce identical paths, permissions, and
  completeness.
- Direct writes outside declared paths and silently missing outputs fail before
  deployment.
- Runtime-owned mutable state is not declared as a generated artifact; only the
  deterministic bootstrap files are managed by this contract.
- Watchdog history and recovery memory live under `.homelab-state/`, never in
  the generated-output tree that is reconciled and synchronized to guests.
