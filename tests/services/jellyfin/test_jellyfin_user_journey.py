from pathlib import Path
from unittest.mock import patch

from toolkit.core.config.config import Config
from toolkit.services.jellyfin.plugin import JellyfinPlugin


def test_directory_login_is_a_blocking_real_user_check(tmp_path: Path) -> None:
    plugin = JellyfinPlugin()
    cfg = Config(domain="example.com", email="brei@example.com")

    with patch(
        "toolkit.services.sdk.docker_curl",
        return_value=(0, '{"AccessToken":"user-token"}'),
    ) as request:
        check = plugin._check_directory_login(
            cfg,
            {"SSO_USER_PASSWORD": "chosen-password"},
            "10.0.0.2",
            tmp_path,
        )

    assert check.passed
    assert check.check == "directory_login"
    assert '"Username": "brei"' in request.call_args.kwargs["body"]
    assert '"Pw": "chosen-password"' in request.call_args.kwargs["body"]


def test_directory_login_fails_when_owner_password_is_unavailable(tmp_path: Path) -> None:
    check = JellyfinPlugin()._check_directory_login(
        Config(domain="example.com", email="brei@example.com"),
        {},
        "10.0.0.2",
        tmp_path,
    )

    assert not check.passed
    assert "unavailable" in check.detail
