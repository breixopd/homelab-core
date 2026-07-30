# Storage and Backups

## Service storage

Runtime bind mounts are generated under `/opt/homelab` on each node. Every
stateful service declares its source environment variable or named volume,
container target, size estimate, and snapshot policy in
`toolkit/services/<service>/service.yaml`. Generation rejects declarations
that do not match the service's Compose model.

Run this after generation to inspect the resolved inventory programmatically:

```python
from pathlib import Path

from toolkit.core.config.config import load_config
from toolkit.core.manifest.storage import compile_storage_inventory

root = Path("/opt/homelab")
inventory = compile_storage_inventory(load_config(root / "config.yaml"), root)
```

The media library uses the media node's local storage tier. When media cache is
enabled, rclone exposes the configured remote backends through the library
mount and keeps recently accessed content in the local cache.

## Backup topology

Kopia runs as a TLS repository server on the infra private address. Media and
apps each run an unexposed node agent. The Compose compiler mounts only storage
assets whose manifests opt into raw snapshots; repository data, live database
directories, transient caches, and generated guest bundles are not snapshot
inputs.

Before a snapshot, the node discovers manifest-owned, application-consistent
exports for:

- shared PostgreSQL on infra;
- Immich and development PostgreSQL on apps;
- Komodo MongoDB on infra; and
- Roundcube and Headscale SQLite on infra; and
- FMD SQLite on apps.

PostgreSQL and MongoDB use their container-native export tools. SQLite uses the
online Backup API, so WAL-backed services remain available while a consistent
point-in-time database is produced. Any failed or empty export blocks the
encrypted snapshot.

The snapshot timer runs every six hours on every enabled node. Verification
checks freshness per node, so one fresh snapshot cannot hide a stale node.

Manual execution uses the same audited operation as the timer:

```bash
homelab-toolkit maintenance snapshot --node infra
homelab-toolkit maintenance snapshot --node media
homelab-toolkit maintenance snapshot --node apps
```

## Repository targets

### Local

```yaml
backups:
  enabled: true
  target: local
```

The encrypted repository is stored under `KOPIA_REPOSITORY_SOURCE` on infra.
This protects against application errors and supports point-in-time restore,
but it does not protect against loss of the infra storage device.

### Managed off-host storage

Add or edit a managed host in the Fleet view, enable **Backup storage**, and
provide an absolute repository directory. Desired state then selects that host:

```yaml
backups:
  enabled: true
  target: remote
  storage_host: nas-01
```

Reconciliation creates the directory and installs a dedicated Ed25519 key with
a forced `internal-sftp` command restricted to that directory. Kopia receives
that key and the pinned SSH host key; it never receives the controller's
administrative SSH identity. The infra repository server proxies all node
clients to this SFTP backend.

## Restore safety

List immutable dump records and run an isolated restore drill before a real
restore:

```bash
homelab-toolkit maintenance list-dumps
homelab-toolkit maintenance restore-drill <dump-id>
homelab-toolkit maintenance restore-db <dump-id>
```

The drill starts a temporary database container, verifies the dump digest,
restores it, runs a validation query, and issues a recovery checkpoint. A real
restore requires the exact discovered dump ID as confirmation and records an
audit intent before changing PostgreSQL.

Keep the age decryption key in a second, independent location. Losing both the
deployment and that key makes encrypted service credentials unrecoverable.

## ZFS guidance

ZFS adds checksums, compression, scrub-based integrity checks, and fast local
snapshots beneath the application backup layer. For redundant local storage,
prefer a mirror over a stripe:

```bash
zpool create tank mirror /dev/disk/by-id/<disk-a> /dev/disk/by-id/<disk-b>
zfs set compression=lz4 tank
zfs create tank/vms
zfs create tank/backups
```

Use stable `/dev/disk/by-id` paths and verify the selected devices before any
destructive command. ZFS snapshots are not a substitute for the off-host Kopia
repository because they share the same host and failure domain.

## Object storage

The cloud role includes SeaweedFS for application S3 storage and file browsing:

| Endpoint | Purpose |
| --- | --- |
| `https://s3.<domain>` | S3-compatible API |
| `https://files.<domain>` | Authenticated filer UI |

SeaweedFS data is a declared apps storage asset and is included in node
snapshots when its snapshot policy is enabled.
