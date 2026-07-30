# Security policy

## Supported versions

Security fixes are applied to the latest release and the default branch. Older
development snapshots are not supported.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting for this repository. Do not open a
public issue containing exploit details, credentials, deployment configuration,
IP addresses, or logs with secrets.

Include the affected version or commit, the trust boundary involved, a minimal
reproduction, and the expected impact. Reports are validated before a fix or
advisory is published.

## Deployment secrets

`config.yaml`, generated Compose files, encrypted-secret identities, runtime
state, logs, backups, and SSH material are operator-owned local files and must
not be committed. If a credential is committed, revoke it at the provider first;
removing the current file does not remove it from Git history.
