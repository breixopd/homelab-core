# Service Lifecycle (Add, Update, Remove)

Each service is a self-contained directory under `toolkit/services/<name>/`.
The framework discovers its manifest and plugin, validates their shared
contract, generates only the environment assigned to its runtime node, and
connects it to deploy, verification, metrics, settings, actions, and recovery.
Adding a service does not require a framework-code change.

Use an existing category ID from `toolkit/categories/*/category.yaml`. To add a
new category, create `toolkit/categories/<name>/category.yaml`; the CLI, Web UI,
config validator, deploy planner, and service catalog discover it automatically.
Category manifests are strict. Optional validation or Compose-profile behavior
lives in that category's `plugin.py` and is referenced by function name from the
manifest, so there is no central category registry to edit.

```yaml
name: photos
label: Photos
placement: apps
priority: 35
description: Photo management services
compose_profiles: [photos]
depends_on: [management]
```

```
toolkit/services/my-service/
|-- plugin.py        # MyServicePlugin(ServicePlugin): behavior
|-- service.yaml     # declarative runtime and management contract
|-- compose.yaml     # standalone Compose application
|-- ansible/         # optional service-owned host lifecycle hooks
|-- templates/       # optional service-owned generated configuration
|-- image/           # optional service-owned custom image context
`-- bootstrap.py     # optional idempotent setup module
```

## Add a New Service

### 1. Create the service directory + three files

```bash
mkdir toolkit/services/my-service/
```

**`compose.yaml`** — a standalone Docker Compose application:

```yaml
services:
  my-service:
    restart: unless-stopped
    networks: [edge]
    profiles: [media, svc-my-service]
    image: org/my-service:1.2.3@sha256:<verified-image-digest>
    container_name: my-service
    environment:
      TZ: ${TZ:-UTC}
    expose: [8080]
    healthcheck:
      test: ["CMD", "curl", "-sf", "http://localhost:8080/health"]
      interval: 30s
      timeout: 5s
      retries: 3
    logging:
      driver: json-file
      options:
        max-size: 10m
        max-file: "3"
    mem_limit: 512m
    cpus: 1.0
```

Use `expose` for traffic routed over the Compose network. Publish a host port
only when Caddy runs on another guest or a documented non-Compose client needs
it; bind that port to `${PRIVATE_IP:-127.0.0.1}`. Pin production images to an
upstream release and verified digest.

During development, a new plugin may start with the reviewed version tag. Run
`homelab-toolkit images lock --plugin my-service --write` to resolve and write
its OCI index digest. Generation refuses to run until the digest is present.
Use `--refresh` after deliberately changing a tag or when auditing whether an
upstream moved it; the command reports registry progress and resumes transient
failures from its short-lived local cache.

Every runtime must declare bounded logs. The catalog accepts Docker's rotating
`local` driver or `json-file` with both `max-size` and `max-file`; an unbounded
plugin is rejected before generation or deployment.

When a service needs a custom image, keep its source in `image/` and declare
the build and verification contract in `service.yaml`:

```yaml
image_build:
  context: image
  env_var: HOMELAB_MY_SERVICE_IMAGE
  # Optional; defaults to the service name.
  repository: my-service
  # Optional; defaults to both supported guest architectures.
  platforms: [linux/amd64, linux/arm64]
  smoke_tests:
    - command: [my-service, --version]
    - entrypoint: python
      command: [-c, "import app; print('ready')"]
      contains: ready
  requirements: requirements.txt
```

Then reference that environment key from every locally built Compose runtime:

```yaml
services:
  my-service:
    build:
      context: ./toolkit/services/my-service/image
    image: ${HOMELAB_MY_SERVICE_IMAGE:?run homelab-toolkit generate}
