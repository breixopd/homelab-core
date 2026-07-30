from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from toolkit.core.config.config import Config, NotificationsConfig, ServicesConfig, SMTPNotificationConfig
from toolkit.core.ops.notifications import _resolve_smtp_transport, send_email


def test_auto_smtp_uses_manifest_declared_mailserver_endpoint() -> None:
    cfg = Config(services=ServicesConfig(email=True))

    transport = _resolve_smtp_transport(cfg, {})

    assert transport is not None
    assert transport.host == cfg.node_ip(cfg.control_node)
    assert transport.port == 25
    assert transport.starttls is False


def test_auto_smtp_is_disabled_without_mailserver() -> None:
    cfg = Config(services=ServicesConfig(email=False))

    assert _resolve_smtp_transport(cfg, {}) is None


def test_external_smtp_uses_encrypted_password_secret() -> None:
    cfg = Config(
        notifications=NotificationsConfig(
            smtp=SMTPNotificationConfig(
                mode="external",
                host="smtp.example.com",
                port=2525,
                username="operator@example.com",
                password_secret="OPERATOR_SMTP_PASSWORD",
                from_address="homelab@example.com",
            )
        )
    )

    transport = _resolve_smtp_transport(cfg, {"OPERATOR_SMTP_PASSWORD": "secret"})

    assert transport is not None
    assert transport.host == "smtp.example.com"
    assert transport.password == "secret"
    assert transport.starttls is True


def test_external_smtp_configuration_requires_complete_auth_pair() -> None:
    with pytest.raises(ValidationError, match="both username and password_secret"):
        SMTPNotificationConfig(mode="external", host="smtp.example.com", username="operator")


def test_send_email_uses_typed_external_transport(tmp_path) -> None:
    cfg = Config(
        domain="example.com",
        email="operator@example.com",
        notifications=NotificationsConfig(
            smtp=SMTPNotificationConfig(
                mode="external",
                host="smtp.example.com",
                username="operator",
                password_secret="SMTP_PASSWORD",
            )
        ),
    )
    server = MagicMock()
    smtp = MagicMock()
    smtp.return_value.__enter__.return_value = server

    with (
        patch("toolkit.core.ops.notifications.load_config", return_value=cfg),
        patch("toolkit.core.ops.notifications._load_secrets", return_value={"SMTP_PASSWORD": "secret"}),
        patch("toolkit.core.ops.notifications.smtplib.SMTP", smtp),
    ):
        send_email("Status", "All systems operational", tmp_path)

    smtp.assert_called_once_with("smtp.example.com", 587, timeout=15)
    server.starttls.assert_called_once_with()
    server.login.assert_called_once_with("operator", "secret")
    assert server.sendmail.call_args.args[:2] == ("noreply@example.com", ["operator@example.com"])
