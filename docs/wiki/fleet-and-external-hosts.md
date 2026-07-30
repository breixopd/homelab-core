# Fleet and external hosts

External servers (VPS, NAS, cache nodes) join the homelab **without FRP**. Connectivity uses **Headscale** (Tailscale client) plus optional Komodo Periphery, monitoring, and Wazuh agents.

## Add and onboard a fleet node

The Operations page can add, edit, reconcile, and remove both full fleet nodes and lightweight external hosts. The CLI exposes the same desired state for local recovery and automation.

Enter an IPv4 address that the controller can reach **before** onboarding. It
can be a LAN or public management address. Do not enter a future Headscale
address: reconciliation installs the VPN client first, then integrations such
as CrowdSec that depend on the mesh.

Managed hosts use the controller's SSH private key. Authorize its public key in
the remote account's `authorized_keys`, then set the local private-key path in
gitignored `config.local.yaml`:

```yaml
ssh:
  key_file: /absolute/path/to/controller_ed25519
```

The platform deliberately does not store a remote SSH password. Use an
unprivileged account with the required passwordless `sudo` commands when root
SSH is disabled.

```bash
homelab-toolkit fleet add vps-01 203.0.113.10 \
  --cluster-group external \
  --headscale-tag tag:fleet-external

homelab-toolkit fleet onboard vps-01
```

Defaults for Headscale tags come from `config.yaml`:

```yaml
fleet:
  headscale_tags:
    - tag:fleet-external
```

During onboard the toolkit:

1. Trusts SSH and optionally creates an LLDAP user
2. Creates a **reusable Headscale preauth key** with those tags (`headscale preauthkeys create --tags …`)
3. Runs `onboard-fleet-node.yml`: Komodo Periphery, monitoring-agent,
   security-agent, **vpn-client** (`tailscale up --advertise-tags=… --accept-tags`),
   then any integrations that declare the VPN client as a prerequisite
4. Records the successful reconciliation in the node's canonical `config.yaml` entry

ACL policy is generated at `generated/headscale/acl.hujson` (tag owners + permissive mesh ACL). Restart Headscale after changing tags or ACL.

## Generic external hosts

```bash
# Default: monitoring + Wazuh + Headscale (via onboard) + DNS; Komodo Periphery on onboard
homelab-toolkit fleet add nas-01 192.168.1.50 --skip-onboard

# Optional media-cache backend only (add path when enabling that service)
homelab-toolkit fleet add cache-01 192.168.1.60 -s media-cache \
  --integration-setting media-cache.path=/mnt/media --skip-onboard

# Skip automatic onboard (agents only, run onboard later)
homelab-toolkit fleet add edge-01 203.0.113.11 --skip-onboard
homelab-toolkit fleet onboard edge-01
```

`fleet add` records desired state and runs **fleet onboard** unless `--skip-onboard`. Onboarding reconciles DNS, storage integrations, mesh membership, directory access, Komodo, monitoring, and security agents. Use `fleet deploy` to re-apply agent roles without repeating the full enrollment flow.

## DNS

- **LAN / mesh**: AdGuard rewrites on infra
- **Public services**: Cloudflare only (see [Networking](networking.md))

## Apps machine

The default `apps` machine hosts cloud and development services such as
Nextcloud, Immich, Vaultwarden, FMD, SeaweedFS, Gitea, and their service-owned
datastores. Its machine kind, VMID, resources, placement labels, and address are
declared by its machine plugin and can be replaced without changing the core.