```

Catalog loading rejects missing contexts, mismatched Dockerfiles, undeclared
Compose builds, duplicate environment keys or repository ownership, and missing dependency inputs.
`homelab-toolkit images list`, GitHub Actions, local CI, guest sync, and Compose
environment generation all discover this declaration automatically. Use
`repository_context: true`, `context: .`, and an explicit `dockerfile` only for
an image that genuinely needs the full repository as its build context.
Deployments pull the declared repository directly on each target and, under
the default `auto` policy, locally build and transfer only pull failures. CI
publishes one OCI index containing every declared platform; keep the default
unless the image has a verified architecture constraint. Local fallback checks
the guest-reported Docker platform against this declaration before building.

For a service released from its own repository, publish and verify the
multi-architecture image first, then declare the immutable release instead of
adding a local build context:

```yaml
image_release:
  compose_service: my-service
  repository: ghcr.io/example/my-service
  version: v1.0.0
  digest: sha256:<64-hex-character OCI index digest>
```

```yaml
services:
  my-service:
    image: ghcr.io/example/my-service:v1.0.0@sha256:<same digest>
```

The catalog rejects mutable or mismatched references. Deployment pulls the
digest directly, while the isolated update scanner uses the declared tag and
the update controller resolves every approved target back to a digest before
rollout. Do not infer package ownership from a matching repository name.

**`service.yaml`** — declarative metadata:

```yaml
name: my-service
label: My Service
description: What it does
icon: box
category: media
placement: media
priority: 50
restart_policy: careful
depends_on: [postgres]
memory_tier: medium
stateful: false

routes:
  - subdomain: my-service
    upstream: my-service:8080
    published_port: 8080
    exposure: private
    auth:
      mode: forward_auth

required_secrets:
  - name: MY_SERVICE_API_KEY
    tier: generated
    length: 32
    description: API key for external integration
  - name: MY_SERVICE_REMOTE_TOKEN
    tier: user
    length: 32
    description: Token for the upstream account
    setup:
      label: Upstream account token
      input: password
      required: true
      when:
        - setting: my-service.enabled
          equals: true

service_endpoint:
  container_port: 8080
  published_port: 8080

health:
  public_probe_path: /health

guidance:
  - id: authorize-account
    phase: post_deploy
    category: Required
    title: Authorize the upstream account
    instructions: Visit {url} and complete authorization for {domain}.
    route_url: true

operator_bookmark:
  section: Custom tools
  priority: 50
  description: Manage My Service

identity:
  # Omit access_groups to inherit the category plugin's service_group.
  invite:
    group: homelab-media
    priority: 50
    path: /
    blurb: Use My Service with your family account
    sign_in: Sign in with Authelia.
  provisioning:
    - id: my_service_first_login
      mode: first_login
      priority: 100
      message: My Service creates the account on first login.
      disabled_message: My Service is disabled.

management:
  settings:
    - key: enabled
      label: Enabled
      description: Run this service.
      type: boolean
      default: true
      setup: true
    - key: workers
      label: Workers
      description: Concurrent background workers.
      type: number
      default: 2
      minimum: 1
      maximum: 16
  actions:
    - id: reconcile
      label: Reconcile now
      description: Reconcile remote state immediately.
      confirmation: Reconcile this service now?
  metrics:
    - key: queue_depth
      label: Queue depth
      source: status
      field: queue_depth
      unit: count
  resources:
    - key: remotes
      label: Remotes
      columns:
        - key: name
          label: Name
        - key: state
          label: State

variables:
  MY_SERVICE_WORKERS: '{setting.workers}'
