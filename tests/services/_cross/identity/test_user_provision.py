from __future__ import annotations

from unittest.mock import MagicMock, patch

import httpx
import pytest
from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.identity.service_groups import build_authelia_access_rules
from toolkit.core.identity.user_provision import provision_user_services
from toolkit.core.manifest.catalog import ServiceCatalog
from toolkit.core.manifest.schema import ServiceManifest
from toolkit.services import IdentityProvisionResult, ServicePlugin
from toolkit.services.nextcloud.plugin import NextcloudPlugin
from toolkit.services.vaultwarden.plugin import VaultwardenPlugin


def test_build_authelia_access_rules_admin_hosts():
    cfg = Config(
        domain="example.com",
        services=ServicesConfig(management=True, media=True, cloud=True),
    )
    rules = build_authelia_access_rules(cfg)
    domains = [r["domain"] for r in rules]
    assert "auth.example.com" in domains
    assert "grafana.example.com" in domains
    assert "sonarr.example.com" in domains
    assert "music.example.com" in domains
    assert "fmd.example.com" in domains
    assert "git.example.com" in domains
    assert "gitea.example.com" not in domains
    sonarr = next(r for r in rules if r["domain"] == "sonarr.example.com")
    music = next(r for r in rules if r["domain"] == "music.example.com")
    assert "group:homelab-admin" in sonarr["subjects"]
    assert "group:homelab-media" not in sonarr["subjects"]
    assert "group:homelab-media" in music["subjects"]
    requests = next(r for r in rules if r["domain"] == "requests.example.com")
    assert "group:homelab-media" in requests["subjects"]
    fmd = next(r for r in rules if r["domain"] == "fmd.example.com")
    assert "group:homelab-admin" in fmd["subjects"]
    assert "group:homelab-media" not in fmd["subjects"]
    wildcard = next(r for r in rules if r["domain"] == "*.example.com")
    assert "group:homelab-admin" in wildcard["subjects"]
    assert "group:homelab-media" not in wildcard["subjects"]
    assert "group:homelab-cloud" not in wildcard["subjects"]


def test_invite_vaultwarden_skips_existing():
    cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
    response = MagicMock(status_code=200)
    response.json.return_value = [{"email": "u@example.com"}]
    with (
        patch("toolkit.services.sdk.vaultwarden_admin_session", return_value=MagicMock()),
        patch("toolkit.services.sdk.vaultwarden_url", return_value="http://vaultwarden"),
        patch("toolkit.services.vaultwarden.plugin.httpx.get", return_value=response),
    ):
        results = VaultwardenPlugin().provision_identity(cfg, {"VAULTWARDEN_ADMIN_TOKEN": "tok"}, "u@example.com")
    assert [(step.key, step.status) for step in results] == [("vaultwarden_invite", "completed")]
    assert "already has an account" in results[0].message


def test_invite_vaultwarden_reports_missing_credentials_as_failed():
    cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))

    results = VaultwardenPlugin().provision_identity(cfg, {}, "u@example.com")

    assert [(step.key, step.status) for step in results] == [("vaultwarden_invite", "failed")]
    assert "VAULTWARDEN_ADMIN_TOKEN missing" in results[0].message


def test_invite_vaultwarden_stops_when_existing_user_lookup_is_unavailable():
    cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
    with (
        patch("toolkit.services.sdk.vaultwarden_url", return_value="http://vaultwarden"),
        patch("toolkit.services.sdk.vaultwarden_admin_session", return_value=MagicMock()),
        patch("toolkit.services.vaultwarden.plugin.httpx.get", side_effect=httpx.ConnectError("private detail")),
        patch("toolkit.services.vaultwarden.plugin.httpx.post") as post,
    ):
        results = VaultwardenPlugin().provision_identity(cfg, {"VAULTWARDEN_ADMIN_TOKEN": "tok"}, "u@example.com")

    assert [(step.key, step.status) for step in results] == [("vaultwarden_invite", "failed")]
    assert results[0].message == "Vaultwarden: existing-user lookup unavailable"
    post.assert_not_called()


def test_nextcloud_oidc_reports_each_configuration_step():
    cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
    with patch(
        "toolkit.services.sdk.docker_exec_on_vm",
        side_effect=[(0, ""), (1, "group rejected")],
    ):
        results = NextcloudPlugin().provision_identity(cfg, {}, "u@example.com")

    assert [(step.key, step.status) for step in results] == [
        ("nextcloud_oidc_auto_provision", "completed"),
        ("nextcloud_oidc_group_provision", "failed"),
    ]


