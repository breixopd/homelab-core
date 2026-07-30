# ADR-002: Manifest-Driven Service Management Capabilities

## Status

Accepted

## Date

2026-07-11

## Context

Every managed service already has a flat folder containing `service.yaml`, `compose.yaml`, and optional Python behavior. Deployment, verification, secrets, health recovery, CLI commands, and Web UI settings still expose some service-specific behavior through separate framework modules. Media cache and music sync demonstrate the problem: both are service plugins, but their operator settings and actions require knowledge outside their folders.

The management surface must support future services without framework edits while preserving these boundaries:

- browsers cannot submit commands, PromQL, code, or plugin HTML;
- secrets cannot appear in manifests, controller read models, durable job payloads, or metrics;
- service actions must be allow-listed, auditable, cancellable where possible, and routed through the controller;
- configuration changes must retain revision checks and full Pydantic validation;
- status and metric responses must be bounded and typed;
- disabled services must remain discoverable for configuration without running hooks or health checks.

## Decision

`toolkit/services/<name>/service.yaml` is the sole service metadata source. A service manifest can declare strict configuration or service-setting predicates and a `management` block containing settings, actions, metrics, and read-only resource tables.

Settings declare stable keys, labels, input types, bounds, choices, and defaults. The controller resolves current values from `service_settings`, and applies changes through the revisioned desired-state resource. Manifests cannot declare secret fields; credentials continue through write-only secret resources.

Actions declare stable IDs and display metadata only. YAML cannot contain commands. Built-in lifecycle actions map to controller-owned container operations. Service-specific actions execute only when the service's Python plugin implements the declared ID. Parameterless actions use durable jobs. Actions requiring sensitive input use dedicated write-only controller resources and never enter the durable job store.

Metrics declare stable IDs and either a plugin status field or a trusted manifest PromQL expression. Clients request metric IDs, never expressions. The controller bounds query count, response size, execution time, numeric ranges, and cache duration. Every enabled service also receives generic container health, CPU, memory, restart, and availability metrics when telemetry exists.

Resource tables declare stable collection and column IDs. A plugin returns candidate rows; the controller caps the collection at 100 rows, removes undeclared columns and control characters, redacts sensitive assignments, and limits cell length. Resource tables are display-only. Mutations use explicit typed controller resources so manifests cannot become a generic CRUD or credential language.

The Web UI and `services inspect`, `services set`, and `services run` CLI commands render shared controls from typed controller read models. Plugins cannot contribute HTML, JavaScript, CSS, templates, routes, browser-executable content, or shell commands. A service can implement typed status and action methods in `plugin.py` using the service SDK.

## Alternatives Considered

### One custom Web UI page per service

Rejected. It duplicates forms, authorization, error handling, progress reporting, and metrics logic, and makes each service addition a framework change.

### Allow manifests to contain shell commands

Rejected. It turns trusted data into an execution language, complicates validation, and creates an unsafe privilege boundary.

### Let the browser query Prometheus directly

Rejected. It exposes infrastructure topology and permits arbitrary expensive queries. The controller must own metric selection, limits, caching, and sanitization.

### Make every service action synchronous

Rejected. Long-running actions would lose progress, cancellation, leases, replay behavior, and durable audit records.

## Consequences

- Media-library selection, Jellyfin transcoding, Gluetun VPN policy, qBittorrent networking, Tdarr automation, media cache, and music sync own their management declarations and optional enablement.
- Service addition remains folder-scoped for ordinary services.
- The plugin contract grows typed `status` and `execute_action` methods with conservative defaults.
- Controller resources and UI components can remain generic and tested once.
- Sensitive, complex resources such as media-cache backend credentials use explicit typed resources; media-cache storage is reconciled through the plugin-declared managed-host integration rather than a service-specific CLI.
- Category manifests can focus on deployment grouping until service-owned compose assembly removes that grouping dependency.
