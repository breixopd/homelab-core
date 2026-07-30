from __future__ import annotations

from pathlib import Path
from unittest.mock import ANY, MagicMock

import pytest
from toolkit.controller.contracts import (
    DeleteDirectoryIdentityCommand,
    InviteUserCommand,
    ReprovisionUserCommand,
    SetUserGroupsCommand,
)
from toolkit.controller.identity_api import activate_invite, read_directory_users
from toolkit.controller.read_models import InviteActivationRequest
from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.identity.invite_email import WelcomeDelivery
from toolkit.core.identity.invite_token import InviteActivation, invite_csrf_token
from toolkit.core.identity.lldap_client import LLDAPUser
from toolkit.core.identity.user_provision import ServiceProvisionReport, ServiceProvisionStep


def _root(tmp_path: Path) -> Path:
    (tmp_path / "config.yaml").write_text(
        "domain: test.example.com\nemail: owner@test.example.com\nservices:\n  management: true\n  media: true\n"
    )
    (tmp_path / "secrets.enc.yaml").write_text("placeholder\n")
    return tmp_path


def _request(secrets: dict[str, str], token: str = "opaque-token") -> InviteActivationRequest:
    return InviteActivationRequest(
        token=token,
        activation_csrf=invite_csrf_token(secrets, token),
        origin="https://homelab.test.example.com",
        password="new-password-12",
    )


def _payload() -> dict:
    return {
        "email": "family@test.example.com",
        "user_id": "family",
        "display_name": "Family",
        "groups": ["homelab-media"],
    }


def _secrets() -> dict[str, str]:
    return {
        "INVITE_TOKEN_SECRET": "test-secret-key-for-invite-tokens-0123456789",
        "LLDAP_BIND_PASSWORD": "bind-secret",
    }


def test_activation_consumes_before_exactly_one_password_mutation(tmp_path: Path, monkeypatch) -> None:
    root, secrets, payload = _root(tmp_path), _secrets(), _payload()
    order: list[str] = []
    client = MagicMock()
    client.find_user.return_value = MagicMock(id="family", email="family@test.example.com")
    client.set_password.side_effect = lambda *_args: order.append("password")
    monkeypatch.setattr("toolkit.controller.identity_api.load_secrets_plaintext", lambda _path: secrets)
    monkeypatch.setattr("toolkit.controller.identity_api.peek_invite_token", lambda *_args: payload)
    monkeypatch.setattr("toolkit.controller.identity_api.LLDAPClient", lambda **_kwargs: client)

    def begin(*_args):
        order.append("consume")
        return InviteActivation(state="acquired", payload=payload, activation_id="a" * 24)

    def complete(*_args, succeeded: bool):
        order.append(f"terminal:{succeeded}")
        return True

    monkeypatch.setattr("toolkit.controller.identity_api.begin_invite_activation", begin)
    monkeypatch.setattr("toolkit.controller.identity_api.complete_invite_activation", complete)

    result = activate_invite(root, _request(secrets))

    assert result.outcome == "activated"
    assert order == ["consume", "password", "terminal:True"]
    client.set_password.assert_called_once_with("family", "new-password-12")


def test_failed_password_mutation_records_terminal_failure(tmp_path: Path, monkeypatch) -> None:
    root, secrets, payload = _root(tmp_path), _secrets(), _payload()
    client = MagicMock()
    client.find_user.return_value = MagicMock(id="family", email="family@test.example.com")
    client.set_password.side_effect = RuntimeError("directory-secret must not escape")
    terminal: list[bool] = []
    monkeypatch.setattr("toolkit.controller.identity_api.load_secrets_plaintext", lambda _path: secrets)
    monkeypatch.setattr("toolkit.controller.identity_api.peek_invite_token", lambda *_args: payload)
    monkeypatch.setattr("toolkit.controller.identity_api.LLDAPClient", lambda **_kwargs: client)
    monkeypatch.setattr(
        "toolkit.controller.identity_api.begin_invite_activation",
        lambda *_args: InviteActivation(state="acquired", payload=payload, activation_id="a" * 24),
    )
    monkeypatch.setattr(
        "toolkit.controller.identity_api.complete_invite_activation",
        lambda *_args, succeeded: terminal.append(succeeded) or True,
    )

    result = activate_invite(root, _request(secrets))

    assert result.outcome == "failed"
    assert terminal == [False]
    assert "directory-secret" not in result.model_dump_json()