def test_nextcloud_oidc_executes_on_apps_vm_for_multi_vm(tmp_path):
    cfg = Config(
        domain="example.com",
        services=ServicesConfig(media=False, cloud=True, email=False),
    )
    with (
        patch(
            "toolkit.services.sdk.docker_exec_on_vm",
            side_effect=[(0, ""), (0, "")],
        ) as remote_exec,
        patch("toolkit.core.ops.automation.docker_exec") as local_exec,
    ):
        results = NextcloudPlugin().provision_identity(cfg, {}, "u@example.com", root=tmp_path)

    assert all(result.status == "completed" for result in results)
    assert remote_exec.call_count == 2
    for call in remote_exec.call_args_list:
        assert call.args[0] is cfg
        assert call.args[1] == "nextcloud"
        assert call.args[3] == cfg.node_ip("apps")
        assert call.args[4] == tmp_path
        assert call.kwargs["user"] == "www-data"
    local_exec.assert_not_called()


def test_nextcloud_oidc_keeps_single_vm_execution_local(tmp_path):
    cfg = MagicMock()
    cfg.services.cloud = True
    cfg.is_multi_node = False
    with (
        patch(
            "toolkit.core.ops.automation.docker_exec",
            side_effect=[(0, ""), (0, "")],
        ) as local_exec,
        patch("toolkit.services.sdk.docker_exec_on_vm") as remote_exec,
    ):
        results = NextcloudPlugin().provision_identity(cfg, {}, "u@example.com", root=tmp_path)

    assert all(result.status == "completed" for result in results)
    assert local_exec.call_count == 2
    remote_exec.assert_not_called()


def test_optional_owner_notification_failure_is_a_warning():
    from toolkit.core.identity.user_provision import _notify_service_invite

    cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))
    with patch("toolkit.services.ntfy.client.post_ntfy_url", return_value=False):
        report = _notify_service_invite(
            cfg,
            {"DEPLOY_NTFY_URL": "https://notify.example.com/topic"},
            "u@example.com",
            ["homelab-cloud"],
        )

    assert [(step.key, step.status) for step in report.steps] == [("owner_notification", "warning")]
    assert report.successful is True


def test_invite_directory_user_uses_placeholder_password():
    from unittest.mock import MagicMock

    from toolkit.core.identity.user_provision import invite_directory_user

    client = MagicMock()
    client.find_user.return_value = None
    client.create_user.return_value = MagicMock(id="family", email="family@example.com")
    client.user_group_names.return_value = ["homelab-media", "lldap_password_manager"]

    user, logs = invite_directory_user(client, "family@example.com", groups=["homelab-media"])
    client.set_password.assert_called_once()
    client.set_user_groups.assert_called_once_with("family", ["homelab-media"])
    client.ensure_groups.assert_called_once_with("family", [])
    assert user.id == "family"
    assert any("placeholder" in line.lower() for line in logs)


def test_reinviting_existing_directory_user_does_not_reset_password():
    from toolkit.core.identity.lldap_client import LLDAPUser
    from toolkit.core.identity.user_provision import invite_directory_user

    client = MagicMock()
    client.find_user.return_value = LLDAPUser(id="family", email="family@example.com")

    user, logs = invite_directory_user(client, "family@example.com", groups=["homelab-media"])

    assert user.id == "family"
    client.set_password.assert_not_called()
    client.set_user_groups.assert_called_once_with("family", ["homelab-media"])
    assert any("kept" in line.lower() for line in logs)


def test_invite_without_email_or_admin_password_fails_before_creating_user():
    from toolkit.core.identity.user_provision import invite_and_provision_user

    client = MagicMock()
    cfg = Config(domain="example.com", services=ServicesConfig(email=False))

    with pytest.raises(RuntimeError, match="Email service"):
        invite_and_provision_user(cfg, {}, client, "family@example.com")

    client.find_user.assert_not_called()
    client.create_user.assert_not_called()


def test_provision_user_services_cloud_group():
    cfg = Config(domain="example.com", services=ServicesConfig(cloud=False))

    report = provision_user_services(
        cfg,
        {},
        "u@example.com",
        ["homelab-cloud"],
        notify=False,
    )

    assert [(step.key, step.status) for step in report.steps] == [
        ("vaultwarden_invite", "skipped"),
        ("nextcloud_oidc_auto_provision", "skipped"),
        ("nextcloud_oidc_group_provision", "skipped"),
        ("immich_oidc_first_login", "skipped"),
        ("gitea_sso_first_login", "skipped"),
        ("owner_notification", "skipped"),
    ]
    assert report.messages == tuple(step.message for step in report.steps)