```

Set `health.public_probe_path` only when the service has a default public
route and exposes an unauthenticated readiness endpoint. The external uptime
runner discovers it automatically and probes the real DNS, CDN, TLS, and Caddy
path; no central service list is maintained.

Use `guidance` only for unavoidable operator work or useful optional follow-up.
`pre_deploy` entries must use the `Prerequisite` category; `post_deploy`
entries use `Required`, `Verify`, or `Optional`. Guidance belongs to the
service that requires it and is shown only while that service is enabled.
Enable `route_url` to derive the service URL from its one default compiled
route. Instructions may interpolate only `{url}` and `{domain}`. DNS,
hypervisor, and generic recovery guidance remain platform-owned.

Access groups are declared by category plugins in `category.yaml`. Each
category selects a `service_group`; services inherit it unless
`identity.access_groups` explicitly narrows or broadens access. Forward-auth
rules are compiled from enabled default routes and always include the one
plugin-declared administrator group. The final wildcard is administrator-only,
so a new service cannot accidentally inherit family access.

An optional `identity.invite` block adds a service card to welcome email,
activation, and family portal views. Its group must be one of the service's
effective access groups. The URL is derived from the one default route, while
`path` is a safe route-relative suffix. Labels come from the service manifest;
no central invite-card or hostname registry is maintained.

`identity.provisioning` defines stable steps returned by user invite and
reprovision operations. Use `first_login` for a declarative pending step. Use
`plugin` only when the service must perform an API or container mutation, and
implement `ServicePlugin.provision_identity()` with typed results for every
declared ID. Plugin discovery rejects a missing or undeclared implementation;
the dispatcher rejects duplicate, unknown, or missing result IDs and reports a
bounded failure without exposing exception details. Disabled services emit the
manifest's `disabled_message` without executing plugin code.

`operator_bookmark` adds an enabled service to operator dashboard navigation.
Its title comes from the service label and its URL comes from the compiled
default route. For a service with multiple default routes, set
`route_subdomain` to select one explicitly. Sections retain plugin discovery
order and entries use `priority`, so custom services can add navigation without
editing a template or central bookmark list. Host commands and SSH links do not
belong in browser navigation; expose host state through a typed management
resource instead.

Set `setup: true` only on the small set of service settings that belong in the
first-run wizard. The controller discovers those fields, projects their typed
constraints into the UI, and rejects bootstrap requests that try to set any
other service option. All declared settings remain available from the service
management page after initialization, so adding a plugin never requires a
bootstrap controller or template change.

User-supplied secrets can opt into the same wizard with a nested `setup`
contract. Conditions are an AND-list of service-setting predicates and may
reference only settings exposed during setup. The UI hides and disables
inactive inputs, while the controller independently re-evaluates the
conditions, rejects inactive or undeclared names, and enforces `required`.
Service-specific derived values belong in `prepare_bootstrap_credentials()` on
the owning plugin; returned names must also be declared by that manifest.

When a route's placement differs from Caddy's placement, `published_port` is
required and the Compose model must publish the matching port on
`${PRIVATE_IP:-127.0.0.1}`. Generation verifies the assembled route and the
guest firewall admits only Caddy's machine as a client.

Non-HTTP host listeners use the same fail-closed ownership model:

```yaml
network_listeners:
  - id: agent-api
    port: 8080
    runtime_service: my-service-agent
    sources: ['@service:prometheus', '@integration:monitoring-agent']
```

Compose listeners are rejected unless that runtime publishes the declared
port on a non-loopback host interface. Set `host_process: true` only for a
listener installed directly on the service's machine. Sources may be a machine
ID or unique capability label, `@all`, `@lan`, `@mesh`, a runtime owned by the
same plugin (`@runtime:<name>`), another enabled service
(`@service:<name>`), or hosts that selected an integration
(`@integration:<id>`). Fleet integrations resolve to the enrolled mesh range;
plain hosts remain restricted to their configured IP.

Do not declare HTTP routes, Prometheus scrapes, service dependencies, or
Project database connections again as network listeners. The compiler derives
those flows from `routes`, `prometheus`, `depends_on`, and `database_provider`.

Metrics targets are also plugin-owned and may share a Prometheus job:

```yaml
prometheus:
  - id: controller
    job: my-service
    container_port: 9100
  - id: managed-agents
    job: my-service
    container_port: 9100
    host_port: 9100
    runtime_service: my-service-agent
  - id: external-agents
    job: my-service
    host_port: 9100
    host_integration: monitoring-agent
```

Primary containers use Compose DNS when colocated with Prometheus. Runtime and
cross-node targets require a published `host_port`; external targets appear
only for hosts that selected the named integration. The compiler rejects
undeclared runtimes and integrations, conflicting paths within one job, and
host ports without a matching Compose publication. The same target model
drives Prometheus configuration and guest firewall access.

Services that provision isolated PostgreSQL tenants for managed Projects may
also expose a typed connection contract:

```yaml
database_provider:
  engine: postgresql
  compose_service: my-postgres  # omit when it matches `name`
  container_port: 5432
  published_port: 5433          # required only for cross-node projects
  admin_username_env: MY_POSTGRES_USER
  admin_password_env: MY_POSTGRES_PASSWORD
  admin_database_env: MY_POSTGRES_DB