def test_directory_identity_mismatch_never_consumes_invite(tmp_path: Path, monkeypatch) -> None:
    root, secrets, payload = _root(tmp_path), _secrets(), _payload()
    client = MagicMock()
    client.find_user.return_value = MagicMock(id="different", email="family@test.example.com")
    begin = MagicMock()
    monkeypatch.setattr("toolkit.controller.identity_api.load_secrets_plaintext", lambda _path: secrets)
    monkeypatch.setattr("toolkit.controller.identity_api.peek_invite_token", lambda *_args: payload)
    monkeypatch.setattr("toolkit.controller.identity_api.LLDAPClient", lambda **_kwargs: client)
    monkeypatch.setattr("toolkit.controller.identity_api.begin_invite_activation", begin)

    result = activate_invite(root, _request(secrets))

    assert result.outcome == "failed"
    begin.assert_not_called()
    client.set_password.assert_not_called()


def test_directory_view_joins_users_and_groups_in_two_queries(tmp_path: Path, monkeypatch) -> None:
    users = [
        LLDAPUser(id="family", email="family@example.com", display_name="Family"),
        LLDAPUser(id="admin", email="owner@example.com", display_name="Owner"),
        LLDAPUser(id="ldap-bind", email="ldap-bind@example.com", display_name="LDAP Bind"),
    ]
    groups = [
        {"displayName": "homelab-media", "users": [{"id": "family"}, {"id": "admin"}]},
        {"displayName": "lldap_admin", "users": [{"id": "admin"}]},
    ]
    client = MagicMock()
    client.list_users.return_value = users
    client.list_groups.return_value = groups
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_config",
        lambda _path: Config(
            domain="example.com",
            services=ServicesConfig(media=True, cloud=False, email=True),
        ),
    )
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_secrets_plaintext",
        lambda _path: {
            "LLDAP_BIND_PASSWORD": "bind-secret",
            "LLDAP_ADMIN_PASSWORD": "admin-secret",
            "INVITE_TOKEN_SECRET": "a" * 32,
        },
    )
    monkeypatch.setattr("toolkit.controller.identity_api.LLDAPClient", lambda **_kwargs: client)

    view = read_directory_users(tmp_path)

    client.list_users.assert_called_once_with()
    client.list_groups.assert_called_once_with()
    assert [user.id for user in view.users] == ["admin", "family", "ldap-bind"]
    assert view.users[1].groups == ["homelab-media"]
    assert view.users[0].is_protected is True
    assert view.users[2].is_protected is True
    assert view.users[1].is_protected is False
    assert [group.name for group in view.group_options] == [
        "homelab-media",
        "homelab-cloud",
        "homelab-admin",
    ]
    assert view.group_options[0].is_default is True
    assert view.group_options[1].is_default is False
    assert view.invites_enabled is True


def test_directory_view_maps_dependency_failure_without_secret_details(tmp_path: Path, monkeypatch) -> None:
    from toolkit.controller.identity_api import DirectoryUnavailableError

    client = MagicMock()
    client.list_users.side_effect = RuntimeError("directory-secret-canary")
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_config",
        lambda _path: Config(domain="example.com"),
    )
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_secrets_plaintext",
        lambda _path: {"LLDAP_BIND_PASSWORD": "bind-secret"},
    )
    monkeypatch.setattr("toolkit.controller.identity_api.LLDAPClient", lambda **_kwargs: client)

    with pytest.raises(DirectoryUnavailableError) as exc_info:
        read_directory_users(tmp_path)

    assert "directory-secret-canary" not in str(exc_info.value)