def test_provision_user_services_reports_requested_media_when_disabled():
    cfg = Config(domain="example.com", services=ServicesConfig(media=False))

    report = provision_user_services(
        cfg,
        {},
        "u@example.com",
        ["homelab-media"],
        notify=False,
    )

    assert [(step.key, step.status) for step in report.steps] == [
        ("jellyfin_ldap_first_login", "skipped"),
        ("navidrome_external_auth_first_login", "skipped"),
        ("romm_oidc_first_login", "pending"),
        ("owner_notification", "skipped"),
    ]


def test_custom_service_contributes_first_login_provisioning_without_core_change() -> None:
    manifest = ServiceManifest.model_validate(
        {
            "name": "example",
            "label": "Example",
            "description": "Custom service",
            "icon": "box",
            "category": "cloud",
            "placement": "apps",
            "priority": 50,
            "identity": {
                "provisioning": [
                    {
                        "id": "example_first_login",
                        "mode": "first_login",
                        "priority": 10,
                        "message": "Example: first login creates the account",
                        "disabled_message": "Example: cloud service disabled",
                    }
                ]
            },
        }
    )

    report = provision_user_services(
        Config(domain="example.com", services={"cloud": True}),
        {},
        "u@example.com",
        ["homelab-cloud"],
        notify=False,
        catalog=ServiceCatalog((manifest,)),
        plugins={},
    )

    assert [(step.key, step.status, step.message) for step in report.steps] == [
        ("example_first_login", "pending", "Example: first login creates the account"),
        ("owner_notification", "skipped", "Invite: owner notification disabled"),
    ]


def test_plugin_provisioning_fails_closed_on_undeclared_step_ids() -> None:
    manifest = ServiceManifest.model_validate(
        {
            "name": "example",
            "label": "Example",
            "description": "Custom service",
            "icon": "box",
            "category": "cloud",
            "placement": "apps",
            "priority": 50,
            "identity": {
                "provisioning": [
                    {
                        "id": "declared_step",
                        "mode": "plugin",
                        "disabled_message": "Example disabled",
                    }
                ]
            },
        }
    )

    class InvalidPlugin(ServicePlugin):
        def provision_identity(self, cfg, secrets, email, *, root=None):
            return (IdentityProvisionResult("unknown_step", "completed", "invalid"),)

    report = provision_user_services(
        Config(domain="example.com", services={"cloud": True}),
        {},
        "u@example.com",
        ["homelab-cloud"],
        notify=False,
        catalog=ServiceCatalog((manifest,)),
        plugins={"example": InvalidPlugin()},
    )

    assert [(step.key, step.status) for step in report.steps] == [
        ("declared_step", "failed"),
        ("owner_notification", "skipped"),
    ]


def test_first_login_provisioning_is_reported_as_pending(tmp_path):
    cfg = Config(domain="example.com", services=ServicesConfig(cloud=True, media=True))
    with (
        patch(
            "toolkit.services.vaultwarden.plugin.VaultwardenPlugin.provision_identity",
            return_value=(IdentityProvisionResult("vaultwarden_invite", "completed", "Vaultwarden ready"),),
        ),
        patch(
            "toolkit.services.nextcloud.plugin.NextcloudPlugin.provision_identity",
            return_value=(
                IdentityProvisionResult("nextcloud_oidc_auto_provision", "completed", "OIDC ready"),
                IdentityProvisionResult("nextcloud_oidc_group_provision", "completed", "Groups ready"),
            ),
        ) as oidc_provision,
    ):
        report = provision_user_services(
            cfg,
            {},
            "u@example.com",
            ["homelab-cloud", "homelab-media"],
            notify=False,
            root=tmp_path,
        )

    assert [(step.key, step.status) for step in report.steps if step.status == "pending"] == [
        ("immich_oidc_first_login", "pending"),
        ("jellyfin_ldap_first_login", "pending"),
        ("navidrome_external_auth_first_login", "pending"),
        ("romm_oidc_first_login", "pending"),
        ("gitea_sso_first_login", "pending"),
    ]
    assert report.successful is True
    oidc_provision.assert_called_once_with(cfg, {}, "u@example.com", root=tmp_path)
