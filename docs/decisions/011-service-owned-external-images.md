# ADR 011: Service-Owned External Images

## Status

Accepted.

## Context

Every container plugin consumes external registry content. Mutable tags make a
reviewed source tree non-deterministic, while duplicating the same image in a
central lock file moves ownership away from the service. Some first-party
services also have independent release cycles and repositories; building those
images from a Homelab checkout duplicated source ownership and slowed clean
deployments.

## Decision

Every non-built runtime declares `repository:version@sha256:index-digest`
directly in its plugin-owned `compose.yaml`. Catalog loading rejects missing,
floating, digest-only, `latest`, or malformed references before generation.
There is no duplicated central runtime-image catalog.

`homelab-toolkit images lock --write` resolves unpinned version tags through
official registry metadata and writes only parsed Compose image fields. Docker
Hub uses its tag API; other registries use Docker Buildx manifest inspection.
Resolution is concurrent and retry-bounded. A mode-0600, one-hour cache below
`.homelab-state` retains successful lookups across transient registry failures,
but it is never a deployment source of truth. `--refresh` re-resolves existing
locks. Renovate preserves and updates Compose digests in reviewable pull
requests.

An independently released first-party service additionally declares
`image_release` in its manifest. That contract owns the Compose service,
explicit GHCR repository, reviewed version tag, and verified OCI index digest.
Its Compose application uses the same tag and digest and contains no local
build context.

Catalog loading also rejects mismatched first-party Compose images, unknown
runtime services, and duplicate repository ownership. Normal deployment pulls
the public multi-architecture image directly on its target. Update discovery
projects only the declared version tag into its isolated scanner model; an
approved update resolves the new registry digest before deployment and uses the
existing snapshot, health-gate, and rollback workflow.

Registry discovery is not inferred from repository names. Explicit ownership
keeps generation deterministic and prevents an unrelated or newly created
package from silently replacing reviewed local source.

Homelab-owned custom images follow the same pull-first delivery model. Their
service manifests declare the supported build platforms, CI publishes a single
multi-platform OCI index, and Docker selects the target guest architecture.
Application images are never downloaded to or built on the Proxmox hypervisor.
When no registry artifact exists, the controller detects each guest platform,
builds that platform, and transfers the image directly to the guest.

## Consequences

- Clean installs download verified release images instead of compiling them.
- Every generated deployment remains bound to reviewed registry content even
  if an upstream tag is later moved.
- Each standalone service owns its source, tests, dependencies, release notes,
  SBOM, provenance, attestations, and release cadence.
- Homelab owns deployment policy, secrets, placement, storage, networking,
  management controls, health verification, update approval, and rollback.
- Adding or removing an external service remains a service-directory change;
  core contains no service-specific image list.
- Mixed `amd64` and `arm64` guest fleets use the same reviewed image reference;
  local fallback remains architecture-aware.

## References

- [GitHub Container registry permissions](https://docs.github.com/en/packages/learn-github-packages/about-permissions-for-github-packages)
- [GitHub artifact attestations](https://docs.github.com/en/actions/security-for-github-actions/using-artifact-attestations)
- [Docker image digests](https://docs.docker.com/dhi/core-concepts/digests/)
- [Docker Hub API](https://docs.docker.com/reference/api/hub/latest/)