def test_directory_view_disables_invites_when_token_secret_is_weak(tmp_path: Path, monkeypatch) -> None:
    client = MagicMock()
    client.list_users.return_value = []
    client.list_groups.return_value = []
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_config",
        lambda _path: Config(domain="example.com", services=ServicesConfig(email=True)),
    )
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_secrets_plaintext",
        lambda _path: {
            "LLDAP_BIND_PASSWORD": "bind-secret",
            "LLDAP_ADMIN_PASSWORD": "admin-secret",
            "INVITE_TOKEN_SECRET": "too-short",
        },
    )
    monkeypatch.setattr("toolkit.controller.identity_api.LLDAPClient", lambda **_kwargs: client)

    view = read_directory_users(tmp_path)

    assert view.invites_enabled is False
    assert view.invite_disabled_reason == "Identity invitation credentials are not configured."


@pytest.mark.parametrize(
    "command",
    (
        ReprovisionUserCommand(user_id="admin"),
        ReprovisionUserCommand(user_id="ldap-bind"),
        SetUserGroupsCommand(user_id="admin", groups=["homelab-admin"]),
        SetUserGroupsCommand(user_id="ldap-bind", groups=[]),
        DeleteDirectoryIdentityCommand(user_id="admin", confirmation="admin"),
        DeleteDirectoryIdentityCommand(user_id="ldap-bind", confirmation="ldap-bind"),
    ),
)
def test_protected_directory_accounts_cannot_be_mutated(tmp_path: Path, monkeypatch, command) -> None:
    from toolkit.controller.identity_api import DirectoryMutationError, execute_directory_command

    client = MagicMock()
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_config",
        lambda _path: Config(domain="example.com", services=ServicesConfig(email=True)),
    )
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_secrets_plaintext",
        lambda _path: {"LLDAP_ADMIN_PASSWORD": "admin-secret"},
    )
    monkeypatch.setattr("toolkit.controller.identity_api.LLDAPClient", lambda **_kwargs: client)

    with pytest.raises(DirectoryMutationError) as exc_info:
        execute_directory_command(tmp_path, command)

    assert exc_info.value.code == "FORBIDDEN"
    client.list_users.assert_not_called()
    client.set_user_groups.assert_not_called()
    client.delete_user.assert_not_called()


@pytest.mark.parametrize("protected_id", ("admin", "ldap-bind"))
def test_invite_email_cannot_mutate_protected_directory_account(
    tmp_path: Path,
    monkeypatch,
    protected_id: str,
) -> None:
    from toolkit.controller.identity_api import DirectoryMutationError, execute_directory_command

    client = MagicMock()
    client.find_user.return_value = LLDAPUser(
        id=protected_id,
        email="owner@example.com",
        display_name="Owner",
    )
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_config",
        lambda _path: Config(domain="example.com", services=ServicesConfig(email=True)),
    )
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_secrets_plaintext",
        lambda _path: {
            "LLDAP_ADMIN_PASSWORD": "admin-secret",
            "INVITE_TOKEN_SECRET": "a" * 32,
        },
    )
    monkeypatch.setattr("toolkit.controller.identity_api.LLDAPClient", lambda **_kwargs: client)

    with pytest.raises(DirectoryMutationError) as exc_info:
        execute_directory_command(
            tmp_path,
            InviteUserCommand(email="owner@example.com", groups=["homelab-admin"]),
        )

    assert exc_info.value.code == "FORBIDDEN"
    client.find_user.assert_called_once_with("owner@example.com")
    client.ensure_homelab_groups.assert_not_called()
    client.set_user_groups.assert_not_called()
    client.set_password.assert_not_called()


def test_invite_rejects_weak_token_secret_before_directory_mutation(tmp_path: Path, monkeypatch) -> None:
    from toolkit.controller.identity_api import DirectoryMutationError, execute_directory_command

    client = MagicMock()
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_config",
        lambda _path: Config(domain="example.com", services=ServicesConfig(email=True)),
    )
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_secrets_plaintext",
        lambda _path: {
            "LLDAP_ADMIN_PASSWORD": "admin-secret",
            "INVITE_TOKEN_SECRET": "too-short",
        },
    )
    monkeypatch.setattr("toolkit.controller.identity_api.LLDAPClient", lambda **_kwargs: client)

    with pytest.raises(DirectoryMutationError) as exc_info:
        execute_directory_command(
            tmp_path,
            InviteUserCommand(email="family@example.com", groups=["homelab-media"]),
        )

    assert exc_info.value.code == "OPERATION_REJECTED"
    client.find_user.assert_not_called()


