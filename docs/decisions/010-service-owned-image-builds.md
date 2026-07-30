# ADR 010: Service-Owned Image Builds

## Status

Accepted.

## Context

Custom image definitions were duplicated across a Python tuple, GitHub Actions,
local CI, Compose files, and guest synchronization. That made service removal
incomplete by default and left an unused LLDAP helper image in the release
pipeline after runtime code returned to the supported upstream command.

## Decision

Each service that builds an image owns an `image_build` contract in its strict
manifest and keeps its build source in its service directory. The contract
declares the build context, registry repository, Compose image environment key, Dockerfile, CI
participation, smoke checks, dependency input, and explicit audit exceptions.

Catalog loading validates this contract against every build entry in the
service's Compose application and against the repository filesystem. Runtime
generation, image placement, CLI operations, local CI, and GitHub Actions
compile the same validated catalog. The full-repository control-plane image is
the sole supported repository-scoped exception and declares that scope.

## Consequences

- Adding or removing a custom image requires changes only inside its service.
- CI cannot silently omit a service image or retain a removed one.
- CI publishes commit-addressed tags and adds GitHub provenance attestations
  when the repository visibility supports public package attestations.
- Smoke checks and dependency exceptions are reviewable beside their owner.
- Guest deployment pulls published images directly on each target. Automatic
  mode retries pulls and locally builds/transfers only unavailable repositories;
  strict registry and offline-local policies are also explicit.
- Build contexts are forward-only. The removed `images/` layout and centralized
  catalog are not supported.

## References

- [GitHub Container registry authentication and pull behavior](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry)
- [Docker registry login with `--password-stdin`](https://docs.docker.com/reference/cli/docker/login/)
