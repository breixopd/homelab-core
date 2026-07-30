"""Gitea post-deploy bootstrap: owner SSO user and admin API token."""

from __future__ import annotations

import re
import shlex
import time
from pathlib import Path
from typing import TYPE_CHECKING

from toolkit.core.identity.lldap_client import user_id_from_email
from toolkit.core.ops.automation import docker_exec
from toolkit.core.secrets.secrets import merge_secret_values

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


# Gitea 1.27 emits a colon; older releases used an ellipsis.  Keep the
# protocol tolerant of that presentation detail while still accepting only a
# complete, single-line token response.
_ACCESS_TOKEN_LINE = re.compile(
    r"^Access token was successfully created(?:\.\.\.|:)\s+(\S+)\s*$",
    re.MULTILINE,
)


def _access_token_from_output(output: str) -> str:
    """Extract the token without ever surfacing Gitea's random-password line."""
    match = _ACCESS_TOKEN_LINE.search(output or "")
    return match.group(1) if match is not None else ""


def _safe_admin_error(output: str) -> str:
    """Never surface the random local password emitted by Gitea's CLI."""
    lines = [line for line in (output or "").splitlines() if not line.startswith("generated random password is")]
    return "\n".join(lines).strip()[:120]


def _owner_username_from_list(output: str, owner_email: str, preferred: str) -> str | None:
    """Resolve the existing owner by username first, then by email.

    Gitea keeps the local username independent from the identity provider. A
    prior SSO bootstrap can therefore have a different username while still
    owning the configured email address; treating that account as new causes a
    duplicate-email failure on every recovery.
    """
    email = owner_email.strip().lower()
    for line in (output or "").splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[0].lower() == "id":
            continue
        username, address = fields[1], fields[2].lower()
        if username == preferred:
            return username
        if address == email:
            return username
    return None


def bootstrap_gitea_admin(
    config: Config,
    secrets: dict[str, str],
    *,
    root: Path | None = None,
) -> list[str]:
    """Ensure the canonical owner SSO user and a technical API token exist."""
    logs: list[str] = []
    owner_email = config.email.strip().lower()
    try:
        admin_user = user_id_from_email(owner_email)
    except ValueError:
        logs.append("Gitea: config.email must be a valid owner email for SSO administration")
        return logs

    list_rc, list_out = docker_exec(
        "gitea",
        ["gitea", "admin", "user", "list"],
        environment={"GITEA__security__INSTALL_LOCK": "true"},
    )
    existing_user = _owner_username_from_list(list_out, owner_email, admin_user) if list_rc == 0 else None
    user_exists = existing_user is not None
    effective_user = existing_user or admin_user
    token = secrets.get("GITEA_ADMIN_TOKEN", "")
    if user_exists:
        logs.append(f"Gitea: owner admin {effective_user} exists")
    else:
        create_rc, create_out = docker_exec(
            "gitea",
            [
                "gitea",
                "admin",
                "user",
                "create",
                "--username",
                effective_user,
                "--email",
                owner_email,
                "--admin",
                "--random-password",
                "--access-token",
                "--access-token-name",
                "homelab-toolkit-bootstrap",
                "--access-token-scopes",
                "all",
                "--must-change-password=false",
            ],
            environment={"GITEA__security__INSTALL_LOCK": "true"},
        )
        if create_rc == 0:
            token = _access_token_from_output(create_out)
            logs.append(f"Gitea: created owner admin {admin_user}")
            if token:
                logs.append("Gitea: generated admin access token")
        else:
            logs.append(f"Gitea: owner admin create failed ({_safe_admin_error(create_out) or 'unknown error'})")
            return logs

    if not token:
        token_rc, token_out = docker_exec(
            "gitea",
            [
                "gitea",
                "admin",
                "user",
                "generate-access-token",
                "--username",
                effective_user,
                "--token-name",
                f"homelab-toolkit-{int(time.time())}",
                "--scopes",
                "all",
            ],
            environment={"GITEA__security__INSTALL_LOCK": "true"},
        )
        token = _access_token_from_output(token_out) if token_rc == 0 else ""
        if token:
            logs.append("Gitea: generated admin access token")
        else:
            logs.append(f"Gitea: admin token generation failed ({_safe_admin_error(token_out) or f'rc={token_rc}'})")

    if root is not None:
        persist = {}
        if token and token != secrets.get("GITEA_ADMIN_TOKEN", ""):
            persist["GITEA_ADMIN_TOKEN"] = token
        logs.extend(merge_secret_values(root, persist))
    elif token:
        logs.append("Gitea: pass root= to persist GITEA_ADMIN_TOKEN")

    return logs