def test_reprovision_issues_fresh_activation_email(tmp_path: Path, monkeypatch) -> None:
    from toolkit.controller.identity_api import execute_directory_command

    user = LLDAPUser(id="family", email="family@example.com", display_name="Family")
    client = MagicMock()
    client.list_users.return_value = [user]
    client.user_group_names.return_value = ["homelab-media"]
    deliver = MagicMock(return_value=WelcomeDelivery(status="sent", reason="sent"))
    provision = MagicMock(return_value=ServiceProvisionReport(()))
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_config",
        lambda _path: Config(domain="example.com", services=ServicesConfig(email=True, media=True)),
    )
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_secrets_plaintext",
        lambda _path: {
            "LLDAP_ADMIN_PASSWORD": "admin-secret",
            "INVITE_TOKEN_SECRET": "a" * 32,
        },
    )
    monkeypatch.setattr("toolkit.controller.identity_api.LLDAPClient", lambda **_kwargs: client)
    monkeypatch.setattr("toolkit.controller.identity_api.deliver_welcome_email", deliver)
    monkeypatch.setattr("toolkit.controller.identity_api.provision_user_services", provision)

    result = execute_directory_command(tmp_path, ReprovisionUserCommand(user_id="family"))

    deliver.assert_called_once_with(
        ANY,
        ANY,
        email="family@example.com",
        user_id="family",
        display_name="Family",
        groups=["homelab-media"],
        delivery_id=None,
    )
    provision.assert_called_once()
    assert result["steps"] == [{"key": "welcome_email", "status": "completed"}]


def test_reprovision_persists_typed_partial_failure_without_pii(tmp_path: Path, monkeypatch) -> None:
    from toolkit.controller.identity_api import execute_directory_command

    user = LLDAPUser(id="family", email="family@example.com", display_name="Family")
    client = MagicMock()
    client.list_users.return_value = [user]
    client.user_group_names.return_value = ["homelab-cloud"]
    report = ServiceProvisionReport(
        (
            ServiceProvisionStep(
                "vaultwarden_invite",
                "failed",
                "Vaultwarden invite failed for family@example.com: secret-canary",
            ),
            ServiceProvisionStep(
                "immich_oidc_first_login",
                "pending",
                "Immich will provision family@example.com on first login",
            ),
        )
    )
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_config",
        lambda _path: Config(domain="example.com", services=ServicesConfig(email=True, cloud=True)),
    )
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_secrets_plaintext",
        lambda _path: {
            "LLDAP_ADMIN_PASSWORD": "admin-secret",
            "INVITE_TOKEN_SECRET": "a" * 32,
        },
    )
    monkeypatch.setattr("toolkit.controller.identity_api.LLDAPClient", lambda **_kwargs: client)
    monkeypatch.setattr(
        "toolkit.controller.identity_api.deliver_welcome_email",
        lambda *_args, **_kwargs: WelcomeDelivery(status="sent", reason="sent"),
    )
    monkeypatch.setattr("toolkit.controller.identity_api.provision_user_services", lambda *_args, **_kwargs: report)

    result = execute_directory_command(tmp_path, ReprovisionUserCommand(user_id="family"))

    assert result == {
        "action": "reprovision",
        "user_id": "family",
        "outcome": "partial_failure",
        "steps": [
            {"key": "welcome_email", "status": "completed"},
            {"key": "vaultwarden_invite", "status": "failed"},
            {"key": "immich_oidc_first_login", "status": "pending"},
        ],
    }
    assert "family@example.com" not in str(result)
    assert "secret-canary" not in str(result)