```

The Projects CLI and UI discover enabled providers directly from this block.
The provider plugin remains responsible for idempotently creating and verifying
the selected tenants; core code handles connection projection and least-privilege
secret delivery.

A service that consumes a provider owns its database and role declaration:

```yaml
depends_on: [my-postgres]
databases:
- provider: my-postgres
  database: my_service
  username: my_service
  password_env: MY_SERVICE_DB_PASSWORD
required_secrets:
- name: MY_SERVICE_DB_PASSWORD
  tier: generated
  description: My Service PostgreSQL password
```

The catalog requires the provider dependency, validates the local secret owner,
and rejects duplicate database or role ownership. Enabled bindings drive
idempotent reconciliation, verification, deployment ordering, and secret
delivery to exactly the consumer and provider nodes.

Declare `service_endpoint` when another plugin needs a TCP connection to this
service. `container_port` is used on the local Compose network;
`published_port` is required only for cross-node consumers and must match a
non-loopback Compose publication. Consumers declare `integrations` with their
own environment output names. Required integrations must also appear in
`depends_on`; optional integrations may emit an enable flag, host and port,
`host:port`, or a URL and compile to disabled/empty values when the provider is
off. The watchdog uses the internal endpoint from active consumer containers.
Compose health checks remain the canonical signal for the service process.

Secret tiers are `user`, `generated`, `bootstrapped`, and `derived`. Generated
values are created automatically; user values are write-only operator inputs;
bootstrapped values derive from the owner identity during first start. Runtime
bundles contain only environment references and manifest credentials owned by
services on that node.

Settings are owned by this manifest. Their defaults require no entry in
`config.yaml`; operator overrides are stored under
`service_settings.<service>.<setting>`. A boolean setting named `enabled`
automatically controls service discovery, Compose generation, secrets,
routes, images, verification, and the management UI. `{setting.<key>}`
templates resolve only settings declared by the same manifest.

`enabled_when` and route variants can reference another declared service
setting with `setting: <service>.<key>`. Catalog loading rejects unknown
services, unknown setting keys, and comparison values that do not match the
setting type:

```yaml
enabled_when:
  - setting: media-library.server
    one_of: [jellyfin, both]
```

Every plugin receives built-in CPU, memory, availability, and heal-restart
metrics. `status` metrics and resource tables are allow-listed by the manifest,
so plugin-returned fields that were not declared never cross the controller API.
The same contract appears automatically in both operator interfaces:

```bash
homelab-toolkit services inspect my-service
homelab-toolkit services set my-service interval-minutes 30
homelab-toolkit services run my-service reconcile --yes
```

Settings are revision checked and reconciled as controller jobs. Actions must be
declared in YAML and implemented by the plugin; arbitrary manifest commands are
never executed.

Every runtime service in `compose.yaml` is placed on the manifest `placement`
by default. Add a `runtimes` entry only when a Compose service has different
placement, execution, or host-resource requirements. For example, an
infra-owned monitoring application can place its agent on every non-primary
node:

```yaml
runtimes:
  my-service-agent:
    placements: ['@non-primary']
    compose_profile: my-service-agents
```

The key must exactly match a service owned by the same `compose.yaml`.
Generation creates a minimal model for each enabled role and rejects unknown
runtime declarations, missing activation profiles, or cross-service runtime
references. `compose_profile` independently activates the placed runtime
without framework changes and must also appear on that Compose service.
Placement selectors may be a machine ID, a machine capability label,
`@primary`, `@non-primary`, or `@all`; capability labels intentionally expand
to every matching enabled node.

Infrastructure providers declare their framework role with `provides`. Provider
roles are unique across the catalog and are resolved by capability rather than
by a fixed service name:

```yaml
provides: [metrics]
```

The built-in provider roles are `ingress` and `metrics`. A replacement must
implement the corresponding Caddy-routing or Prometheus-compatible protocol
contract; catalog validation rejects duplicate providers.

One-shot initialization jobs and optional hardware runtimes are also declared
here so startup and recovery remain plugin-owned:

```yaml
runtimes:
  my-service-init:
    mode: oneshot
  my-service-gpu:
    required_host_paths: [/dev/dri]
