# Service alternatives research

Curated from [selfh.st](https://selfh.st/apps/), awesome-selfhosted, and homelab SSO fit (LLDAP + Authelia). **Bold** = current stack choice.

## Auth principle

Use **native OIDC** when the app supports it (cleaner sessions, no double login). Use **LDAP** for mail protocols and Jellyfin. Use **Caddy forward-auth** only when the app has no SSO protocol, such as the *arr services and AdGuard. Kopia is not web-routed.

---

## Identity & SSO

| Role | Current | Alternatives | Notes |
|------|---------|--------------|-------|
| Directory | **LLDAP** | LDAP Samba AD, OpenLDAP | LLDAP is light and matches homelab scale |
| SSO gateway | **Authelia** | Authentik (~1GB), Keycloak (~1.5GB) | Staying Authelia — lighter, forward-auth first-class |
| Reverse proxy | **Caddy** | Traefik, NPM | Caddy + forward_auth is already wired |

## Management / infra

| Service | Current | Better alternatives? | Switch? |
|---------|---------|---------------------|---------|
| Monitoring | **Prometheus + Grafana + Loki** | Netdata (simpler), Zabbix (heavier) | No — stack is standard |
| Fleet UI | **Komodo** | Portainer, Yacht, Dockge | Komodo fits multi-host; OIDC wired |
| DNS | **AdGuard** | Pi-hole, Technitium | AdGuard forward-auth OK |
| Backups | **Kopia repository server + node agents** | Restic + orchestrator, Borg | Keep; TLS agents and remote SFTP target are automated |
| SIEM | **Wazuh** | Graylog, Security Onion | Wazuh is heavy but valuable |
| VPN mesh | **Headscale** | Tailscale SaaS, Netbird | Headscale + OIDC fits |
| Notifications | **ntfy** | Gotify, Apprise | ntfy is fine; internal POST open by design |
| Mail | **Docker-Mailserver** | Mailcow (heavy), Stalwart | Docker-Mailserver + LLDAP bind auth is automated and fits the current plugin model |
| UI | **homelab-ui** | Homarr, Homepage, Dashy | Custom UI matches toolkit |

## Media

| Service | Current | Alternatives | Notes |
|---------|---------|--------------|-------|
| Video | **Jellyfin** | Plex, Emby | Jellyfin + LDAP; Plex if you need official clients |
| Requests | **Seerr** | Jellyseerr, Overseerr | Same codebase family; forward-auth sufficient |
| Music | **Navidrome** | Audiobookshelf, Funkwhale, Ampache | Navidrome + OIDC auto-create on first login |
| *arr stack | **Sonarr/Radarr/Prowlarr/Bazarr** | None better | Forward-auth is the SSO layer |
| Downloads | **qBit + Gluetun** | Transmission, Deluge | qBit + VPN sidecar is solid |
| Transcode | **Tdarr** | Unmanic, FFmpeg cron | Tdarr if you use library automation |
| Subtitles | **Bazarr** | — | Keep |
| Indexers | **Prowlarr + FlareSolverr** | Jackett | Prowlarr is superset |

## Cloud & dev

| Service | Current | Alternatives | Notes |
|---------|---------|--------------|-------|
| Files | **Nextcloud** | Seafile, Syncthing (no web office) | Nextcloud OIDC auto-provision |
| Photos | **Immich** | PhotoPrism, LibrePhotos | Immich + `OAUTH_AUTO_REGISTER` |
| Passwords | **Vaultwarden** | Passbolt (OIDC), Bitwarden SaaS | VW invitation via welcome email signup link |
| Git | **Gitea** | Forgejo, GitLab CE | Gitea reverse-proxy SSO (not OIDC) — intentional for git/CI |
| Object storage | **SeaweedFS** | MinIO, Garage | S3 and Filer bind to the apps guest IP; the guest firewall permits only infra Caddy |
| Device location | **FMD** | OwnTracks, Home Assistant companion app | Native encrypted phone protocol plus an Authelia browser perimeter |

## Kasm Workspaces — can we host it?

**Possible but not recommended on this fleet without a dedicated VM.**

Kasm provides browser-isolated desktops (like a self-hosted “browser RDP farm”). Typical requirements:

- **2–4 GB RAM baseline** + **~1–2 GB per concurrent session**
- CPU for encoding streams; prefers its own host

Your layout (infra / media / apps) is already busy:

- **media** — Jellyfin, Tdarr, Gluetun, *arr*, optional GPU transcode
- **apps** — Nextcloud, Immich **ML**, Postgres workloads
- **infra** — auth, monitoring, Wazuh, mail

Adding Kasm to **apps** or **media** risks OOM and transcode contention during simultaneous Jellyfin + Immich + Kasm sessions.

**Recommendation:** Skip Kasm unless you add a **fourth LXC/VM** (e.g. 8 GB RAM, no GPU) dedicated to remote desktops.

## Remote desktop / browser apps (if not Kasm)

| Option | RAM | SSO | Fit |
|--------|-----|-----|-----|
| Apache Guacamole | ~1GB | LDAP possible | RDP/VNC gateway |
| Kasm Workspaces | 2GB+ | OIDC | Full desktops, heavy |

---

*Last updated: 2026-06 — aligns with OIDC-native routes and single-place invite flow in homelab-ui `/operations/users`.*
