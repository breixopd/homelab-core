# ADR-005: Manifest-Owned Service Secrets

## Status

Accepted

## Date

2026-07-11

## Context

Service credentials were split between central category arrays, Compose
references, plugin bootstrap code, and a small subset of service manifests.
The central lists could omit a required credential, generate credentials for a
disabled service, or disagree with the plugin contract.

Guest synchronization also replaced the generated role environment with a
raw-secret-only file. That removed non-secret Compose inputs and derived
bootstrap passwords, while an exclusion list incorrectly removed the Caddy DNS
token and Vaultwarden admin token that runtime containers require.

## Decision

Every runtime credential is declared in the owning service's strict
`required_secrets` manifest block. The accepted tiers are `user`, `generated`,
`bootstrapped`, and `derived`. Configuration predicates decide whether a
service's credentials are required.

Only credentials that do not belong to a runtime service remain fixed
infrastructure inputs: Proxmox API and SSH identities, age-key backup
attestation, Cloudflare DNS credentials, and the deploy-notification endpoint.

The generated role `.env` is the source for guest bundles. Bundle compilation
selects exact Compose references, manifest-owned hook/bootstrap credentials,
and runner keys such as `COMPOSE_PROFILES`. Controller-only names are removed.
The deployable bundle, not the broader controller env, is validated against the
role Compose model.

## Consequences

- Adding or removing a service changes its secret inventory in the same folder.
- Disabled services no longer request credentials or appear in secret forms.
- Bootstrap-derived passwords are reported as derived rather than generated or
  user-configurable.
- Hooks receive service credentials even when those names are not container
  environment references.
- Guest bundles preserve all required runtime configuration while excluding
  Proxmox and controller Cloudflare credential names.
- Repository tests reject sensitive Compose references without a declared
  manifest or fixed-infrastructure owner.
