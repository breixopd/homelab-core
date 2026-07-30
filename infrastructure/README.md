# Infrastructure as Code (OpenTofu)

Provider-managed Proxmox machines compiled from the root desired state.

## Prerequisites

1. **OpenTofu** (or Terraform) on your control machine.
2. **Proxmox API token** in the toolkit encrypted secret store. Generation writes the provider value to ignored `terraform.tfvars`.
3. Network access from Proxmox to the configured machine image sources.

## Provider

Uses the **BPG Proxmox provider** (`bpg/proxmox`, `~> 0.111`) for LXC and VM lifecycle management. The checked-in lock file selects and verifies the exact provider artifact.

The toolkit downloads the default LXC template through a `proxmox_download_file` resource. Its URL, SHA-256 checksum, and `vztmpl` datastore are typed `proxmox` settings in `config.yaml`; an individual machine plugin can instead provide `template_file_id`.

TLS verification is mandatory. The toolkit uses `proxmox.tls_ca_file` when set;
otherwise it retrieves the cluster CA over authenticated SSH and builds an
ignored system-plus-Proxmox bundle for every OpenTofu operation.

## Sync from `config.yaml`

Machine plugins and provider settings are generated into **`generated.auto.tfvars`**:
```bash
homelab-toolkit --root . sync
```

## Usage

Use `homelab-toolkit deploy all`; it refreshes generated inputs and the private
CA bundle, then runs OpenTofu with the managed token and trust environment.
Direct OpenTofu review requires the generated files and exporting
`SSL_CERT_FILE=.homelab-state/trust/proxmox-ca-bundle.pem` when the Proxmox API
uses its cluster CA.

## Machines

`config.yaml` owns the machine map. Each enabled, managed entry may be an `lxc` or `vm` and declares its own VMID, hostname, placement labels, resources, disks, bridges, and image override. Adding or removing a machine does not require editing this module.

## Networking

Machines only receive the interfaces declared by their plugin. A non-empty `public_bridge` adds a DHCP interface; the private bridge receives the declared static address and gateway.

## Ansible Integration

The toolkit compiles the same machine map into the Ansible inventory, so provisioning and configuration share one topology.

## Secrets

- **`terraform.tfvars`** — contains `proxmox_api_token` (sensitive). Never commit.
- **`generated.auto.tfvars`** — auto-generated from `config.yaml`, contains non-sensitive config.
