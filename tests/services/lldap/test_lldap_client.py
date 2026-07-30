from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import MagicMock, call, patch

import pytest
from toolkit.core.config.config import Config
from toolkit.core.identity.lldap_client import LLDAPClient, LLDAPUser, resolve_lldap_api_url, user_id_from_email


def test_user_id_from_email():
    assert user_id_from_email("brei@example.com") == "brei-65330c20"


@pytest.mark.parametrize("email", ["@example.com", "+@example.com", "missing-at-sign"])
def test_user_id_from_email_rejects_unsafe_ids(email: str):
    with pytest.raises(ValueError, match="user ID"):
        user_id_from_email(email)


def test_user_id_is_collision_resistant_across_domains_and_reserved_names():
    first = user_id_from_email("admin@example.com")
    second = user_id_from_email("admin@other.example")

    assert first != second
    assert first != "admin"
    assert second != "admin"


def test_controller_uses_scoped_service_dns_for_local_lldap(tmp_path, monkeypatch):
    config = MagicMock(is_multi_node=True)
    monkeypatch.setenv("HOMELAB_NODE", "infra")
    monkeypatch.setenv("HOMELAB_CONTROLLER_ROLE", "local")
    monkeypatch.setattr("toolkit.core.config.config.load_config", lambda _path: config)
    monkeypatch.setattr("toolkit.core.manifest.placement.service_node", lambda _cfg, _service: "infra")
    monkeypatch.setattr(
        "toolkit.core.identity.lldap_client.resolve_docker_service_url",
        MagicMock(side_effect=AssertionError("controller must not use an unrelated bridge IP")),
    )

    assert resolve_lldap_api_url(tmp_path) == "http://lldap:17170"


def test_find_user_never_matches_a_different_email_by_colliding_id():
    client = LLDAPClient(admin_password="admin")
    client.list_users = MagicMock(return_value=[LLDAPUser(id="family", email="other@example.com")])

    assert client.find_user("family@example.com") is None


def test_ensure_owner_updates_existing(monkeypatch):
    client = LLDAPClient(admin_password="admin")
    client.find_user = MagicMock(return_value=LLDAPUser(id="brei", email="brei@example.com"))
    client.set_password = MagicMock()
    client.ensure_groups = MagicMock(return_value=["added brei to lldap_admin"])
    client.ensure_user_posix = MagicMock(return_value=[])

    logs = client.ensure_owner("brei@example.com", "secret", domain="example.com", groups=["lldap_admin"])

    client.set_password.assert_called_once_with("brei", "secret")
    client.ensure_user_posix.assert_called_once_with("brei")
    assert any("already exists" in line for line in logs)
    assert any("password updated" in line for line in logs)


def test_create_user_removes_directory_entry_when_posix_setup_fails():
    client = LLDAPClient(admin_password="admin")
    client._graphql = MagicMock(
        return_value={
            "createUser": {
                "id": "brei",
                "email": "brei@example.com",
                "displayName": "Brei",
            }
        }
    )
    client.ensure_user_posix = MagicMock(side_effect=RuntimeError("posix failed"))
    client.delete_user = MagicMock()

    with pytest.raises(RuntimeError, match="posix failed"):
        client.create_user("brei@example.com", user_id="brei")

    client.delete_user.assert_called_once_with("brei")


def test_ensure_owner_migrates_to_explicit_username_and_preserves_groups():
    client = LLDAPClient(admin_password="admin")
    existing = LLDAPUser(id="brei-65330c20", email="brei@example.com", display_name="Brei")
    client.find_user = MagicMock(return_value=existing)
    client.list_users = MagicMock(return_value=[existing])
    client.user_group_names = MagicMock(return_value=["homelab-cloud"])
    client.get_user_attribute = MagicMock(
        side_effect=lambda _user_id, name: {
            "uidNumber": "3007",
            "gidNumber": "3000",
            "homeDirectory": "/home/brei-65330c20",
        }[name]
    )
    client.update_user_email = MagicMock()
    client.create_user = MagicMock(return_value=LLDAPUser(id="brei", email="brei@example.com", display_name="Brei"))
    client.set_password = MagicMock()
    client.ensure_groups = MagicMock(return_value=["added brei to lldap_admin"])
    client.ensure_user_posix = MagicMock(return_value=[])
    client.delete_user = MagicMock()

    logs = client.ensure_owner(
        "brei@example.com",
        "secret",
        domain="example.com",
        groups=["lldap_admin"],
        user_id="brei",
    )

    client.update_user_email.assert_called_once_with("brei-65330c20", "brei-65330c20@migrated.invalid")
    client.create_user.assert_called_once_with(
        "brei@example.com",
        display_name="Brei",
        user_id="brei",
        posix_uid=3007,
        posix_gid=3000,
        posix_home="/home/brei-65330c20",
    )
    client.set_password.assert_called_once_with("brei", "secret")
    client.ensure_groups.assert_called_once_with("brei", ["homelab-cloud", "lldap_admin"])
    client.delete_user.assert_called_once_with("brei-65330c20")
    assert any("migrated owner username" in line for line in logs)


