# Automation (Ansible)

All provisioning lives under **`automation/ansible/`**. OpenTofu is separate under `infrastructure/`; the full loop merges both via `homelab-toolkit deploy all`.

## Layout

```
automation/
  ansible/
    host-setup.yml          # Proxmox: bridge, ZFS, template, kernel modules
    guest-setup.yml         # LXCs: bootstrap → toolkit → hooks → verify
    site.yml                # Imports host-setup → guest-setup (no OpenTofu)
    playbooks/              # bootstrap, deploy-server-toolkit, fleet, and verification
    roles/                  # vpn_client, komodo_periphery, wazuh_manager, …
    tasks/                  # controller sync, guest venv, operational unit reconciliation
    inventory/              # hosts.yml (generated)
    group_vars/             # all.yml + generated.yml (from toolkit generate)
```

## Typical order

1. `host-setup.yml` — Proxmox prep
2. OpenTofu — LXCs (run via `homelab-toolkit deploy all`)
3. `guest-setup.yml`:
   - `bootstrap-lxc.yml` — packages, Docker
   - `setup-docker-registry.yml` — mirror, log rotation
   - `configure-storage.yml` — media mounts
   - `deploy-server-toolkit.yml` — sync repo, venv, operational timers, compose
   - Security agents + `verify-services.yml`

## Commands

```bash
homelab-toolkit generate
homelab-toolkit machines sync <machine-id>

# One command to deploy everything:
homelab-toolkit deploy all -y

# Ansible-only site stack (no OpenTofu):
ansible-playbook -i automation/ansible/inventory/hosts.yml automation/ansible/site.yml
```

## Operator vs automated

| Automated | Operator |
| --- | --- |
| Bridge, ZFS, template, Docker bootstrap, generated inventory, homelab deploy per role, routes, verify playbook | Proxmox API token, DNS tokens, disk layout overrides |

`deploy-server-toolkit.yml` reconciles host-level systemd services during every
guest deployment, including watchdog, maintenance, backups, and control-only
resource tuning. Controller sync distributes the public automation identity to
all managed nodes and the private key only to the configured control node;
workload nodes actively remove any stale private copy after a role change.
