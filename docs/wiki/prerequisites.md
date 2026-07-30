# Prerequisites

One-time setup before your first successful deploy. The Web UI **Setup** wizard and **pre-flight** checks enforce most of these; see also [Operations & deployment](operations.md).

## Always required

| Item | What to do |
| --- | --- |
| **Domain + email** | Set `domain` and `email` in `config.yaml` (or complete `/setup`). Used for TLS, service URLs, and alerts. |
| **SOPS age key** | Run `homelab-toolkit secrets init-sops` on the deploy controller. Creates `keys/age.key` used to decrypt `secrets.enc.yaml`. |
| **Secrets file** | Run `homelab-toolkit secrets generate` after editing config. Stores tokens and generated passwords in `secrets.enc.yaml`. |
| **Age key backup** | Copy `keys/age.key` to a **second location** (password manager, offline USB, escrow). Losing this file means **total secrets loss**. After backup, set `AGE_KEY_BACKUP_ATTEST=1` in secrets (or via `homelab-toolkit secrets set`). Pre-flight fails until attested. |

## Cloudflare (DNS + public exposure)

Required when `dns.provider: cloudflare` (default for internet-facing homelabs).

| Secret | Purpose |
| --- | --- |
| `CLOUDFLARE_API_TOKEN` | DNS record sync and DNS-01 TLS |
| `CLOUDFLARE_ZONE_ID` | Target zone for your domain |

Create a token with **Zone → DNS → Edit** on the homelab zone. Pre-flight checks the token when Cloudflare DNS is enabled.

### Vaultwarden WAF (public vault subdomain)

When `vaultwarden` is exposed as **public** (recommended for mobile Bitwarden apps):

1. Ensure the `vault.<domain>` A record is **proxied** (orange cloud).
2. Add a **Cloudflare WAF or rate-limit rule** that matches `vault.*` (geo-block optional).
3. Alternatively, after manual setup, set `CF_VAULT_WAF_ATTEST=1` in secrets.

Pre-flight verifies proxy + WAF when the API token allows it. See [SSO & accounts](sso-and-accounts.md) for Vaultwarden master password vs SSO.

## Proxmox (IaC deploy)

Required when `proxmox.provision_machines: true`.

| Item | Notes |
| --- | --- |
| `PROXMOX_API_TOKEN_ID` / `PROXMOX_API_TOKEN_SECRET` | API token with VM/LXC permissions |
| Proxmox control SSH key | `proxmox.ssh.key_file` in `config.local.yaml`, authorized on the Proxmox host |
| Managed guest SSH key | `ssh.key_file` in `config.local.yaml`, with its public key in `proxmox.ssh_public_key` for guest injection |
| OpenTofu + Ansible | Installed on the controller (pre-flight checks) |

See [Infrastructure as code](infrastructure-as-code.md).

## Media VPN (Gluetun)

Required when the `gluetun.enabled` service setting is true.

| Secret | When |
| --- | --- |
| `VPN_PROVIDER` | Always (e.g. `nordvpn`, `protonvpn`, `mullvad`, `custom`) |
| `NORDVPN_TOKEN` | NordVPN WireGuard (key auto-derived) |
| `VPN_USER` + `VPN_PASSWORD` | OpenVPN providers |
| `WIREGUARD_PRIVATE_KEY` + `WIREGUARD_ADDRESSES` | Custom WireGuard only |

Pre-flight blocks deploy if provider credentials are missing. Details: [Media stack — VPN](media-stack.md).

## Spotify (music sync)

Required when the `music-sync.enabled` service setting is true.

| Secret | Purpose |
| --- | --- |
| `SPOTIFY_CLIENT_ID` | From [Spotify Developer Dashboard](https://developer.spotify.com/dashboard) |
| `SPOTIFY_CLIENT_SECRET` | App secret |

After deploy you still complete **one OAuth consent** in the music-sync UI (`music-sync.<domain>`). That click is unavoidable and is not a post-deploy manual step in the toolkit list.

## Optional feature-specific

| Feature | Prerequisite |
| --- | --- |
| Plex (not Jellyfin-only) | `PLEX_CLAIM` token before media compose |
| Backups (Kopia) | Backup target credentials per [Storage & backups](storage-and-backups.md) |
| OVH bare metal | [OVHcloud setup](ovhcloud-setup.md) |

## Checklist flow

```text
/setup or config init → secrets init-sops → secrets generate → back up age key → attested
→ fill feature secrets (CF, Proxmox, VPN, Spotify) → pre-flight green → deploy
```

The Deploy page disables the button until pre-flight passes. CLI equivalent: `homelab-toolkit deploy all` (same pipeline).

## Related docs

- [Configuration](configuration.md) — full `config.yaml` schema
- [Operations](operations.md) — deploy, verify, recover
- [Networking](networking.md) — public vs internal exposure
- [Homelab UI](homelab-ui.md) — Web console and watchdog