def test_ensure_owner_refuses_conflicting_explicit_username():
    client = LLDAPClient(admin_password="admin")
    existing = LLDAPUser(id="brei-65330c20", email="brei@example.com")
    client.find_user = MagicMock(return_value=existing)
    client.list_users = MagicMock(return_value=[existing, LLDAPUser(id="brei", email="someone@example.com")])

    with pytest.raises(RuntimeError, match="already belongs"):
        client.ensure_owner("brei@example.com", "secret", user_id="brei")


def test_ensure_owner_checks_admin_conflict_before_moving_email():
    client = LLDAPClient(admin_password="admin")
    admin = LLDAPUser(id="admin", email="brei@example.com")
    client.find_user = MagicMock(return_value=admin)
    client.list_users = MagicMock(return_value=[admin, LLDAPUser(id="brei", email="someone@example.com")])
    client.update_user_email = MagicMock()

    with pytest.raises(RuntimeError, match="already belongs"):
        client.ensure_owner("brei@example.com", "secret", user_id="brei")

    client.update_user_email.assert_not_called()


def test_ensure_owner_rolls_back_migration_when_old_user_delete_fails():
    client = LLDAPClient(admin_password="admin")
    existing = LLDAPUser(id="brei-old", email="brei@example.com", display_name="Brei")
    created = LLDAPUser(id="brei", email="brei@example.com", display_name="Brei")
    client.find_user = MagicMock(return_value=existing)
    client.list_users = MagicMock(return_value=[existing])
    client.user_group_names = MagicMock(return_value=["lldap_admin"])
    client.get_user_attribute = MagicMock(return_value=None)
    client.update_user_email = MagicMock()
    client.create_user = MagicMock(return_value=created)
    client.set_password = MagicMock()
    client.ensure_groups = MagicMock(return_value=[])
    client.ensure_user_posix = MagicMock(return_value=[])
    client.delete_user = MagicMock(side_effect=[RuntimeError("delete failed"), None])

    with pytest.raises(RuntimeError, match="delete failed"):
        client.ensure_owner("brei@example.com", "secret", user_id="brei")

    assert client.delete_user.call_args_list == [call("brei-old"), call("brei")]
    assert client.update_user_email.call_args_list == [
        call("brei-old", "brei-old@migrated.invalid"),
        call("brei-old", "brei@example.com"),
    ]


def test_ensure_owner_restores_admin_email_when_creation_fails():
    client = LLDAPClient(admin_password="admin")
    admin = LLDAPUser(id="admin", email="brei@example.com")
    client.find_user = MagicMock(side_effect=[admin, None])
    client.list_users = MagicMock(return_value=[admin])
    client.update_user_email = MagicMock()
    client.create_user = MagicMock(side_effect=RuntimeError("create failed"))

    with pytest.raises(RuntimeError, match="create failed"):
        client.ensure_owner(
            "brei@example.com",
            "secret",
            domain="example.com",
            user_id="brei",
        )

    assert client.update_user_email.call_args_list == [
        call("admin", "lldap-admin@example.com"),
        call("admin", "brei@example.com"),
    ]


def test_ensure_owner_restores_admin_email_when_post_move_lookup_fails():
    client = LLDAPClient(admin_password="admin")
    admin = LLDAPUser(id="admin", email="brei@example.com")
    client.find_user = MagicMock(side_effect=[admin, RuntimeError("lookup failed")])
    client.list_users = MagicMock(return_value=[admin])
    client.update_user_email = MagicMock()

    with pytest.raises(RuntimeError, match="lookup failed"):
        client.ensure_owner(
            "brei@example.com",
            "secret",
            domain="example.com",
            user_id="brei",
        )

    assert client.update_user_email.call_args_list == [
        call("admin", "lldap-admin@example.com"),
        call("admin", "brei@example.com"),
    ]


def test_ensure_homelab_groups_creates_missing(monkeypatch):
    client = LLDAPClient(admin_password="admin")
    client._graphql = MagicMock(
        side_effect=[
            {"groups": [{"displayName": "homelab-media", "id": 1}]},
            {"createGroup": {"id": 2, "displayName": "homelab-cloud"}},
        ]
    )
    client.create_group = MagicMock(return_value=2)
    logs = client.ensure_homelab_groups(["homelab-media", "homelab-cloud"])
    assert any("homelab-cloud" in line for line in logs)


