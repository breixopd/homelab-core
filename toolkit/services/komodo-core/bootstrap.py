"""Komodo Core / Periphery bootstrap and heal helpers."""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT, secrets_path

KOMODO_SERVICE_USER_ID = "homelab-toolkit"
KOMODO_API_KEY_NAME = "homelab-toolkit"
_RUNTIME_INPUT_KEYS = frozenset(
    {
        "KOMODO_INIT_ADMIN_PASSWORD",
        "KOMODO_ONBOARDING_SEED",
        "KOMODO_API_KEY",
        "KOMODO_API_SECRET",
    }
)
_RUNTIME_OUTPUT_KEYS = frozenset({"KOMODO_API_KEY", "KOMODO_API_SECRET"})


def derive_komodo_onboarding_key(seed: str) -> str:
    """Derive the v2 Periphery onboarding private-key format from secret seed material."""
    if len(seed) < 16:
        return ""
    return f"O_{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:28]}_O"


def komodo_onboarding_key(root: Path) -> str:
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    secrets = load_secrets_plaintext(secrets_path(root))
    return derive_komodo_onboarding_key(secrets.get("KOMODO_ONBOARDING_SEED", ""))


def _komodo_core_base() -> str:
    """Komodo Core API base URL reachable from the infra LXC controller."""
    return "http://127.0.0.1:9120"


def _komodo_post(
    endpoint: str, payload: dict, *, jwt: str = "", api_key: str = "", api_secret: str = ""
) -> dict | list | None:
    """POST to Komodo Core without placing credentials in process arguments."""
    from toolkit.core.ops.automation import docker_curl

    headers = {"Content-Type": "application/json"}
    if jwt:
        headers["Authorization"] = jwt
    if api_key:
        headers["X-Api-Key"] = api_key
    if api_secret:
        headers["X-Api-Secret"] = api_secret

    rc, out = docker_curl(
        "komodo-core",
        f"{_komodo_core_base()}{endpoint}",
        method="POST",
        headers=headers,
        body=json.dumps(payload, separators=(",", ":")),
        timeout=30,
    )
    if rc != 0 or not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return None


def _komodo_login(admin_password: str) -> str | None:
    """Authenticate against Komodo Core as the local admin; return JWT."""
    resp = _komodo_post(
        "/auth/login",
        {"type": "LoginLocalUser", "params": {"username": "admin", "password": admin_password}},
    )
    if not isinstance(resp, dict) or resp.get("type") != "Jwt":
        return None
    data = resp.get("data")
    if not isinstance(data, dict):
        return None
    return str(data.get("jwt") or "").strip() or None


