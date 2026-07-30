# Homelab wiki

Canonical documentation. Start here instead of hunting scattered pages.

## Start here

| Doc | What it covers |
| --- | --- |
| [Operations & deployment](operations.md) | Prerequisites, automated deploy, Web UI path, verify |
| [Prerequisites](prerequisites.md) | One-time setup: domain, SOPS, Cloudflare, Proxmox, VPN, Spotify, age key backup |
| [Configuration](configuration.md) | `config.yaml`, secrets, compose profiles |
| [Repository layout](repo-layout.md) | Where code, Ansible, and generated files live |

## Infrastructure

| Doc | What it covers |
| --- | --- |
| [Infrastructure as code](infrastructure-as-code.md) | OpenTofu, Proxmox LXCs, tfvars |
| [Automation (Ansible)](automation.md) | Playbooks, inventory, provisioning |
| [Networking](networking.md) | Bridges, DNS, Caddy routes, public vs mesh exposure |
| [Fleet & external hosts](fleet-and-external-hosts.md) | NAS, cache nodes, Komodo fleet |
| [OVHcloud setup](ovhcloud-setup.md) | Optional: OVH bare metal, PTR, vRack |

## Services

| Doc | What it covers |
| --- | --- |
| [Media stack](media-stack.md) | *arr, Jellyfin, Tdarr, VPN (Gluetun) |
| [Security stack](security-stack.md) | Authelia SSO, Wazuh, CrowdSec |
| [SSO & accounts](sso-and-accounts.md) | One login, forward-auth vs OIDC, LLDAP |
| [Storage & backups](storage-and-backups.md) | Kopia, ZFS, SeaweedFS S3 |
| [Find My Device](find-my-device.md) | FMD phone protocol, registration, auth, metrics |
| [Email](email-setup.md) | Docker-Mailserver and Roundcube |
| [Homelab UI & ops](homelab-ui.md) | Web console, watchdog, mobile access |
| [Webhooks](webhooks.md) | Grafana alert webhook for auto-heal |
| [Projects](projects.md) | Custom subdomain projects CLI and Web UI |

## Reference

| Doc | What it covers |
| --- | --- |
| [Troubleshooting](troubleshooting.md) | Common failures and fixes |
| [Adding a service](../adding-a-new-service.md) | New service plugin checklist |
