# Managed Projects

Projects are digest-pinned OCI containers with controller-owned deployment,
routing, DNS, access policy, monitoring, and lifecycle actions. Arbitrary proxy
targets are not accepted.

## Add A Project

Use the **Projects** page or the CLI:

```bash
homelab-toolkit projects add \
  --subdomain status \
  --name "Status" \
  --image ghcr.io/example/status@sha256:<64-hex-digest> \
  --port 8080 \
  --placement apps \
  --database dev-postgres \
  --auth-mode forward_auth \
  --exposure private
```

The image digest, DNS label, container port, placement, exposure, and auth mode
are validated before desired state is saved. Placement accepts either a unique
machine capability label such as `apps` or an exact machine ID. Capability
labels keep projects portable when the topology is renamed or resized. The
upstream is derived from the project identity and cannot be overridden.

The optional `--database` value names an enabled service whose manifest declares
the `database_provider` contract. The UI discovers the same providers dynamically.
Each project receives a dedicated database, role, and generated password. That
password is scoped to the project node and provider node only. Cross-node traffic
uses the private machine network; no public database listener is added.

## Reconciliation

The Web UI queues a deployment after add or remove. The deployment regenerates
and validates Compose, Caddy, DNS, guest firewall rules, and per-node profiles;
then it starts the project in the terminal project wave and verifies container
and optional HTTP health.

CLI changes can be applied with:

```bash
homelab-toolkit projects deploy status
```

## Runtime

```bash
homelab-toolkit projects list
homelab-toolkit projects ps
homelab-toolkit projects status status
homelab-toolkit projects logs status
homelab-toolkit projects restart status
homelab-toolkit projects stop status
homelab-toolkit projects start status
homelab-toolkit projects remove status
```

The Projects page provides the same start, stop, restart, and remove actions,
plus live container state and health from the controller inventory.

## Network Policy

Project host ports bind only to the selected machine's private IP. The guest
firewall permits that port only from the machine providing ingress. Private
routes additionally return 404 unless the immediate client is on the declared
LAN or mesh ranges. Client-supplied identity headers are stripped before any
request reaches an upstream.

## Service Plugins

Use `toolkit/services/<name>/` for full homelab services that need custom
settings, hooks, secrets, dependencies, verification, or management actions.
Use Projects for independent containers that fit the bounded image/port/health
contract. A PostgreSQL service can opt into project provisioning by declaring
`database_provider` in its manifest and reconciling only projects whose
`database_service` selects it.
