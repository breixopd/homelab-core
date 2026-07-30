import pytest
from toolkit.core.identity.service_groups import default_user_groups_for_enabled_services, validate_service_groups


class _Services:
    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)

    def enabled(self, name: str) -> bool:
        return bool(getattr(self, name, True))


def test_default_user_groups_never_grant_directory_or_operator_privileges():
    svc = _Services(management=True, media=True, cloud=True)
    assert default_user_groups_for_enabled_services(svc) == ["homelab-media", "homelab-cloud"]


def test_service_group_validation_allows_explicit_admin_but_rejects_lldap_roles():
    assert validate_service_groups(["homelab-media", "homelab-admin"]) == ["homelab-media", "homelab-admin"]
    with pytest.raises(ValueError, match="unsupported"):
        validate_service_groups(["lldap_password_manager"])
