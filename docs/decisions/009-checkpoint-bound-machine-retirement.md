# ADR-009: Checkpoint-Bound Managed-Machine Retirement

## Status

Accepted

## Date

2026-07-15

## Context

Removing a machine definition is safe only before the guest exists. Once a
managed LXC or VM has been provisioned, deleting its configuration first can
orphan infrastructure, while destroying it without a recovery proof can make a
mistake irreversible. A browser request must also not be able to reach the
unbounded full-environment destruction path.

The project uses one OpenTofu state for the declared machine topology. Retiring
one guest therefore needs an explicitly bounded operation. OpenTofu documents
resource targeting as an exceptional mechanism because routine use can hide
drift. Retirement is an exceptional lifecycle transition rather than the
normal reconciliation path, so it needs additional independent evidence.

## Decision

Retirement is a controller-owned operation with two distinct actions:

- `retire_machine` targets exactly one enabled, managed, non-control machine
  that hosts no service or project.
- `destroy_all` includes every enabled machine and remains available only to a
  local operator.

The controller issues an actor-bound immutable plan only after a recent,
verified restore-drill checkpoint exists for the scope. The plan binds the
action, machine ID, checkpoint evidence digest, and SHA-256 revision of
`config.yaml`. Approval requires an exact typed phrase and yields a short-lived,
single-use token. Job submission consumes that token transactionally.

Execution rechecks the configuration revision and retirement blockers while
holding the configuration lock. It then holds the global destructive-operation
lease, revalidates the checkpoint, and runs an OpenTofu targeted destroy for
the machine's guest, generated password, and VM image resources. Success is not
accepted until both OpenTofu state and the relevant Proxmox LXC or QEMU
inventory independently prove the resources are absent. Only then is the
machine removed from desired state and all generated artifacts revalidated.

## Alternatives Considered

### Remove the configuration and run a normal apply

This makes the desired-state mutation visible before the destructive action
has been proven and a normal apply may also reconcile unrelated pending drift.
Rejected because retirement needs an isolated and auditable failure boundary.

### Destroy directly from the CLI or browser

This bypasses durable jobs, actor binding, recovery proof, and structured
progress. Rejected because destructive workflows need one policy boundary and
one audit trail.

### Give every machine a separate OpenTofu state

This avoids resource targeting but multiplies backend, locking, credential,
and cross-machine dependency management for a small topology. Rejected for now
because the operational cost is greater than the bounded exceptional use of
targeting. Revisit if machine states require independent teams, backends, or
high-frequency lifecycles.

## Consequences

- The UI can retire one eligible machine but can never request a global wipe.
- A config edit, expired approval, missing checkpoint, occupied machine, state
  mismatch, or Proxmox inventory mismatch fails closed.
- Desired state is preserved when destruction is not independently verified.
- A generation failure after verified destruction leaves the already-removed
  machine absent from desired state and is reported as a failed job that can be
  retried safely with `homelab-toolkit generate`.
- Targeted OpenTofu operations remain confined to this exceptional workflow;
  normal deployment always performs full desired-state reconciliation.

## Sources

- <https://opentofu.org/docs/cli/commands/plan/#resource-targeting>
- <https://opentofu.org/docs/cli/commands/apply/>
- <https://pve.proxmox.com/pve-docs/api-viewer/index.html#/nodes/{node}/lxc>
- <https://pve.proxmox.com/pve-docs/api-viewer/index.html#/nodes/{node}/qemu>
