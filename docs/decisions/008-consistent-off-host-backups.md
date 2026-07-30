# ADR-008: Consistent Database Exports And Restricted Off-Host Storage

## Status

Accepted

## Date

2026-07-11

## Context

Copying live database files does not provide a portable application-consistent
restore point. The configured remote backup host also had no implementation:
backup-only hosts were excluded from deployment and Kopia always initialized a
local filesystem repository.

## Decision

Each database service declares its own `backup_exports` contract in its service
manifest. The core discovers enabled exports by machine placement, streams
container-native PostgreSQL and MongoDB exports, and uses SQLite's Online
Backup API for live SQLite databases. Infra exports shared PostgreSQL, Komodo
MongoDB, Roundcube SQLite, and Headscale SQLite. Apps exports Immich PostgreSQL
the development PostgreSQL service, and FMD SQLite when those groups are
enabled.

Every export is compressed, integrity checked where supported, written to a
private temporary file, and atomically promoted. A failed or empty export
blocks the node snapshot. Live database directories are excluded from Kopia's
raw filesystem inputs because manifest-owned exports are the recovery
artifact.

The local repository remains available as a fast restore tier. A selected
managed backup host is implemented as an SFTP repository backend. Plain and
fleet host playbooks share one `backup_storage` role. It creates the configured
directory and authorizes a dedicated Ed25519 key with a forced
`internal-sftp` command restricted to that directory. Kopia receives only this
key and the target's pinned SSH host key, never the controller administration
key.

Repository bootstrap derives its provider from desired state, disconnects a
mismatched backend, connects or creates the selected target, and reapplies
users and retention. Verification rejects a local repository when desired
state requires SFTP.

## Consequences

- Database restore artifacts are portable across container replacement.
- New database plugins own their backup behavior without core service tables.
- Failed consistency preparation cannot be reported as a successful backup.
- A remote storage host is a real deployable capability rather than UI-only
  metadata.
- Removing a managed backup host removes its restricted authorized key.
- Switching targets is explicit and leaves the previous encrypted repository
  intact for manual retention or cleanup.

## Sources

- <https://kopia.io/docs/repositories/>
- <https://kopia.io/docs/reference/command-line/common/repository-connect-sftp/>
- <https://github.com/roundcube/roundcubemail-docker/blob/master/README.md#persistent-data>
- <https://www.sqlite.org/backup.html>
- <https://headscale.net/stable/setup/upgrade/#backup>
