from __future__ import annotations

import importlib
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from toolkit.core.config.config import Config
from toolkit.core.config.storage import secrets_path
from toolkit.core.secrets.secrets import save_secrets_plaintext

KOMODO_BOOTSTRAP = importlib.import_module("toolkit.services.komodo-core.bootstrap")
KOMODO_API_KEY_NAME = KOMODO_BOOTSTRAP.KOMODO_API_KEY_NAME
KOMODO_SERVICE_USER_ID = KOMODO_BOOTSTRAP.KOMODO_SERVICE_USER_ID
_komodo_login = KOMODO_BOOTSTRAP._komodo_login
_komodo_post = KOMODO_BOOTSTRAP._komodo_post
_reset_komodo_admin_password = KOMODO_BOOTSTRAP._reset_komodo_admin_password
derive_komodo_onboarding_key = KOMODO_BOOTSTRAP.derive_komodo_onboarding_key
reconcile_komodo_runtime_credentials = KOMODO_BOOTSTRAP.reconcile_komodo_runtime_credentials
setup_komodo_api_key = KOMODO_BOOTSTRAP.setup_komodo_api_key
setup_komodo_onboarding_key = KOMODO_BOOTSTRAP.setup_komodo_onboarding_key


def _seed_secrets(root: Path, **extra) -> None:
    save_secrets_plaintext(
        {"KOMODO_INIT_ADMIN_PASSWORD": "admin-pass", **extra},
        secrets_path(root),
    )


def test_skips_when_init_admin_password_missing(tmp_path: Path):
    save_secrets_plaintext({}, secrets_path(tmp_path))
    logs = setup_komodo_api_key(tmp_path)
    assert logs == ["Komodo: KOMODO_INIT_ADMIN_PASSWORD missing — skip API key bootstrap"]


def test_runtime_admin_password_reset_uses_stdin_not_process_arguments() -> None:
    completed = MagicMock(returncode=0, stdout="Password updated", stderr="")

    with patch.object(KOMODO_BOOTSTRAP.subprocess, "run", return_value=completed) as run:
        assert _reset_komodo_admin_password("managed-admin-password")

    args, kwargs = run.call_args
    assert "managed-admin-password" not in repr(args)
    assert kwargs["input"] == "managed-admin-password\n"


def test_komodo_post_keeps_authentication_and_body_off_process_arguments() -> None:
    with patch(
        "toolkit.core.ops.automation.docker_exec",
        return_value=(0, '{"type":"Jwt","data":{"jwt":"token"}}'),
    ) as execute:
        response = _komodo_post(
            "/auth/login",
            {"password": "managed-admin-password"},
            api_key="managed-api-key",
            api_secret="managed-api-secret",
        )

    assert response == {"type": "Jwt", "data": {"jwt": "token"}}
    assert execute.call_args.args == ("komodo-core", ["curl", "--disable", "--config", "-"])
    config = execute.call_args.kwargs["stdin"]
    assert "managed-admin-password" not in repr(execute.call_args.args)
    assert "managed-api-key" not in repr(execute.call_args.args)
    assert "managed-api-secret" not in repr(execute.call_args.args)
    assert "managed-admin-password" in config
    assert "managed-api-key" in config
    assert "managed-api-secret" in config


def test_v2_local_login_uses_login_endpoint_and_nested_jwt() -> None:
    response = {"type": "Jwt", "data": {"jwt": "jwt-token"}}

    with patch.object(KOMODO_BOOTSTRAP, "_komodo_post", return_value=response) as post:
        assert _komodo_login("managed-admin-password") == "jwt-token"

    assert post.call_args.args == (
        "/auth/login",
        {
            "type": "LoginLocalUser",
            "params": {"username": "admin", "password": "managed-admin-password"},
        },
    )


def test_fast_path_existing_key_validates(tmp_path: Path):
    _seed_secrets(tmp_path, KOMODO_API_KEY="k", KOMODO_API_SECRET="s")
    # /read/ListServers with valid key returns a list -> fast path.
    with patch.object(KOMODO_BOOTSTRAP, "_komodo_post", return_value=[{"name": "node-1", "id": "srv-1"}]) as post:
        logs = setup_komodo_api_key(tmp_path)
    assert logs == ["Komodo: API key verified"]
    # Should only have probed /read/ListServers, no login.
    assert post.call_count == 1
    endpoint = post.call_args.args[0]
    assert endpoint == "/read/ListServers"


