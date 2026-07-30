# Machine Templates

Add reusable machine definitions at `machines/<template-id>/machine.yaml`.
Each file is validated against the strict `MachineSpec` contract and appears in
the Web UI and `homelab-toolkit machines add --template` catalog.

Configured machine instances remain in `config.yaml`; templates are immutable
starting points and do not change existing instances when edited or removed.

Use `toolkit/machines/*/machine.yaml` as complete examples for LXC definitions.
VM templates additionally require a pinned cloud-image URL, SHA-256 digest,
import-enabled datastore, image format, and administrator user.