```

**`plugin.py`** — behavior (override only what YAML can't express):

```python
from toolkit.services import ServicePlugin

class MyServicePlugin(ServicePlugin):
    service = "my-service"

    def post_start(self, cfg, secrets, *, root=None) -> list[str]:
        """Run after compose-up. Idempotent, non-fatal on error."""
        from toolkit.services.sdk import http_check, wait_for_http
        url = f"http://my-service:8080/health"
        ok, detail = http_check(url)
        return [f"my-service health: {detail}"] if ok else ["my-service: not ready yet"]

    def verify(self, cfg, secrets, vm_ip, root) -> list:
        from toolkit.core.verify.models import VerifyCheck
        from toolkit.services.sdk import http_check

        ok, detail = http_check(f"http://{vm_ip}:8080/health")
        return [VerifyCheck(
            service="my-service",
            check="health",
            passed=ok,
            detail=detail,
        )]

    def supported_actions(self) -> frozenset[str]:
        return frozenset({"reconcile"})

    def execute_action(self, action, cfg, secrets, root) -> list[str]:
        if action != "reconcile":
            raise ValueError("unsupported action")
        # Run a bounded, idempotent reconciliation here.
        return ["Reconciliation completed"]

    def status(self, cfg, secrets, root) -> dict[str, object]:
        return {"queue_depth": 0, "private_token": "never projected"}

    def resources(self, cfg, secrets, root) -> dict[str, list[dict[str, object]]]:
        return {"remotes": [{"name": "primary", "state": "ready"}]}
```

Static metadata such as category, placement, icon, enablement, storage, backups,
management capabilities, metrics, and routes belongs only in `service.yaml`.
Discovery fails when a manifest declares an action, status metric, or resource
without its required plugin implementation, or when a plugin implements an
undeclared action.

### Host Lifecycle Hooks

Host automation belongs under the owning service directory rather than in a
central playbook. Add only the phases the service needs:

```text
ansible/guest.yml          # managed guest bootstrap
ansible/guest-final.yml    # work immediately before final hooks
ansible/manager.yml        # standalone manager wrapper phase
ansible/security.yml       # standalone security wrapper phase
ansible/sync.yml           # explicit operator sync phase
ansible/recovery.yml       # recovery after Compose reconciliation
ansible/pre-deploy.yml     # before Compose rollout
ansible/post-deploy.yml    # after Compose rollout
ansible/storage.yml        # service-owned storage preparation
```

Each file is an Ansible task list. The generator projects enabled service hooks
into typed phase variables (`service_*_task_files`); core playbooks only loop
over those variables. Keep placement and cleanup conditions inside the hook,
and make every task idempotent. `guest_task_order` controls ordering within the
guest phase without changing Compose startup priority; `recovery_task_order`
does the same for recovery hooks. Do not add service role names or service IDs
to core playbooks.
For behavior that must run around the plugin's own Compose wave, override
`before_runtime_start(context, services)` or
`after_runtime_start(context, services)`. The context intentionally exposes a
bounded set of Compose, host-command, health, retry, recovery, state, and
progress methods; plugins must not import or mutate the deploy runner.

**Shared primitives** — import from `toolkit.services.sdk` rather than
re-implementing container/HTTP exec:

- **Leaf / cfg-free** submodules (`http`, `docker`, `registry`): stdlib + httpx
  helpers with explicit host/port params (`http_check`, `wait_for_http`,
  `docker_exec`, `container_exists`, …).
- **Cfg-aware** submodules (`authelia`, `postgres`, `redis`, `monitoring`,
  `vaultwarden`, `adguard`, `caddy`, `wazuh`, `crowdsec`, `ldap`): take a
  `Config` and centralise URLs/maps for essential infrastructure services.
- **Multi-VM execution** (`docker_exec_on_vm`, `ssh_on_vm`, `docker_curl`, …):
  re-exported from the internal `_vmexec` submodule — still imported as
  `from toolkit.services.sdk import docker_exec_on_vm`.

**Optional tests** — keep implementation tests under
`tests/services/<name>/` (or `tests/services/_cross/<area>/` for concrete
multi-service orchestration). The framework gate collects only
`tests/framework/`; deployment `post_start`, `verify`, and `heal` hooks remain
the runtime verification contract for the plugin.

Always use the package import surface (`from toolkit.services.sdk import …`);
do not import `toolkit.services.sdk._vmexec` from plugins.

**`bootstrap.py` (optional)** — when `post_start()` setup exceeds ~50 lines,
co-locate it in `toolkit/services/<name>/bootstrap.py` next to `plugin.py`.
The plugin's `post_start()` then becomes a thin caller:

```python
    def post_start(self, cfg, secrets, *, root=None) -> list[str]:
        import importlib
        return importlib.import_module("toolkit.services.my-service.bootstrap").bootstrap_my_service(
            cfg, secrets, root=root
        )
