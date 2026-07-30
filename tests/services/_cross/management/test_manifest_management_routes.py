from __future__ import annotations

from pathlib import Path

import yaml
from toolkit.core.manifest.schema import ServiceManifest

_SERVICES = Path(__file__).resolve().parents[4] / "toolkit" / "services"


def _manifest(name: str) -> ServiceManifest:
    raw = yaml.safe_load((_SERVICES / name / "service.yaml").read_text(encoding="utf-8"))
    return ServiceManifest.model_validate(raw)


def test_management_and_notification_routes_have_explicit_policy() -> None:
    expected = {
        "adguard": ("private", "forward_auth"),
        "authelia": ("public", "native"),
        "grafana": ("private", "oidc"),
        "homelab-ui": ("public", "forward_auth"),
        "komodo-core": ("private", "oidc"),
        "lldap": ("private", "forward_auth"),
        "portal": ("public", "forward_auth"),
        "prometheus": ("private", "forward_auth"),
        "ntfy": ("public", "native"),
        "uptime-kuma": ("public", "native"),
    }

    for name, (exposure, auth_mode) in expected.items():
        defaults = [route for route in _manifest(name).routes if route.match is None]
        assert len(defaults) == 1, name
        assert defaults[0].exposure == exposure, name
        assert defaults[0].auth.mode == auth_mode, name


def test_homelab_invite_capability_precedes_authenticated_default() -> None:
    routes = _manifest("homelab-ui").routes
    invite = next(route for route in routes if route.match and "/invite/" in route.match.paths)
    default = next(route for route in routes if route.match is None)

    assert invite.match is not None
    assert invite.match.kind == "prefix"
    assert invite.auth.mode == "native"
    assert routes.index(invite) < routes.index(default)
    assert default.auth.mode == "forward_auth"


def test_management_oidc_contracts_are_complete() -> None:
    grafana = _manifest("grafana").oidc
    komodo = _manifest("komodo-core").oidc

    assert grafana is not None
    assert grafana.client_id == "grafana"
    assert grafana.secret_env_var == "GRAFANA_OIDC_SECRET"
    assert grafana.redirect_uris == ("https://grafana.{domain}/login/generic_oauth",)
    assert komodo is not None
    assert komodo.client_id == "komodo"
    assert komodo.secret_env_var == "KOMODO_OIDC_CLIENT_SECRET"
    assert komodo.redirect_uris == ("https://komodo.{domain}/auth/oidc/callback",)
    assert komodo.secret_env_var in {secret.name for secret in _manifest("komodo-core").required_secrets}


def test_stateful_management_manifests_use_strict_data_lists() -> None:
    assert _manifest("grafana").data_specs[0].source_env == "GRAFANA_DATA_SOURCE"
    assert _manifest("uptime-kuma").data_specs[0].target == "/app/data"
    assert _manifest("crowdsec").data_specs[0].target == "/var/lib/crowdsec/data"
    assert _manifest("crowdsec").routes == ()
