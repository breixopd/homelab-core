# SSO, accounts, and Authelia

**LLDAP is your single account directory.** Authelia does not store users — it authenticates against LLDAP via LDAP (`toolkit/services/authelia/templates/authelia.yml.j2`). Manage users and passwords in one place:

1. **Primary:** LLDAP at `https://users.<domain>` (internal — LAN or mesh only)
2. **Owner bootstrap:** `config.email` + `SSO_USER_PASSWORD` in secrets; infra hooks run `bootstrap_lldap_user()` to create/update the owner and sync the password
3. **Authelia:** login portal at `https://auth.<domain>`; sessions and OIDC for apps

You do **not** maintain separate user lists in Authelia. Add users in LLDAP; they can sign in through Authelia immediately.

## Where to set credentials

| What | Where |
| --- | --- |
| Email | `config.yaml` → `email` |
| Password | `secrets.enc.yaml` → `SSO_USER_PASSWORD`, Web UI **Secrets → Owner SSO account**, or `owner_password` in gitignored `config.local.yaml` |
| After password change | `homelab-toolkit deploy hooks --node infra` then sign out/in at Authelia |

## Forward auth vs native OIDC

### Forward auth (mandatory for apps without OIDC)

Caddy calls Authelia **before** proxying. Used for Sonarr, Radarr, Prowlarr, qBittorrent, Navidrome (when internal), homelab-ui, Seerr, Jellyfin/Plex (extra layer), and all internal admin UIs.

Run `homelab-toolkit config exposure` to see which routes use forward-auth vs OIDC.

### Native OIDC

Apps redirect to `https://auth.<domain>`. Configured for Vaultwarden, Nextcloud, Gitea, Grafana, and Immich. OIDC discovery is served **without** forward-auth.

## Public vs private

| Mode | Cloudflare DNS | How you reach it |
| --- | --- | --- |
| **public** | Yes | Internet → Caddy → Authelia → app |
| **private** | No | **LAN** and **Headscale mesh** through AdGuard rewrites; no public A record |

Exposure and authentication are declared by each service's route manifest. The global
`network.expose_via_internet` switch can reduce public routes to private operation but
cannot promote a private route to public.

Managed machines do **not** need Headscale because they share their declared private
network. Headscale is for **phones, laptops, and external fleet nodes** added via Web
UI or `fleet add`.

## Email without mesh

- **MX / mail.*** A records sync to Cloudflare when email is enabled (public mail delivery).
- **IMAP/SMTP** (993, 465/587): with `network.mail_public_access: true`, the mail
  plugin generates public DNAT and guest-firewall rules to its selected machine for
  phone clients (for example FairEmail or Apple Mail). No machine address or manual
  port-forward rule is hardcoded.
- **Webmail** at `https://mail.<domain>` stays **private** (forward-auth via Caddy on LAN/mesh).

## What stays manual

- Plex claim token (if not set in secrets before start)
- Spotify OAuth in music-sync
- Headscale on **personal devices**: `homelab-toolkit mesh join` (OIDC via Authelia — not a preauth key). **Fleet/external** hosts use automated preauth during `fleet onboard`.
- Optional Bazarr subtitle providers

## Troubleshooting

1. `homelab-toolkit deploy verify --sso`
2. `homelab-toolkit deploy hooks --node infra`
3. Confirm LLDAP hook log: password updated for your user
4. Clear cookies for `auth.<domain>`

See [Networking](networking.md) and [Email setup](email-setup.md).