```

Kebab-case service names (e.g. `immich-server`) aren't valid Python
identifiers, so load the bootstrap module via `importlib.import_module`, not a
plain `import`.

### 2. Declare environment values

Add environment values to `variables:` in `service.yaml`. Values can reference
service-owned settings, typed platform configuration, and another service's
resolved node or address. They are projected only to nodes running the
application:

```yaml
variables:
  MY_SERVICE_HOST: my-service.{config.domain}
  MY_SERVICE_WORKERS: '{setting.workers}'
  DATABASE_HOST: '{service.postgres.address}'
```

### 3. Declare host bind sources

Every Compose bind source other than the platform-wide `INSTALL_ROOT` owns a
canonical relative path in one service manifest. Reference the same key and
path as the Compose fallback:

```yaml
host_sources:
  MY_SERVICE_DATA_SOURCE:
    path: data/my-service
```

```yaml
services:
  my-service:
    volumes:
      - ${MY_SERVICE_DATA_SOURCE:-./data/my-service}:/var/lib/my-service
```

Generation resolves the path below the configured install root and projects it
only to the machine running its owner. Catalog loading rejects missing or
duplicate owners, unused declarations, fallback drift, unsafe relative paths,
and consumers placed on a different machine from the owner.

A shared path belongs to the service that defines the shared resource, not to
each consumer. Shared consumers must use the same placement. Conditional path
layouts use typed variants rather than core Python:

```yaml
host_sources:
  MEDIA_LIBRARY_ROOT:
    path: media
    variants:
      - when:
          setting: media-cache.enabled
          equals: true
        path: media/library
```

Predicates use the same validated config or service-setting contract as
`enabled_when`. Only one variant may match; otherwise generation fails closed.

### 4. Declare generated artifacts

Generated runtime configuration belongs to the service that consumes it. List
every file or symlink, keep its Jinja templates in the service directory, and
implement `generate_artifacts()` in `plugin.py`:

```yaml
generated_artifacts:
  - path: generated/my-service/config.yml
    sensitive: true
  - path: generated/my-service/healthcheck.sh
    executable: true
```

```python
class MyServicePlugin(ServicePlugin):
    service = "my-service"

    def generate_artifacts(self, context: ArtifactGenerationContext) -> None:
        context.render_template(
            "generated/my-service/config.yml",
            "config.yml.j2",
            {"domain": context.config.domain, "token": context.secrets["MY_SERVICE_API_KEY"]},
        )
        context.write_text("generated/my-service/healthcheck.sh", "#!/bin/sh\nexec my-service check\n")
```

The context is restricted to declared paths, uses atomic idempotent writes,
applies `0600` to sensitive files and `0500` to executable files, preserves
declared storage ownership during root-side repair, and fails if the plugin
does not produce its complete contract. Catalog loading rejects duplicate
artifact owners and generated Compose sources without an owner. Before each
Compose startup, the guest discovers active owners and repairs missing or
wrong-type artifacts through the same generators.

### 5. Declare storage and consistent backups

Every writable Compose mount must have a matching `data_specs` entry. Set
`snapshot: true` only for files that are safe to copy while the service is
running. Database services should opt their live store out and declare a
portable `backup_exports` artifact in the same manifest:

```yaml
stateful: true
data_specs:
  - name: my-database
    source_env: MY_DATABASE_SOURCE
    target: /var/lib/my-service
    size_estimate_gb: 5
    snapshot: false

