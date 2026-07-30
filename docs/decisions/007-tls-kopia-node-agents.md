# ADR-007: TLS Kopia Repository With Node-Local Agents

## Status

Accepted

## Date

2026-07-11

## Context

The infra Kopia container mounted only the infra install root. Its scheduled
job therefore omitted media and apps data, while its source recursively
included the repository itself. The server listened without TLS, backup health
accepted one recent snapshot for the entire cluster, and the Kopia UI was
published through the service proxy even though backup operations belong in
the homelab operations console.

Kopia's repository-server design keeps storage credentials on the server and
lets authenticated clients snapshot local files through TLS. Its snapshot
command and policy model are explicitly local-source oriented.

## Decision

Infra runs the direct repository server on its private address with a generated
self-signed TLS identity. Media and apps run a minimal `kopia-agent` from the
same service folder. Each agent has a repository-server identity scoped by
Kopia's `username@hostname` model and receives only the shared agent password,
server endpoint, and pinned certificate fingerprint.

A shared node-local operation enrolls the agent, reconciles policy, creates a
role-tagged snapshot, records structured audit evidence, and returns bounded
errors. The same operation is used by the CLI and a persistent six-hour
systemd timer on every node. Verification checks freshness independently for
every enabled role. Rebuildable caches and the repository are excluded with a
root `.kopiaignore`.

The repository server port binds only to the infra private IP. Kopia has no
Caddy route; operators use the homelab operations console and CLI.

## Consequences

- Every data node reads its own filesystem instead of relying on cross-node
  mounts.
- A fresh infra snapshot can no longer conceal a stale apps or media backup.
- Repository credentials and the TLS private key remain on infra.
- Media and apps sync archives explicitly exclude the server identity.
- The controller rotates the TLS identity when the infra private endpoint
  changes and redistributes the new fingerprint.
- Logical database dumps and off-host repository placement remain separate
  consistency and failure-domain concerns.

## Sources

- <https://kopia.io/docs/repository-server/>
- <https://kopia.io/docs/reference/command-line/common/repository-connect-server/>
- <https://kopia.io/docs/reference/command-line/common/snapshot-create/>