def _reset_komodo_admin_password(admin_password: str) -> bool:
    """Reconcile the persisted local admin password through Komodo's CLI."""
    if not admin_password:
        return False
    script = 'IFS= read -r password; exec km update user admin password "$password" --yes'
    try:
        proc = subprocess.run(
            ["docker", "exec", "-i", "komodo-core", "sh", "-c", script],
            input=f"{admin_password}\n",
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return proc.returncode == 0


def _persist_komodo_secret(root: Path, key: str, value: str) -> None:
    from toolkit.core.secrets.secrets import load_secrets_plaintext, save_secrets_plaintext

    sp = secrets_path(root)
    if not sp.exists():
        return
    stored = load_secrets_plaintext(sp)
    if stored.get(key) == value:
        return
    stored[key] = value
    save_secrets_plaintext(stored, sp)


def _setup_komodo_api_key_values(
    root: Path,
    secrets: dict[str, str],
    *,
    persist: bool,
) -> tuple[list[str], dict[str, str]]:
    """Provision or validate the service API key from explicit secret values."""
    logs: list[str] = []
    updates: dict[str, str] = {}
    admin_password = secrets.get("KOMODO_INIT_ADMIN_PASSWORD", "")
    if not admin_password:
        logs.append("Komodo: KOMODO_INIT_ADMIN_PASSWORD missing — skip API key bootstrap")
        return logs, updates

    existing_key = secrets.get("KOMODO_API_KEY", "").strip()
    existing_secret = secrets.get("KOMODO_API_SECRET", "").strip()
    if existing_key and existing_secret:
        probe = _komodo_post(
            "/read/ListServers",
            {"type": "ListServers"},
            api_key=existing_key,
            api_secret=existing_secret,
        )
        if probe is not None:
            logs.append("Komodo: API key verified")
            return logs, updates
        logs.append("Komodo: existing API key rejected — re-provisioning")

    jwt = _komodo_login(admin_password)
    if not jwt:
        logs.append("Komodo: admin login failed — skip API key bootstrap (komodo-core still starting?)")
        return logs, updates

    # Komodo v2 exposes typed requests as path-specific endpoints. Resolve the
    # service user's Mongo id first; CreateApiKeyForServiceUser does not accept
    # the human-readable username in ``user_id``.
    service_user_id = ""
    users = _komodo_post("/read/ListUsers", {}, jwt=jwt)
    if isinstance(users, list):
        for user in users:
            if not isinstance(user, dict) or user.get("username") != KOMODO_SERVICE_USER_ID:
                continue
            raw_id = user.get("_id")
            if isinstance(raw_id, dict):
                raw_id = raw_id.get("$oid")
            service_user_id = str(raw_id or user.get("id") or "").strip()
            break

    if not service_user_id:
        created_user = _komodo_post(
            "/write/CreateServiceUser",
            {"username": KOMODO_SERVICE_USER_ID, "description": "Homelab deployment automation"},
            jwt=jwt,
        )
        if isinstance(created_user, dict):
            raw_id = created_user.get("_id")
            if isinstance(raw_id, dict):
                raw_id = raw_id.get("$oid")
            service_user_id = str(raw_id or created_user.get("id") or "").strip()
    if not service_user_id:
        logs.append("Komodo: service-user creation returned no id — skip API key bootstrap")
        return logs, updates

    resp = _komodo_post(
        "/write/CreateApiKeyForServiceUser",
        {"user_id": service_user_id, "name": KOMODO_API_KEY_NAME, "expires": 0},
        jwt=jwt,
    )
    if not isinstance(resp, dict):
        logs.append("Komodo: CreateApiKeyForServiceUser returned no body — skip")
        return logs, updates

    api_key = str(resp.get("key") or resp.get("api_key") or "").strip()
    api_secret = str(resp.get("secret") or resp.get("api_secret") or "").strip()
    if not api_key or not api_secret:
        logs.append(f"Komodo: API key response missing key/secret fields — got {list(resp.keys())}")
        return logs, updates

    updates = {"KOMODO_API_KEY": api_key, "KOMODO_API_SECRET": api_secret}
    if persist:
        for key, value in updates.items():
            _persist_komodo_secret(root, key, value)
        logs.append("Komodo: service-user API key provisioned and persisted to secrets")
    else:
        logs.append("Komodo: service-user API key provisioned")
    return logs, updates


def setup_komodo_api_key(root: Path) -> list[str]:
    """Provision a Komodo service-user API key and persist it to SOPS.

    Logs in as the local admin (KOMODO_INIT_ADMIN_PASSWORD), creates a service user
    named KOMODO_SERVICE_USER_ID if missing, and creates an API key for it. The key
    + secret are written back to the secrets store so fleet onboarding can call the
    Komodo API without manual UI steps. Idempotent: skips when valid key already present.
    """
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    secrets = load_secrets_plaintext(secrets_path(root))
    logs, _updates = _setup_komodo_api_key_values(root, secrets, persist=True)
    return logs


def setup_komodo_onboarding_key(root: Path) -> list[str]:
    """Register the deterministic v2 Periphery onboarding key with Komodo Core."""
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    secrets = load_secrets_plaintext(secrets_path(root))
    return _setup_komodo_onboarding_key_values(secrets)


def _setup_komodo_onboarding_key_values(secrets: dict[str, str]) -> list[str]:
    """Register the onboarding key using explicit controller-provided values."""
    admin_password = secrets.get("KOMODO_INIT_ADMIN_PASSWORD", "")
    private_key = derive_komodo_onboarding_key(secrets.get("KOMODO_ONBOARDING_SEED", ""))
    if not admin_password or not private_key:
        return ["Komodo: onboarding seed or admin password missing — skip onboarding key registration"]
    jwt = _komodo_login(admin_password)
    if not jwt:
        return ["Komodo: admin login failed — skip onboarding key registration"]
    existing = _komodo_post(
        "/read",
        {"type": "ListOnboardingKeys", "params": {}},
        jwt=jwt,
    )
    if isinstance(existing, list) and any(
        isinstance(item, dict) and item.get("name") == KOMODO_API_KEY_NAME for item in existing
    ):
        return ["Komodo: Periphery onboarding key verified"]
    created = _komodo_post(
        "/write",
        {
            "type": "CreateOnboardingKey",
            "params": {
                "name": KOMODO_API_KEY_NAME,
                "expires": 0,
                "private_key": private_key,
                "tags": [],
                "privileged": True,
                "copy_server": "",
                "create_builder": False,
            },
        },
        jwt=jwt,
    )
    if not isinstance(created, dict) or not isinstance(created.get("created"), dict):
        return ["Komodo: onboarding key registration failed"]
    return ["Komodo: Periphery onboarding key registered"]


def _runtime_bootstrap_response(payload: object) -> dict[str, object]:
    """Validate controller input and return a fixed-shape guest bootstrap response."""
    if not isinstance(payload, dict) or set(payload) - _RUNTIME_INPUT_KEYS:
        raise ValueError("invalid runtime credential input")
    secrets = {key: value for key, value in payload.items() if isinstance(key, str) and isinstance(value, str)}
    if len(secrets) != len(payload):
        raise ValueError("runtime credential values must be strings")

    admin_password = secrets.get("KOMODO_INIT_ADMIN_PASSWORD", "")
    if not _reset_komodo_admin_password(admin_password):
        return {
            "ok": False,
            "logs": ["Komodo: local admin password reconciliation failed"],
            "updates": {},
        }

    api_logs, updates = _setup_komodo_api_key_values(Path(DEFAULT_HOMELAB_ROOT), secrets, persist=False)
    api_logs.insert(0, "Komodo: local admin password reconciled")
    onboarding_logs = _setup_komodo_onboarding_key_values(secrets)
    api_ok = bool(updates) or api_logs[-1:] == ["Komodo: API key verified"]
    onboarding_ok = onboarding_logs[-1:] in (
        ["Komodo: Periphery onboarding key verified"],
        ["Komodo: Periphery onboarding key registered"],
    )
    return {"ok": api_ok and onboarding_ok, "logs": [*api_logs, *onboarding_logs], "updates": updates}


def reconcile_komodo_runtime_credentials(cfg, root: Path) -> list[str]:
    """Bootstrap Komodo and persist its generated API key on the controller."""
    from toolkit.core.ansible.ansible_ssh import sanitize_probe_output, ssh_run_on_vm
    from toolkit.core.manifest.placement import service_address
    from toolkit.core.secrets.secrets import load_secrets_plaintext, merge_secret_values

    secrets = load_secrets_plaintext(secrets_path(root))
    payload = {key: secrets.get(key, "") for key in _RUNTIME_INPUT_KEYS}
    command = (
        f"cd {DEFAULT_HOMELAB_ROOT} && "
        f"{DEFAULT_HOMELAB_ROOT}/.venv/bin/python3 -m toolkit.services.komodo-core.bootstrap "
        "--runtime-bootstrap"
    )
    rc, out, err = ssh_run_on_vm(
        cfg,
        service_address(cfg, "komodo-core"),
        command,
        root=root,
        timeout=180,
        retries=3,
        stdin=json.dumps(payload),
    )
    if rc != 0:
        detail = sanitize_probe_output(err, max_len=100) or f"exit {rc}"
        return [f"Hook error: Komodo runtime credential reconciliation failed ({detail})"]
    if len(out) > 65_536:
        return ["Hook error: Komodo runtime credential response rejected"]
    try:
        response = json.loads(out)
    except json.JSONDecodeError:
        return ["Hook error: Komodo runtime credential response rejected"]
    if not isinstance(response, dict):
        return ["Hook error: Komodo runtime credential response rejected"]
    ok = response.get("ok")
    remote_logs = response.get("logs")
    updates = response.get("updates")
    if (
        not isinstance(ok, bool)
        or not isinstance(remote_logs, list)
        or not all(isinstance(line, str) and "\n" not in line and len(line) <= 500 for line in remote_logs)
        or not isinstance(updates, dict)
        or set(updates) - _RUNTIME_OUTPUT_KEYS
        or not all(isinstance(value, str) and value for value in updates.values())
        or (updates and set(updates) != _RUNTIME_OUTPUT_KEYS)
    ):
        return ["Hook error: Komodo runtime credential response rejected"]

    logs = list(remote_logs)
    if not ok:
        logs.append("Hook error: Komodo runtime credential bootstrap incomplete")
        return logs
    if updates:
        merge_secret_values(root, updates)
        logs.append(f"Komodo: persisted {len(updates)} runtime credential(s) to encrypted storage")
    return logs


def _runtime_bootstrap_cli() -> int:
    import sys

    try:
        payload = json.loads(sys.stdin.read(65_537))
        if len(json.dumps(payload)) > 65_536:
            raise ValueError("runtime credential input too large")
        response = _runtime_bootstrap_response(payload)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        print(json.dumps({"error": str(exc)}))
        return 2
    print(json.dumps(response))
    return 0


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser()
    parser.add_argument("--runtime-bootstrap", action="store_true")
    args = parser.parse_args()
    if not args.runtime_bootstrap:
        parser.error("--runtime-bootstrap is required")
    raise SystemExit(_runtime_bootstrap_cli())