backup_exports:
  - artifact: my-service.sqlite.gz
    strategy: sqlite
    data_spec: my-database
    database_path: db.sqlite
```

The SQLite strategy uses the online Backup API, runs an integrity check, and
compresses the result without stopping the service. For a database with a
native dump tool, use a structured container command; the framework streams
and compresses stdout:

```yaml
backup_exports:
  - artifact: my-service.sql.gz
    strategy: container
    command: [pg_dumpall, -U, app]
```

Artifact names must begin with the service name. Catalog loading rejects
unknown containers, unsafe paths, missing data assets, and ambiguous strategy
fields before generation. Empty or failed exports block the Kopia snapshot.

### 6. Override settings when needed

Manifest defaults require no configuration. To override them directly:

```yaml
service_settings:
  my-service:
    enabled: true
    workers: 4
```

The Web UI generates the same validated overrides and queues reconciliation
when a changed setting requires deployment. Reconciliation includes the
owner's category nodes and any cross-category service whose enablement or route
variant references that owner's settings, so declarative dependencies remain
correct when services are placed on different machines.

### 7. Deploy

```bash
homelab-toolkit generate
homelab-toolkit deploy all --yes
```

Or deploy just the new service:

```bash
homelab-toolkit services deploy my-service
```

## Update a Service

- **Change routes/secrets/memory_tier**: edit `service.yaml`, then `generate` + `services deploy <name>`.
- **Change the Compose application**: edit `compose.yaml`, then `generate` + deploy.
- **Change post_start/verify behavior**: edit `plugin.py`, sync to guests, `deploy recover`.

## Remove a Service

1. Delete the `toolkit/services/<name>/` directory.
2. Remove its optional configuration from `config.yaml`.
3. Run `generate` + `deploy all`.

## Plugin SDK

Plugins import shared utilities from `toolkit.services.sdk` (a package, not a
single `sdk.py` file). Submodules group helpers by concern (`http`, `docker`,
`authelia`, `postgres`, `redis`, `monitoring`, `vaultwarden`, `adguard`,
`caddy`, `wazuh`, `crowdsec`, `ldap`, `registry`); multi-VM helpers are
re-exported from `_vmexec`. The import style is unchanged:

```python
from toolkit.services.sdk import (
    http_check,           # (url, expected_status, headers, timeout) -> (ok, detail)
    docker_exec,          # (service, command, vm_ip, root, timeout, user) -> (rc, output)
    docker_exec_on_vm,    # cfg-aware remote container exec
    ssh_on_vm,            # cfg-aware SSH on a guest LXC
    docker_health_status, # (container, vm_ip, root) -> (state, health)
    container_exists,     # (name, vm_ip, root) -> bool
    resolve_service_url,  # (service, port, fallback_host) -> str
    http_health_check,    # (checks: list[(name, url)]) -> list[str]
    basic_auth_header,    # (username, password) -> str
    wait_for_http,        # (url, timeout, interval) -> bool
    authelia_oidc_issuer, # (cfg) -> issuer URL for OIDC checks
)
```

Leaf modules (`http`, `docker`) avoid `toolkit.*` imports; cfg-aware modules
may import core config helpers. Import only from `toolkit.services.sdk` in
plugins to avoid circular dependencies.

## Essential services

Services marked `essential: true` in `service.yaml` are protected
infrastructure:

`authelia`, `postgres`, `redis`, `lldap`, `caddy`, `adguard`, `prometheus`,
`loki`, `vaultwarden`, `registry-mirror`, `wazuh-indexer`, `wazuh-dashboard`,
`crowdsec`

Contract:

- Deploy treats them as **non-removable** (not dropped when trimming or reconciling).
- Staggered compose starts them in **early waves** on infra.
- Watchdog/heal use a **careful restart policy** — other services depend on them.
- Dependent plugins should use the matching **`toolkit.services.sdk`** submodule
  (`authelia`, `postgres`, `redis`, `ldap`, `monitoring`, `vaultwarden`, …)
  instead of ad-hoc URLs or duplicate connection logic.

Do not delete or edit these plugins without understanding the dependency chain.
Set `essential: true` only for true platform roots — not for ordinary apps.