def test_reprovisions_when_existing_key_rejected(tmp_path: Path):
    _seed_secrets(tmp_path, KOMODO_API_KEY="stale", KOMODO_API_SECRET="stale")

    # First /read call returns None (rejected). Login returns jwt. The v2
    # bootstrap then lists users, creates the service user, and creates a key.
    responses = iter(
        [
            None,  # probe /read/ListServers (rejected)
            {"type": "Jwt", "data": {"jwt": "jwt-token"}},  # login
            [],  # ListUsers
            {"_id": {"$oid": "su-1"}},  # CreateServiceUser
            {"key": "new-key", "secret": "new-secret"},  # CreateApiKeyForServiceUser
        ]
    )

    def fake_post(endpoint, payload, **kwargs):
        return next(responses)

    with patch.object(KOMODO_BOOTSTRAP, "_komodo_post", side_effect=fake_post):
        logs = setup_komodo_api_key(tmp_path)

    assert "Komodo: existing API key rejected — re-provisioning" in logs
    assert "Komodo: service-user API key provisioned and persisted to secrets" in logs

    from toolkit.core.secrets.secrets import load_secrets_plaintext

    stored = load_secrets_plaintext(secrets_path(tmp_path))
    assert stored["KOMODO_API_KEY"] == "new-key"
    assert stored["KOMODO_API_SECRET"] == "new-secret"


def test_creates_service_user_and_api_key(tmp_path: Path):
    _seed_secrets(tmp_path)  # no existing key

    responses = iter(
        [
            {"type": "Jwt", "data": {"jwt": "jwt-token"}},  # login
            [],  # ListUsers
            {"_id": {"$oid": "su-1"}},  # CreateServiceUser
            {"key": "k1", "secret": "s1"},  # CreateApiKeyForServiceUser
        ]
    )

    def fake_post(endpoint, payload, **kwargs):
        resp = next(responses)
        if endpoint == "/write/CreateServiceUser":
            assert payload == {
                "username": KOMODO_SERVICE_USER_ID,
                "description": "Homelab deployment automation",
            }
        if endpoint == "/write/CreateApiKeyForServiceUser":
            assert payload == {"user_id": "su-1", "name": KOMODO_API_KEY_NAME, "expires": 0}
        return resp

    with patch.object(KOMODO_BOOTSTRAP, "_komodo_post", side_effect=fake_post):
        logs = setup_komodo_api_key(tmp_path)

    assert "Komodo: service-user API key provisioned and persisted to secrets" in logs

    from toolkit.core.secrets.secrets import load_secrets_plaintext

    stored = load_secrets_plaintext(secrets_path(tmp_path))
    assert stored["KOMODO_API_KEY"] == "k1"
    assert stored["KOMODO_API_SECRET"] == "s1"


def test_login_failure_skips(tmp_path: Path):
    _seed_secrets(tmp_path)

    with patch.object(KOMODO_BOOTSTRAP, "_komodo_post", return_value=None):
        logs = setup_komodo_api_key(tmp_path)

    assert any("admin login failed" in line for line in logs)
    from toolkit.core.secrets.secrets import load_secrets_plaintext

    stored = load_secrets_plaintext(secrets_path(tmp_path))
    assert "KOMODO_API_KEY" not in stored


def test_api_key_response_missing_fields(tmp_path: Path):
    _seed_secrets(tmp_path)

    responses = iter(
        [
            {"type": "Jwt", "data": {"jwt": "jwt"}},  # login
            [],  # ListUsers
            {"_id": {"$oid": "su"}},  # CreateServiceUser
            {"unexpected": "shape"},  # CreateApiKeyForServiceUser
        ]
    )

    with patch.object(KOMODO_BOOTSTRAP, "_komodo_post", side_effect=lambda *_a, **_k: next(responses)):
        logs = setup_komodo_api_key(tmp_path)

    assert any("missing key/secret fields" in line for line in logs)