def reconcile_gitea_runtime_credentials(config: Config, root: Path) -> list[str]:
    """Create a controller-owned Gitea API token after the guest is ready.

    Guest hook execution intentionally uses a scoped secret bundle and cannot
    write back to the controller's encrypted store. This narrow protocol runs
    token generation on the Gitea guest, returns only the token line, and
    persists it through the controller secret store. Existing credentials are
    Existing credentials are validated against the live admin API; only an
    invalid credential is rotated, so repeated deploys do not create an
    unbounded token trail.
    """
    from toolkit.core.ansible.ansible_ssh import sanitize_probe_output, ssh_run_on_vm
    from toolkit.core.config.storage import secrets_path
    from toolkit.core.manifest.placement import service_address
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    stored = load_secrets_plaintext(secrets_path(root))
    stored_token = stored.get("GITEA_ADMIN_TOKEN", "").strip()

    try:
        admin_user = user_id_from_email(config.email.strip().lower())
    except ValueError:
        return ["Hook error: Gitea owner email is invalid"]
    owner_email = config.email.strip().lower()
    logs: list[str] = []
    if stored_token:
        from toolkit.core.net.curl_config import render_curl_config

        try:
            validation = render_curl_config(
                "http://localhost:3000/api/v1/admin/users",
                headers={"Authorization": f"token {stored_token}"},
                timeout=15,
            )
            validate_rc, _validate_out, _validate_err = ssh_run_on_vm(
                config,
                service_address(config, "gitea"),
                "docker exec -i gitea curl --disable --config -",
                root=root,
                timeout=30,
                retries=2,
                stdin=validation,
            )
        except ValueError:
            validate_rc, _validate_err = 1, "stored token has invalid characters"
        if validate_rc == 0:
            return ["Gitea: controller admin token already present"]
        # Do not include guest diagnostics here: an HTTP client may echo the
        # Authorization header in an error, which would leak the old token.
        logs.append("Gitea: controller admin token invalid; rotating")
    username = shlex.quote(admin_user)
    email = shlex.quote(owner_email)
    # Keep the random password and diagnostics on the guest. The only stdout
    # allowed across SSH is the structured token line.
    command = (
        "set -eu; "
        f"username={username}; "
        f"owner_email={email}; "
        "existing=$(docker exec gitea gitea admin user list 2>/dev/null | "
        "awk -v email=\"$owner_email\" 'NR > 1 && $3 == email {print $2; exit}'); "
        '[ -z "$existing" ] || username="$existing"; '
        "if ! docker exec gitea gitea admin user list 2>/dev/null | "
        "awk -v username=\"$username\" 'NR > 1 && $2 == username {found=1} END {exit !found}'; then "
        f'docker exec gitea gitea admin user create --username "$username" --email {email} '
        "--admin --random-password --must-change-password=false >/dev/null 2>&1; "
        "fi; "
        'token_name="homelab-toolkit-$(date +%s)"; '
        'docker exec gitea gitea admin user generate-access-token --username "$username" '
        '--token-name "$token_name" --scopes all 2>/dev/null | '
        "grep -E '^Access token was successfully created'"
    )
    rc, out, err = ssh_run_on_vm(
        config,
        service_address(config, "gitea"),
        command,
        root=root,
        timeout=90,
        retries=3,
    )
    if rc != 0:
        detail = sanitize_probe_output(err, max_len=100) or f"exit {rc}"
        return logs + [f"Hook error: Gitea runtime credential reconciliation failed ({detail})"]
    token = _access_token_from_output(out)
    if not token or len(token) > 512 or any(ch.isspace() for ch in token):
        return logs + ["Hook error: Gitea runtime credential response rejected"]

    from toolkit.core.secrets.secrets import merge_secret_values

    logs.extend(merge_secret_values(root, {"GITEA_ADMIN_TOKEN": token}))
    if logs:
        logs.append("Gitea: controller admin token provisioned")
    else:
        logs.append("Gitea: controller admin token already current")
    return logs
