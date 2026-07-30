from toolkit.core.secrets.bootstrap_passwords import resolve_bootstrap_password


def test_resolve_bootstrap_password_prefers_explicit():
    secrets = {"GRAFANA_ADMIN_PASSWORD": "service-secret", "SSO_USER_PASSWORD": "owner"}
    assert resolve_bootstrap_password(secrets, "GRAFANA_ADMIN_PASSWORD") == "service-secret"


def test_resolve_bootstrap_password_uses_manifest_fallback():
    secrets = {"SSO_USER_PASSWORD": "owner"}
    assert resolve_bootstrap_password(secrets, "IMMICH_ADMIN_PASSWORD") == "owner"


def test_resolve_bootstrap_password_does_not_guess_a_fallback():
    assert resolve_bootstrap_password({"SSO_USER_PASSWORD": "owner"}, "UNKNOWN_PASSWORD") == ""
