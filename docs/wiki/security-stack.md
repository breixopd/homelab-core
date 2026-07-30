# Security Stack

Monitoring, threat detection, and network security services. Security tooling runs on the **infra** LXC alongside the services it protects.

For a single owner login (`email` + `SSO_USER_PASSWORD`), forward-auth vs native OIDC, and LLDAP, see **[SSO & accounts](sso-and-accounts.md)**.

## Services

| Service | Purpose | Subdomain |
| --- | --- | --- |
| Prometheus / Grafana | Metrics and dashboards | `grafana.<domain>` (internal by default) |
| Wazuh | SIEM / threat detection | `siem.<domain>` |
| CrowdSec | Collaborative intrusion prevention | Internal |
| Headscale | Mesh VPN coordination server | `vpn.<domain>` |

## Metrics (Prometheus / Grafana)

- **Grafana:** `https://grafana.<your-domain>` (SSO via Authelia)
- **Prometheus:** internal scrape targets; not published to the public internet by default
- Per-guest **monitoring-agent** profile: node-exporter, cAdvisor, and Alloy on media/apps

## Wazuh (SIEM)

Security information and event management for log analysis and threat detection.

- **Features:** File integrity monitoring, vulnerability detection, compliance checking, log analysis

### Architecture

| Component | Runtime | Purpose |
| --- | --- | --- |
| `wazuh-manager` + `wazuh-agent` | Native systemd on infra host | Collect events, run rules, monitor file integrity |
| `wazuh-indexer` | Docker container | OpenSearch-based alert storage |
| `wazuh-dashboard` | Docker container | Web UI for alert triage |
| Filebeat | Native on infra | Bridges manager alerts to the Docker indexer |

### First-Time Setup

1. Wazuh packages are installed by Ansible `host-setup.yml` on the infra LXC
2. Open the dashboard at `https://siem.<your-domain>`
3. Credentials are in `secrets.enc.yaml` (auto-generated)
4. Wazuh agents deploy to managed machines via `deploy-security-agents.yml`

## CrowdSec

Collaborative intrusion prevention — parses logs from Caddy, SSH, and other services to block malicious IPs.

- **Bouncer:** Caddy integration blocks bad actors at the reverse proxy
- **Collections:** Pre-built parsers for common attack patterns

## Headscale

Self-hosted coordination server for Tailscale-compatible mesh VPN.

- **Control plane:** `https://vpn.<your-domain>`
- **Use case:** Remote access to internal services without exposing them publicly
- **Fleet:** External VPS nodes join with preauth keys (`fleet.headscale_tags` in `config.yaml`)

## Hardening checklist

| Item | Detail |
| --- | --- |
| Secrets | Use **SOPS + age** for `secrets.enc.yaml` on the controller; never commit plaintext secrets |
| Shared DB | Infra Postgres/Redis bind to `PRIVATE_IP` in `generated/infra/.env` — verify LAN-only exposure |
| Gitea Actions | `gitea-runner` uses privileged Docker-in-Docker — trust repo/workflow code |
| Dev DB ports | `dev-postgres` (5433) and `dev-redis` (6380) on apps — firewall from WAN |

## Related

- [Networking](networking.md) — exposure and guest routes
- [Operations & deployment](operations.md) — deploy flow
- [Configuration](configuration.md) — `services.security`, Headscale tags
