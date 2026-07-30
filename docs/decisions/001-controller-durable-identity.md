# ADR-001: Durable Controller Identity Operations

## Status

Accepted

## Date

2026-07-10

## Context

Identity changes affect LLDAP, email, Vaultwarden, and services on more than one VM. The Web UI must not receive directory administration credentials or direct Docker/SSH authority. Controller jobs also survive process restarts, so their persisted requests and replay behavior are security boundaries.

The design must:

- reject passwords at the job contract;
- keep invite recipient data out of cleartext SQLite records and API job readback;
- prevent changes to the built-in `admin` and `ldap-bind` identities;
- serialize mutations and reject cancellation after execution starts;
- distinguish complete, pending, warning, and partial outcomes;
- make worker replay converge without issuing a different activation link;
- keep service-specific failures visible without persisting free-form errors or recipient PII.

## Decision

All identity mutations run as durable controller jobs under the global mutation lease. The UI uses typed controller resources and never imports directory, Docker, SSH, or secret-management implementations.

`InviteUserCommand` is accepted at the controller boundary, then its normalized email and display name are encrypted with AES-256-GCM before SQLite insertion. The persisted job contains an internal `invite_sealed` command. Idempotency fingerprints use HMAC-SHA-256 instead of an unkeyed digest. The controller payload key and its authenticated startup check live in a controller-only volume separate from the SQLite volume. Missing, permissive, symlinked, or incorrect key material fails closed.

Identity results contain only strict step keys and statuses. `PARTIAL_FAILURE` is a distinct terminal job state and is surfaced separately by SSE. Human-readable adapter messages remain transient CLI output; they are not copied into controller results, events, or audit details.

Invitation delivery is at-least-once. A job ID selects one Redis-cached activation token and one deterministic RFC message ID. Redis creates the invite and delivery cache atomically. A crash after SMTP acceptance can produce an identical duplicate, but cannot invalidate the earlier email with a new token.

Directory group mutations create required managed groups and verify the exact postcondition before downstream provisioning. Replayed directory deletion treats an already-absent target as converged; an absent target on the first attempt remains `NOT_FOUND`.

The current delete command is deliberately named `delete_directory_identity`. Full user offboarding is a separate future workflow because it must revoke invites and downstream sessions before deleting LLDAP, and permanent Vaultwarden deletion requires an explicit data-disposition decision.

## Alternatives Considered

### Persist cleartext in an owner-only SQLite database

Rejected. File permissions reduce access but do not protect database-only backups, diagnostics, or accidental job readback. Low-entropy email fields also make unkeyed request hashes guessable.

### Give the UI direct LLDAP or service credentials

Rejected. This would restore the host-authority boundary removed from the production UI and make browser routes responsible for distributed mutation recovery.

### Mark partial adapter failure as `SUCCEEDED`

Rejected. Operators and automation would receive a green terminal state even when required service provisioning failed.

### Claim exactly-once email delivery

Rejected. SMTP does not provide a transaction that can atomically commit remote acceptance and local job state. Stable at-least-once delivery is the truthful guarantee.

## Consequences

- The payload key must be backed up and restored separately from `controller.db`.
- Losing the key makes queued encrypted invites unrecoverable; the controller refuses to start with a mismatched key.
- Existing controller databases without the payload key are intentionally unsupported and must be recreated.
- Service adapters must be observe-before-create and return typed reports.
- UI and CLI consumers must treat `PARTIAL_FAILURE` as terminal and show its distinct status.
- Full offboarding still requires a durable multi-service state machine; directory deletion must not be presented as complete offboarding.
