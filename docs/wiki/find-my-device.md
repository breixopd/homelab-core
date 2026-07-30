# Find My Device

The cloud profile deploys FMD Server `0.16.0` on the apps node at
`https://fmd.<domain>`. The official multi-architecture Alpine image is pinned
by digest.

## Authentication

FMD Server 0.16.0 does not implement OIDC. The deployment uses two explicit
layers; it is not exposed without authentication:

- Current Android protocol endpoints (`/version` and `/api/v1/...`) use FMD's
  native encrypted session and access-token protocol so a lost phone can reach
  the server without an interactive SSO redirect.
- The browser application is protected by Authelia before FMD's own account
  login.

Only the exact v1 endpoints registered by FMD 0.16.0 are allowed through the
native-auth route. There is no `/api/v1/*` wildcard, and no unregistered root
endpoint is exposed. Requests are capped at FMD's upstream 15 MB limit. Caddy removes
caller-supplied identity headers and overwrites `X-Real-IP` before proxying, so
clients cannot spoof the address used by FMD's failed-login controls.

## Device registration

`FMD_REGISTRATION_TOKEN` is generated during secret initialization and is
required when adding a device. Deployment syncs it to Vaultwarden as **FMD
Device Registration**. Rotating it affects new registrations only; existing
device access tokens remain managed by FMD.

## Monitoring and data

FMD stores its WAL-mode SQLite database under `data/fmd`. Before every Kopia
snapshot, the service manifest drives an online, integrity-checked
`fmd-server.sqlite.gz` export; the live database directory is not copied as a
raw recovery artifact.
Its Prometheus listener is published on the apps guest only for infra, and the
service-management page shows accounts, active sessions, stored locations,
pictures, pending commands, and failed-login accounts. The public Caddy route
does not expose the metrics listener.
