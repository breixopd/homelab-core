from __future__ import annotations

from pathlib import Path

from toolkit.core.config.config import Config
from toolkit.core.identity.lldap_client import user_id_from_email
from toolkit.services.gitea.bootstrap import bootstrap_gitea_admin


def test_bootstrap_creates_owner_admin_with_random_password_and_access_token(monkeypatch, tmp_path: Path) -> None:
    calls: list[tuple[str, list[str], dict]] = []
    persisted: list[dict[str, str]] = []

    def fake_exec(container: str, command: list[str], **kwargs):
        calls.append((container, command, kwargs))
        if command[-1] == "list":
            return 0, "ID\tUsername\tEmail\n"
        return 0, "generated random password is 'not-for-logs'\nAccess token was successfully created... token-value\n"

    monkeypatch.setattr("toolkit.services.gitea.bootstrap.docker_exec", fake_exec)
    monkeypatch.setattr(
        "toolkit.services.gitea.bootstrap.merge_secret_values",
        lambda _root, values: persisted.append(values) or ["saved"],
    )

    config = Config(domain="example.com", email="owner@example.com")
    logs = bootstrap_gitea_admin(config, {}, root=tmp_path)

    assert logs == [
        f"Gitea: created owner admin {user_id_from_email(config.email)}",
        "Gitea: generated admin access token",
        "saved",
    ]
    create = calls[1][1]
    assert "--password" not in create
    assert "--random-password" in create
    assert "--access-token" in create
    assert user_id_from_email(config.email) in create
    assert all("not-for-logs" not in repr(command) for _container, command, _kwargs in calls)
    assert persisted == [{"GITEA_ADMIN_TOKEN": "token-value"}]


def test_runtime_reconcile_persists_only_validated_token(monkeypatch, tmp_path: Path) -> None:
    from toolkit.services.gitea.bootstrap import reconcile_gitea_runtime_credentials

    config = Config(domain="example.com", email="owner@example.com")
    monkeypatch.setattr(
        "toolkit.core.secrets.secrets.load_secrets_plaintext",
        lambda _path: {},
    )
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        lambda *_args, **_kwargs: (0, "Access token was successfully created... token-value\n", ""),
    )
    monkeypatch.setattr(
        "toolkit.core.manifest.placement.service_address",
        lambda *_args: "10.10.10.12",
    )
    persisted: list[dict[str, str]] = []
    monkeypatch.setattr(
        "toolkit.core.secrets.secrets.merge_secret_values",
        lambda _root, values: persisted.append(values) or ["Secrets: saved GITEA_ADMIN_TOKEN"],
    )

    logs = reconcile_gitea_runtime_credentials(config, tmp_path)

    assert logs == ["Secrets: saved GITEA_ADMIN_TOKEN", "Gitea: controller admin token provisioned"]
    assert persisted == [{"GITEA_ADMIN_TOKEN": "token-value"}]


def test_runtime_reconcile_keeps_stored_token_after_live_admin_validation(monkeypatch, tmp_path: Path) -> None:
    from toolkit.services.gitea.bootstrap import reconcile_gitea_runtime_credentials

    config = Config(domain="example.com", email="owner@example.com")
    monkeypatch.setattr(
        "toolkit.core.secrets.secrets.load_secrets_plaintext",
        lambda _path: {"GITEA_ADMIN_TOKEN": "stale-looking-but-live"},
    )
    calls: list[tuple[str, dict]] = []

    def fake_ssh(*_args, **kwargs):
        calls.append((_args[2], kwargs))
        return 0, "[{}]", ""

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)
    monkeypatch.setattr("toolkit.core.manifest.placement.service_address", lambda *_args: "10.10.10.12")

    logs = reconcile_gitea_runtime_credentials(config, tmp_path)

    assert logs == ["Gitea: controller admin token already present"]
    assert len(calls) == 1
    command, kwargs = calls[0]
    assert "stale-looking-but-live" not in command
    assert "stale-looking-but-live" in kwargs["stdin"]


def test_runtime_reconcile_rotates_invalid_stored_token(monkeypatch, tmp_path: Path) -> None:
    from toolkit.services.gitea.bootstrap import reconcile_gitea_runtime_credentials

    config = Config(domain="example.com", email="owner@example.com")
    monkeypatch.setattr(
        "toolkit.core.secrets.secrets.load_secrets_plaintext",
        lambda _path: {"GITEA_ADMIN_TOKEN": "revoked-token"},
    )
    responses = iter(
        [
            (22, "", "HTTP 401"),
            (0, "Access token was successfully created: replacement-token\n", ""),
        ]
    )
    calls: list[tuple[str, dict]] = []

    def fake_ssh(*_args, **kwargs):
        calls.append((_args[2], kwargs))
        return next(responses)

    monkeypatch.setattr("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", fake_ssh)
    monkeypatch.setattr("toolkit.core.manifest.placement.service_address", lambda *_args: "10.10.10.12")
    persisted: list[dict[str, str]] = []
    monkeypatch.setattr(
        "toolkit.core.secrets.secrets.merge_secret_values",
        lambda _root, values: persisted.append(values) or ["saved"],
    )

    logs = reconcile_gitea_runtime_credentials(config, tmp_path)

    assert logs == [
        "Gitea: controller admin token invalid; rotating",
        "saved",
        "Gitea: controller admin token provisioned",
    ]
    assert persisted == [{"GITEA_ADMIN_TOKEN": "replacement-token"}]
    assert "revoked-token" not in calls[0][0]


def test_access_token_parser_accepts_current_colon_format() -> None:
    from toolkit.services.gitea.bootstrap import _access_token_from_output

    assert _access_token_from_output("Access token was successfully created: token-value\n") == "token-value"


def test_bootstrap_reuses_existing_owner_matched_by_email(monkeypatch, tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def fake_exec(_container: str, command: list[str], **_kwargs):
        calls.append(command)
        if command[-1] == "list":
            return (
                0,
                "ID   Username   Email               IsActive IsAdmin 2FA\n"
                "1    gitadmin   owner@example.com   true     true    false\n",
            )
        return 0, "Access token was successfully created... token-value\n"

    monkeypatch.setattr("toolkit.services.gitea.bootstrap.docker_exec", fake_exec)
    monkeypatch.setattr(
        "toolkit.services.gitea.bootstrap.merge_secret_values",
        lambda _root, values: [f"saved {sorted(values)}"],
    )

    logs = bootstrap_gitea_admin(Config(domain="example.com", email="owner@example.com"), {}, root=tmp_path)

    assert logs == [
        "Gitea: owner admin gitadmin exists",
        "Gitea: generated admin access token",
        "saved ['GITEA_ADMIN_TOKEN']",
    ]
    assert all("create" not in command for command in calls)
    assert "gitadmin" in calls[-1]