def test_group_update_verifies_directory_membership_before_provisioning(tmp_path: Path, monkeypatch) -> None:
    from toolkit.controller.identity_api import DirectoryMutationError, execute_directory_command

    user = LLDAPUser(id="family", email="family@example.com", display_name="Family")
    client = MagicMock()
    client.list_users.return_value = [user]
    client.user_group_names.return_value = ["homelab-media"]
    provision = MagicMock()
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_config",
        lambda _path: Config(domain="example.com", services=ServicesConfig(cloud=True, media=True)),
    )
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_secrets_plaintext",
        lambda _path: {"LLDAP_ADMIN_PASSWORD": "admin-secret"},
    )
    monkeypatch.setattr("toolkit.controller.identity_api.LLDAPClient", lambda **_kwargs: client)
    monkeypatch.setattr("toolkit.controller.identity_api.provision_user_services", provision)

    with pytest.raises(DirectoryMutationError) as exc_info:
        execute_directory_command(
            tmp_path,
            SetUserGroupsCommand(user_id="family", groups=["homelab-cloud"]),
        )

    assert exc_info.value.code == "OPERATION_FAILED"
    client.ensure_homelab_groups.assert_called_once_with(["homelab-cloud"])
    client.set_user_groups.assert_called_once_with("family", ["homelab-cloud"])
    provision.assert_not_called()


@pytest.mark.parametrize("existing", (False, True))
def test_invite_email_failure_returns_typed_partial_result(
    tmp_path: Path,
    monkeypatch,
    existing: bool,
) -> None:
    from toolkit.controller.identity_api import execute_directory_command

    user = LLDAPUser(id="family", email="family@example.com", display_name="Family")
    client = MagicMock()
    client.find_user.return_value = user if existing else None
    client.user_group_names.return_value = ["homelab-media"]
    provision = MagicMock()
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_config",
        lambda _path: Config(domain="example.com", services=ServicesConfig(email=True, media=True)),
    )
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_secrets_plaintext",
        lambda _path: {
            "LLDAP_ADMIN_PASSWORD": "admin-secret",
            "INVITE_TOKEN_SECRET": "a" * 32,
        },
    )
    monkeypatch.setattr("toolkit.controller.identity_api.LLDAPClient", lambda **_kwargs: client)
    monkeypatch.setattr("toolkit.controller.identity_api.invite_directory_user", lambda *_args, **_kwargs: (user, []))
    monkeypatch.setattr(
        "toolkit.controller.identity_api.deliver_welcome_email",
        lambda *_args, **_kwargs: WelcomeDelivery(status="failed", reason="smtp"),
    )
    monkeypatch.setattr("toolkit.controller.identity_api.provision_user_services", provision)

    result = execute_directory_command(
        tmp_path,
        InviteUserCommand(email="family@example.com", groups=["homelab-media"]),
    )

    assert result == {
        "action": "invite",
        "user_id": "family",
        "outcome": "partial_failure",
        "steps": [
            {"key": "directory", "status": "completed"},
            {"key": "welcome_email", "status": "failed"},
        ],
    }
    assert "family@example.com" not in str(result)
    provision.assert_not_called()


def test_replayed_directory_deletion_treats_absent_target_as_converged(tmp_path: Path, monkeypatch) -> None:
    from toolkit.controller.identity_api import DirectoryMutationError, execute_directory_command

    client = MagicMock()
    client.list_users.return_value = []
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_config",
        lambda _path: Config(domain="example.com"),
    )
    monkeypatch.setattr(
        "toolkit.controller.identity_api.load_secrets_plaintext",
        lambda _path: {"LLDAP_ADMIN_PASSWORD": "admin-secret"},
    )
    monkeypatch.setattr("toolkit.controller.identity_api.LLDAPClient", lambda **_kwargs: client)
    command = DeleteDirectoryIdentityCommand(user_id="family", confirmation="family")

    with pytest.raises(DirectoryMutationError) as exc_info:
        execute_directory_command(tmp_path, command)

    assert exc_info.value.code == "NOT_FOUND"
    result = execute_directory_command(tmp_path, command, replay=True)
    assert result == {
        "action": "delete_directory_identity",
        "user_id": "family",
        "outcome": "completed",
        "steps": [{"key": "directory_identity", "status": "completed"}],
    }
