# Static Service Configuration

This directory contains reviewed static configuration mounted read-only into
containers. Writable application configuration and databases belong under the
ignored `data/<service>/` tree; secret-bearing generated inputs belong under
`generated/`.

| Subdirectory / File         | Purpose                                              |
| --------------------------- | ---------------------------------------------------- |
| `grafana/`                  | Pre-provisioned dashboards, datasources, alerts      |
| `homelab-ui.service`        | systemd unit file for the Homelab UI                 |
| `loki/`                     | Loki log aggregation configuration                   |
| `alloy/`                    | Alloy log collection and delivery configuration      |
| `proxmox/`                  | Proxmox API example `.env` files                     |
| `sops.yaml.example`         | Example SOPS config for secret encryption            |
| `tdarr/bootstrap/`          | Tdarr flow definitions imported during bootstrap     |
| `wazuh/`                    | Wazuh SIEM agent and indexer configuration           |
