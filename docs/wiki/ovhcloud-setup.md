# OVHcloud Setup

This page documents OVHcloud-specific configuration for the homelab: API credentials, PTR/reverse DNS, port 25 whitelist, Proxmox bridge setup, and vRack networking.

---

## OVHcloud API credentials

OVHcloud provides a REST API for DNS management, service administration, and infrastructure automation.

### Creating API credentials

1. Go to [https://api.ovh.com/createToken/](https://api.ovh.com/createToken/)
2. Log in with your OVHcloud account (or a **sub-account** with restricted permissions).
3. Fill in the application details:
   - **Application Name**: `homelab-toolkit` (or your choice)
   - **Application Description**: Optional
   - **Validity**: Unlimited or set an expiry date
4. Set the required permissions for DNS management:

   ```
   GET    /domain/zone/*
   POST   /domain/zone/*
   PUT    /domain/zone/*
   DELETE /domain/zone/*
   ```

5. Click **Create Keys**.

You will receive three values:

| Credential | Description | Where to store |
| --- | --- | --- |
| **Application Key** (`OVH_APPLICATION_KEY`) | Identifies your app | Toolkit secrets (encrypted) |
| **Application Secret** (`OVH_APPLICATION_SECRET`) | Authenticates your app | Toolkit secrets (encrypted) |
| **Consumer Key** (`OVH_CONSUMER_KEY`) | Authorises access to your account | Toolkit secrets (encrypted) |

### Storing in the toolkit

The toolkit's secrets system (SOPS-encrypted YAML) is the intended storage location. Add these entries to your secrets file:

```yaml
OVH_APPLICATION_KEY: your-app-key
OVH_APPLICATION_SECRET: your-app-secret
OVH_CONSUMER_KEY: your-consumer-key
```

These can be set via the Homelab UI Secrets page or by editing `secrets.enc.yaml` directly (decrypt with `sops` first).

> **Note**: Currently the toolkit's DNS automation supports Cloudflare natively (`CLOUDFLARE_API_TOKEN` in secrets). OVHcloud API credential secrets are reserved for future provider-agnostic DNS support.

---

## PTR / Reverse DNS Configuration

OVHcloud does **not** automatically set PTR records for dedicated servers or VPS. You must configure them manually.

### Via OVHcloud Control Panel

1. Log in to the [OVHcloud Control Panel](https://www.ovh.com/auth/).
2. Navigate to **Bare Metal Cloud** → **Dedicated Servers** (or **VPS** → your VPS).
3. Select your server.
4. Go to the **Network** or **IP** tab.
5. Find the public IP address and click the **…** (options) button.
6. Select **Edit reverse** (or **Modify reverse**).
7. Enter the FQDN (e.g. `homelab.example.com`).
8. Click **Confirm**.

### Via OVHcloud API

```bash
# Requires OVH API credentials with PUT /ip/* permission
curl -XPUT \
  -H "X-Ovh-Application: $OVH_APPLICATION_KEY" \
  -H "X-Ovh-Consumer: $OVH_CONSUMER_KEY" \
  -H "Content-Type: application/json" \
  -H "X-Ovh-Timestamp: $(date +%s)" \
  --data '{"ipReverse": "homelab.example.com"}' \
  "https://api.ovh.com/1.0/ip/$(YOUR_PUBLIC_IP_ESCAPED)/reverse"
```

Replace `$(YOUR_PUBLIC_IP_ESCAPED)` with your IP where dots are `%3A` (e.g. `1.2.3.4` → `1.2.3.4` — OVH API uses `/`-separated blocks, check the OVH API documentation for the exact format).

### Verification

```bash
dig -x <your-public-ip> +short
# Should return: homelab.example.com
```

---

## Port 25 (SMTP) Whitelist Confirmation

OVHcloud **blocks outbound port 25 by default** on all dedicated servers and VPS to prevent spam. If you need to send email directly (e.g. Docker-Mailserver, Authelia notifications), you must request the block to be lifted.

### Request process

1. Log in to the [OVHcloud Control Panel](https://www.ovh.com/auth/).
2. Navigate to **Bare Metal Cloud** → your server.
3. Open a **support ticket** requesting:
   - Outbound port 25 unblock
   - Reason: running a personal mail server for the homelab
   - Commitment: following anti-spam best practices (DKIM, SPF, DMARC)
4. OVHcloud typically responds within 24–48 hours.

### Alternative (recommended)

Use a **SMTP relay** service instead of direct outbound mail:
- SendGrid, Mailgun, or Amazon SES
- Configure as `SMTP_HOST` / `SMTP_PORT` in config.yaml
- Avoids the port 25 block entirely
- Better deliverability for transactional email

---

## Proxmox Bridge Configuration on OVHcloud

OVHcloud dedicated servers have a specific network layout that differs from typical on-premise homelab setups.

### Default OVHcloud networking

```
Public interface: eno1 (or ens3f0) — configured with your public /64 IPv6 and /32 IPv4
Private interface: eno2 (or ens3f1) — connected to OVHcloud vRack (L2 VLAN)
```

### Bridge setup for Proxmox

The Proxmox playbook creates the private bridge and CIDR declared by the machine
selected for infrastructure services. The example defaults are `vmbr1` and
`10.10.10.0/24`; on OVHcloud hardware, bind the configured private bridge to the
**vRack interface**, not the public interface.

Example `/etc/network/interfaces` snippet for OVHcloud:

```
# Private bridge on vRack interface
auto vmbr1
iface vmbr1 inet static
    address 10.10.10.1/24
    bridge-ports eno2
    bridge-stp off
    bridge-fd 0
```

**Important differences from on-premise setup:**

| Feature | On-premise | OVHcloud |
| --- | --- | --- |
| Public IP access | Direct on bridge | Failover IP / Additional IP |
| Private bridge interface | Any free NIC | `eno2` (or vRack NIC) |
| Default gateway | Router on LAN | OVHcloud gateway (in your IP block) |
| MAC address | Not important | Must match OVHcloud-registered MAC for failover IP |

### Failover / Additional IP

If you need a public IP for a specific VM (not NAT):
1. Order a **Failover IP** (or "Additional IP") from OVHcloud Control Panel.
2. Attach it to your hypervisor's MAC address.
3. Bridge the IP directly to the VM.

---

## vRack Considerations

OVHcloud **vRack** is a private L2 network that connects your servers and services within the same data centre.

### Enabling vRack for homelab VMs

1. Ensure all your OVHcloud services (dedicated servers, VPS, etc.) are in the **same vRack**.
   - OVHcloud Control Panel → **Bare Metal Cloud** → **vRack** → add services.
2. Assign the private NIC (for example `eno2`) to the configured private bridge.
3. Verify inter-machine connectivity on the configured private CIDR.

### vRack benefits

- **Low latency**: L2 adjacency between hosts in the same DC.
- **Security**: Traffic never leaves the OVHcloud private network.
- **Bandwidth**: Typically 10 Gbps (depends on server model).
- **No egress costs**: vRack traffic is free.

### Troubleshooting

| Symptom | Likely cause | Fix |
| --- | --- | --- |
| Machines cannot reach each other on the private CIDR | vRack VLAN not configured | Verify the NIC is in the configured private bridge and the VLAN is set |
| Can ping vRack IP but no TCP connectivity | Firewall on guest | Check UFW/iptables on guest VMs |
| Intermittent connectivity | STP on bridge | Set `bridge-stp off` |
| No route to public internet from VMs | Missing iptables MASQUERADE on host | Enable IP forwarding + NAT on Proxmox host |

---

## Reference

- [OVHcloud API Documentation](https://docs.ovh.com/gb/en/api/)
- [OVHcloud vRack Documentation](https://docs.ovh.com/gb/en/public-cloud/public-cloud-vrack/)
- [OVHcloud Failover IP](https://docs.ovh.com/gb/en/dedicated/failover-ip/)
- [Port 25 Request](https://www.ovhcloud.com/en/support/)