def test_ensure_owner_moves_admin_email(monkeypatch):
    client = LLDAPClient(admin_password="admin")
    client.find_user = MagicMock(
        side_effect=[
            LLDAPUser(id="admin", email="brei@example.com"),
            None,
        ]
    )
    client.list_users = MagicMock(return_value=[LLDAPUser(id="admin", email="brei@example.com")])
    client.update_user_email = MagicMock()
    client.create_user = MagicMock(return_value=LLDAPUser(id="brei", email="brei@example.com"))
    client.set_password = MagicMock()
    client.ensure_groups = MagicMock(return_value=[])
    client.ensure_user_posix = MagicMock(return_value=["posix brei uid=3000"])

    logs = client.ensure_owner("brei@example.com", "secret", domain="example.com")

    client.update_user_email.assert_called_once_with("admin", "lldap-admin@example.com")
    client.create_user.assert_called_once_with("brei@example.com", user_id="brei-65330c20")
    client.ensure_user_posix.assert_called_once_with("brei")
    assert any("moved admin account email" in line for line in logs)


def test_ensure_user_posix_skips_when_set(monkeypatch):
    client = LLDAPClient(admin_password="admin")
    client.get_user_attribute = MagicMock(return_value="3001")
    logs = client.ensure_user_posix("brei")
    assert any("already set" in line for line in logs)


def test_ensure_user_posix_allocates_uid(monkeypatch):
    client = LLDAPClient(admin_password="admin")
    client.get_user_attribute = MagicMock(return_value=None)
    client.ensure_posix_schema = MagicMock(return_value=[])
    client.ensure_homelab_users_group = MagicMock(return_value=[])
    client.ensure_homelab_group_gids = MagicMock(return_value=[])
    client.ensure_groups = MagicMock(return_value=[])
    client.next_uid = MagicMock(return_value=3005)
    client._insert_user_attributes = MagicMock()

    logs = client.ensure_user_posix("brei")
    client.ensure_posix_schema.assert_called_once()
    client._insert_user_attributes.assert_called_once()
    attrs = client._insert_user_attributes.call_args[0][1]
    assert attrs["uidNumber"] == "3005"
    assert attrs["gidNumber"] == "3000"
    assert attrs["homeDirectory"] == "/home/brei"
    assert any("uid=3005" in line for line in logs)


def test_ensure_user_posix_serializes_uid_allocation_across_clients(tmp_path):
    allocated: dict[str, int] = {}
    barrier = threading.Barrier(2)

    def make_client() -> LLDAPClient:
        client = LLDAPClient(admin_password="admin", root=tmp_path)
        client.get_user_attribute = MagicMock(return_value=None)
        client.ensure_posix_schema = MagicMock(return_value=[])
        client.ensure_homelab_users_group = MagicMock(return_value=[])
        client.ensure_homelab_group_gids = MagicMock(return_value=[])
        client.ensure_groups = MagicMock(return_value=[])

        def next_uid() -> int:
            candidate = max(allocated.values(), default=2999) + 1
            time.sleep(0.05)
            return candidate

        client.next_uid = MagicMock(side_effect=next_uid)

        def insert(user_id: str, attrs: dict[str, str]) -> None:
            allocated[user_id] = int(attrs["uidNumber"])

        client._insert_user_attributes = MagicMock(side_effect=insert)
        return client

    first = make_client()
    second = make_client()

    def allocate(client: LLDAPClient, user_id: str) -> None:
        barrier.wait()
        client.ensure_user_posix(user_id)

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(allocate, first, "first"),
            pool.submit(allocate, second, "second"),
        ]
        for future in futures:
            future.result(timeout=5)

    assert sorted(allocated.values()) == [3000, 3001]
    lock_path = tmp_path / ".homelab-state" / "lldap-identity.lock"
    assert lock_path.stat().st_mode & 0o777 == 0o600


def test_verify_user_password_success(monkeypatch):
    """verify_user_password returns (True, user_id) when LLDAP accepts the login."""
    captured = {}

    class FakeResponse:
        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def json(self):
            return self._payload

    def fake_post(url, json=None, timeout=None):
        captured["url"] = url
        captured["body"] = json
        return FakeResponse(200, {"token": "jwt-token-here"})

    monkeypatch.setattr("toolkit.core.identity.lldap_client.httpx.post", fake_post)
    monkeypatch.setattr(
        "toolkit.core.identity.lldap_client.resolve_lldap_api_url",
        lambda root: "http://lldap:17170",
    )
    ok, msg = LLDAPClient.verify_user_password("brei@example.com", "secret123")
    assert ok is True
    assert msg == "brei-65330c20"
    assert captured["body"]["username"] == "brei-65330c20"
    assert captured["body"]["password"] == "secret123"


