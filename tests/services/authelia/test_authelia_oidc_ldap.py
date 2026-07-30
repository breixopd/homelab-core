from __future__ import annotations

import yaml
from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.generate.generate import generate_configs
from toolkit.core.secrets.secrets import generate_all_secrets, get_required_secrets, save_secrets_plaintext


def _generate_authelia(
    tmp_path,
    *,
    cloud: bool = True,
    management: bool = True,
    email: bool = False,
) -> str:
    cfg = Config(
        domain="test.example.com",
        services=ServicesConfig(
            media=False,
            cloud=cloud,
            notifications=False,
            email=email,
            security=False,
            management=management,
        ),
    )
    specs = get_required_secrets(cfg)
    secrets = generate_all_secrets(specs)
    save_secrets_plaintext(secrets, tmp_path / "secrets.enc.yaml")
    generate_configs(cfg, tmp_path)
    return (tmp_path / "generated" / "authelia.yml").read_text()


def test_authelia_ldap_lldap_implementation_and_service_bind(tmp_path):
    content = _generate_authelia(tmp_path)
    assert "implementation: 'lldap'" in content
    assert "cn=ldap-bind,ou=people,dc=test,dc=example,dc=com" in content
    assert "password: '" in content
    assert "cn=admin" not in content
    assert "groups_filter: '(&(member={dn})(objectClass=groupOfNames))'" in content


def test_authelia_claims_policies_and_vaultwarden_scope(tmp_path):
    content = _generate_authelia(tmp_path)
    assert "claims_policies:" in content
    assert "homelab_default:" in content
    assert "vaultwarden:" in content
    assert "claims_policy: 'vaultwarden'" in content
    assert "- 'vaultwarden'" in content


def test_authelia_komodo_oidc_client_redirect(tmp_path):
    content = _generate_authelia(tmp_path)
    assert "client_id: 'komodo'" in content
    assert "https://komodo.test.example.com/auth/oidc/callback" in content
    assert "mgmt.test.example.com" not in content


def test_authelia_yaml_loadable_structure(tmp_path):
    content = _generate_authelia(tmp_path)
    data = yaml.safe_load(content)
    assert data["authentication_backend"]["ldap"]["implementation"] == "lldap"
    assert data["authentication_backend"]["password_reset"]["disable"] is False
    assert data["access_control"]["default_policy"] == "deny"
    assert len(data["identity_providers"]["oidc"]["hmac_secret"]) >= 64
    assert len(data["access_control"]["rules"]) >= 2
    komodo = next(c for c in data["identity_providers"]["oidc"]["clients"] if c["client_id"] == "komodo")
    assert komodo["redirect_uris"] == ["https://komodo.test.example.com/auth/oidc/callback"]
    assert komodo["client_secret"].startswith("$argon2id$")
    assert komodo["authorization_policy"] == "two_factor"
    assert komodo["require_pkce"] is True
    assert komodo["pkce_challenge_method"] == "S256"
    assert komodo["token_endpoint_auth_method"] == "client_secret_basic"


def test_authelia_notifier_uses_declared_mailserver_integration(tmp_path):
    content = _generate_authelia(tmp_path, email=True)
    data = yaml.safe_load(content)

    assert data["notifier"]["smtp"]["address"] == "smtp://mailserver:25"
    assert data["notifier"]["smtp"]["timeout"] == "15s"
    assert data["notifier"]["smtp"]["identifier"] == "auth.test.example.com"
    assert data["notifier"]["smtp"]["disable_require_tls"] is True
    assert "filesystem" not in data["notifier"]


def test_authelia_notifier_falls_back_to_filesystem_without_email(tmp_path):
    content = _generate_authelia(tmp_path, email=False)
    data = yaml.safe_load(content)

    assert data["notifier"]["filesystem"]["filename"] == "/config/notification.txt"
    assert "smtp" not in data["notifier"]


def test_authelia_clients_follow_enabled_manifests(tmp_path):
    content = _generate_authelia(tmp_path, cloud=False)
    data = yaml.safe_load(content)

    assert [client["client_id"] for client in data["identity_providers"]["oidc"]["clients"]] == ["komodo", "grafana"]


def test_sssd_template_uses_ldap_bind_account(tmp_path):
    from jinja2 import Environment, FileSystemLoader

    env = Environment(loader=FileSystemLoader("automation/ansible/roles/ldap_client/templates"))
    rendered = env.get_template("sssd.conf.j2").render(
        service_ips={"lldap": "10.10.10.10"},
        lldap_base_dn="dc=test,dc=example,dc=com",
        lldap_bind_password="bind-secret",
        ldap_offline_expiration=7,
    )
    assert "cn=ldap-bind,ou=people,dc=test,dc=example,dc=com" in rendered
    assert "ldap_uri = ldap://10.10.10.10:3890" in rendered
    assert "ldap_default_authtok = bind-secret" in rendered
    assert "enumerate = false" in rendered
    assert "offline_credentials_expiration = 7" in rendered
    assert "[pam]" in rendered
    assert "cn=admin" not in rendered