def test_registers_deterministic_v2_onboarding_key(tmp_path: Path) -> None:
    seed = "seed-material-for-komodo-onboarding"
    _seed_secrets(tmp_path, KOMODO_ONBOARDING_SEED=seed)
    calls: list[dict] = []

    def post(_endpoint, payload, **_kwargs):
        calls.append(payload)
        if payload["type"] == "LoginLocalUser":
            return {"type": "Jwt", "data": {"jwt": "jwt"}}
        if payload["type"] == "ListOnboardingKeys":
            return []
        return {"private_key": "redacted", "created": {"name": KOMODO_API_KEY_NAME}}

    with patch.object(KOMODO_BOOTSTRAP, "_komodo_post", side_effect=post):
        logs = setup_komodo_onboarding_key(tmp_path)

    create = next(payload for payload in calls if payload["type"] == "CreateOnboardingKey")
    assert create["params"] == {
        "name": KOMODO_API_KEY_NAME,
        "expires": 0,
        "private_key": derive_komodo_onboarding_key(seed),
        "tags": [],
        "privileged": True,
        "copy_server": "",
        "create_builder": False,
    }
    assert logs == ["Komodo: Periphery onboarding key registered"]


def test_existing_v2_onboarding_key_is_not_recreated(tmp_path: Path) -> None:
    _seed_secrets(tmp_path, KOMODO_ONBOARDING_SEED="seed-material-for-komodo-onboarding")
    responses = iter([{"type": "Jwt", "data": {"jwt": "jwt"}}, [{"name": KOMODO_API_KEY_NAME}]])
    with patch.object(KOMODO_BOOTSTRAP, "_komodo_post", side_effect=lambda *_args, **_kwargs: next(responses)) as post:
        logs = setup_komodo_onboarding_key(tmp_path)

    assert logs == ["Komodo: Periphery onboarding key verified"]
    assert post.call_count == 2


def test_controller_reconciles_runtime_credentials_over_ssh_stdin(tmp_path: Path) -> None:
    _seed_secrets(
        tmp_path,
        KOMODO_ONBOARDING_SEED="seed-material-for-komodo-onboarding",
        KOMODO_API_KEY="placeholder-key",
        KOMODO_API_SECRET="placeholder-secret",
    )
    cfg = Config()
    response = {
        "ok": True,
        "logs": ["Komodo: service-user API key provisioned", "Komodo: Periphery onboarding key verified"],
        "updates": {"KOMODO_API_KEY": "runtime-key", "KOMODO_API_SECRET": "runtime-secret"},
    }

    with patch(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        return_value=(0, json.dumps(response), ""),
    ) as ssh:
        logs = reconcile_komodo_runtime_credentials(cfg, tmp_path)

    command = ssh.call_args.args[2]
    stdin = ssh.call_args.kwargs["stdin"]
    assert "KOMODO_INIT_ADMIN_PASSWORD" not in command
    assert "admin-pass" not in command
    assert json.loads(stdin)["KOMODO_INIT_ADMIN_PASSWORD"] == "admin-pass"
    assert all("runtime-key" not in line and "runtime-secret" not in line for line in logs)

    from toolkit.core.secrets.secrets import load_secrets_plaintext

    stored = load_secrets_plaintext(secrets_path(tmp_path))
    assert stored["KOMODO_API_KEY"] == "runtime-key"
    assert stored["KOMODO_API_SECRET"] == "runtime-secret"


def test_controller_rejects_unexpected_runtime_secret_names(tmp_path: Path) -> None:
    _seed_secrets(tmp_path, KOMODO_ONBOARDING_SEED="seed-material-for-komodo-onboarding")
    cfg = Config()
    response = {"ok": True, "logs": [], "updates": {"POSTGRES_PASSWORD": "hostile-update"}}

    with patch(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        return_value=(0, json.dumps(response), ""),
    ):
        logs = reconcile_komodo_runtime_credentials(cfg, tmp_path)

    assert logs == ["Hook error: Komodo runtime credential response rejected"]


def test_controller_fails_closed_when_guest_bootstrap_is_incomplete(tmp_path: Path) -> None:
    _seed_secrets(tmp_path, KOMODO_ONBOARDING_SEED="seed-material-for-komodo-onboarding")
    cfg = Config()
    response = {"ok": False, "logs": ["Komodo: admin login failed"], "updates": {}}

    with patch(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        return_value=(0, json.dumps(response), ""),
    ):
        logs = reconcile_komodo_runtime_credentials(cfg, tmp_path)

    assert logs[-1] == "Hook error: Komodo runtime credential bootstrap incomplete"


def test_komodo_runtime_credentials_are_bootstrapped_not_generated() -> None:
    import yaml

    manifest = yaml.safe_load(Path("toolkit/services/komodo-core/service.yaml").read_text())
    tiers = {secret["name"]: secret["tier"] for secret in manifest["required_secrets"]}

    assert tiers["KOMODO_API_KEY"] == "bootstrapped"
    assert tiers["KOMODO_API_SECRET"] == "bootstrapped"