def test_verify_owner_password_uses_configured_username(monkeypatch, tmp_path):
    """The owner's stable configured username is used instead of the legacy email hash."""
    captured = {}

    class FakeResponse:
        status_code = 200

        @staticmethod
        def json():
            return {"token": "jwt-token-here"}

    def fake_post(_url, json=None, timeout=None):
        captured["body"] = json
        return FakeResponse()

    (tmp_path / "config.yaml").write_text(
        "domain: example.com\nemail: brei@example.com\nowner_username: brei\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("toolkit.core.identity.lldap_client.httpx.post", fake_post)
    monkeypatch.setattr(
        "toolkit.core.identity.lldap_client.resolve_lldap_api_url",
        lambda root: "http://lldap:17170",
    )

    ok, msg = LLDAPClient.verify_user_password(
        "BREI@example.com",
        "secret123",
        root=tmp_path,
    )

    assert ok is True
    assert msg == "brei"
    assert captured["body"]["username"] == "brei"


def test_verify_user_password_invalid(monkeypatch):
    """verify_user_password returns (False, ...) on bad credentials."""

    class FakeResponse:
        def __init__(self, status_code):
            self.status_code = status_code

        def json(self):
            return {}

    monkeypatch.setattr(
        "toolkit.core.identity.lldap_client.httpx.post",
        lambda *a, **k: FakeResponse(401),
    )
    monkeypatch.setattr(
        "toolkit.core.identity.lldap_client.resolve_lldap_api_url",
        lambda root: "http://lldap:17170",
    )
    ok, msg = LLDAPClient.verify_user_password("brei@example.com", "wrong")
    assert ok is False
    assert "Invalid" in msg


def test_verify_user_password_unreachable(monkeypatch):
    """verify_user_password returns (False, ...) when LLDAP is unreachable."""
    import httpx

    def fake_post(*a, **k):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("toolkit.core.identity.lldap_client.httpx.post", fake_post)
    monkeypatch.setattr(
        "toolkit.core.identity.lldap_client.resolve_lldap_api_url",
        lambda root: "http://lldap:17170",
    )
    ok, msg = LLDAPClient.verify_user_password("brei@example.com", "secret")
    assert ok is False
    assert "unreachable" in msg.lower()


def test_ensure_service_bind_creates_account_and_sets_password():
    """ensure_service_bind creates the ldap-bind account, sets its password, and groups it."""
    client = LLDAPClient(admin_password="admin")
    client.list_users = MagicMock(return_value=[])
    client._graphql = MagicMock(return_value={"createUser": {"id": "ldap-bind", "email": "ldap-bind@example.com"}})
    client.set_password = MagicMock()
    client.ensure_groups = MagicMock(return_value=["added ldap-bind to lldap_strict_readonly"])

    logs = client.ensure_service_bind("bind-pass", domain="example.com")
    assert any("created service account" in line for line in logs)
    client.set_password.assert_called_once_with("ldap-bind", "bind-pass")
    client.ensure_groups.assert_called_once_with("ldap-bind", ["lldap_strict_readonly", "lldap_password_manager"])


def test_set_password_sends_secrets_only_over_stdin():
    client = LLDAPClient(admin_password="admin")
    client._token = "directory-jwt-secret"

    with patch("toolkit.core.identity.lldap_client.docker_exec", return_value=(0, "ok")) as execute:
        client.set_password("family", "new-directory-password")

    args, kwargs = execute.call_args
    assert args[0] == "lldap"
    assert args[1][:2] == ["sh", "-ec"]
    assert "/app/lldap_set_password" in args[1][2]
    assert "LLDAP_USER_PASSWORD" in args[1][2]
    payload = json.loads(kwargs["stdin"])
    assert payload == {
        "token": "directory-jwt-secret",
        "username": "family",
        "password": "new-directory-password",
    }
    assert "directory-jwt-secret" not in repr(args)
    assert "new-directory-password" not in repr(args)


def test_set_password_uses_supported_remote_helper_and_stdin_for_multi_node(tmp_path):
    client = LLDAPClient(admin_password="admin", root=tmp_path)
    client._token = "directory-jwt-secret"
    cfg = Config()

    with (
        patch("toolkit.core.config.config.load_config", return_value=cfg),
        patch("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", return_value=(0, "ok", "")) as execute,
    ):
        client.set_password("family", "new-directory-password")

    args, kwargs = execute.call_args
    assert args[1] == "10.10.10.10"
    assert "docker exec -i lldap sh -ec" in args[2]
    assert "/app/lldap_set_password" in args[2]
    assert json.loads(kwargs["stdin"])["password"] == "new-directory-password"
    assert "new-directory-password" not in repr(args)
