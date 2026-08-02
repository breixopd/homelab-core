from __future__ import annotations

import smtplib
from concurrent.futures import TimeoutError as FuturesTimeoutError
from unittest.mock import MagicMock, patch

import pytest
from pydantic import ValidationError
from toolkit.core.config.config import Config, NotificationsConfig, ServicesConfig, SMTPNotificationConfig
from toolkit.core.ops.notifications import SMTPTransport, probe_smtp_transport, resolve_smtp_transport, send_email


def test_auto_smtp_uses_manifest_declared_mailserver_endpoint() -> None:
    cfg = Config(services=ServicesConfig(email=True))

    transport = resolve_smtp_transport(cfg, {})

    assert transport is not None
    assert transport.host == cfg.node_ip(cfg.control_node)
    assert transport.port == 25
    assert transport.starttls is False


def test_auto_smtp_is_disabled_without_mailserver() -> None:
    cfg = Config(services=ServicesConfig(email=False))

    assert resolve_smtp_transport(cfg, {}) is None


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

    transport = resolve_smtp_transport(cfg, {"OPERATOR_SMTP_PASSWORD": "secret"})

    assert transport is not None
    assert transport.host == "smtp.example.com"
    assert transport.password == "secret"
    assert transport.starttls is True


def test_external_smtp_configuration_requires_complete_auth_pair() -> None:
    with pytest.raises(ValidationError, match="both username and password_secret"):
        SMTPNotificationConfig(mode="external", host="smtp.example.com", username="operator")


def test_external_smtp_rejects_unencrypted_transport() -> None:
    with pytest.raises(ValidationError, match="requires STARTTLS or implicit TLS"):
        SMTPNotificationConfig(
            mode="external",
            host="smtp.example.com",
            port=25,
            starttls=False,
        )


def test_external_smtp_rejects_starttls_on_implicit_tls_port() -> None:
    with pytest.raises(ValidationError, match="port 465 requires implicit TLS"):
        SMTPNotificationConfig(
            mode="external",
            host="smtp.example.com",
            port=465,
            starttls=True,
        )


def test_smtp_probe_performs_ehlo_starttls_and_auth_without_sending() -> None:
    server = MagicMock()
    server.ehlo.return_value = (250, b"ok")
    server.starttls.return_value = (220, b"ready")
    server.login.return_value = (235, b"authenticated")
    server.mail.return_value = (250, b"sender accepted")
    smtp = MagicMock()
    smtp.return_value.__enter__.return_value = server
    transport = SMTPTransport(
        host="smtp.example.com",
        port=587,
        from_address="noreply@example.com",
        starttls=True,
        username="operator@example.com",
        password="secret",
    )

    with (
        patch("toolkit.core.ops.notifications.socket.getaddrinfo", return_value=[("address",)]),
        patch("toolkit.core.ops.notifications.smtplib.SMTP", smtp),
    ):
        result = probe_smtp_transport(transport)

    assert result.ok is True
    assert result.stage == "ready"
    server.sendmail.assert_not_called()
    server.starttls.assert_called_once()
    assert server.starttls.call_args.kwargs["context"].check_hostname is True
    server.login.assert_called_once_with("operator@example.com", "secret")
    server.mail.assert_called_once_with("noreply@example.com")
    server.rset.assert_called_once_with()


def test_smtp_probe_uses_verified_implicit_tls_on_port_465() -> None:
    server = MagicMock()
    server.ehlo.return_value = (250, b"ok")
    server.mail.return_value = (250, b"sender accepted")
    smtp_ssl = MagicMock()
    smtp_ssl.return_value.__enter__.return_value = server
    transport = SMTPTransport(
        host="smtp.example.com",
        port=465,
        from_address="noreply@example.com",
        implicit_tls=True,
    )

    with (
        patch("toolkit.core.ops.notifications.socket.getaddrinfo", return_value=[("address",)]),
        patch("toolkit.core.ops.notifications.smtplib.SMTP_SSL", smtp_ssl),
    ):
        result = probe_smtp_transport(transport)

    assert result.ok is True
    smtp_ssl.assert_called_once()
    assert smtp_ssl.call_args.kwargs["context"].check_hostname is True


def test_smtp_probe_bounds_dns_lookup() -> None:
    lookup = MagicMock()
    lookup.result.side_effect = FuturesTimeoutError
    transport = SMTPTransport(
        host="smtp.example.com",
        port=587,
        from_address="noreply@example.com",
        starttls=True,
    )

    with patch("toolkit.core.ops.notifications._DNS_EXECUTOR.submit", return_value=lookup):
        result = probe_smtp_transport(transport, timeout=0.1)

    assert result == result.__class__(False, "dns", "SMTP DNS lookup timed out")
    lookup.cancel.assert_called_once_with()


def test_smtp_probe_fails_closed_when_authentication_is_incomplete() -> None:
    transport = SMTPTransport(
        host="smtp.example.com",
        port=587,
        from_address="noreply@example.com",
        username="operator",
    )

    result = probe_smtp_transport(transport)

    assert result == result.__class__(False, "auth", "SMTP authentication requires username and password")


def test_smtp_probe_redacts_password_and_userinfo_from_errors() -> None:
    transport = SMTPTransport(
        host="smtp.example.com",
        port=587,
        from_address="noreply@example.com",
        username="operator",
        password="super-secret",
    )

    with patch(
        "toolkit.core.ops.notifications.socket.getaddrinfo",
        side_effect=OSError("smtp://operator:super-secret@smtp.example.com password=super-secret"),
    ):
        result = probe_smtp_transport(transport)

    assert result.ok is False
    assert "super-secret" not in result.detail
    assert "<redacted>" in result.detail


def test_smtp_probe_reports_authentication_stage_for_auth_exception() -> None:
    server = MagicMock()
    server.ehlo.return_value = (250, b"ok")
    server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"password=super-secret")
    smtp = MagicMock()
    smtp.return_value.__enter__.return_value = server
    transport = SMTPTransport(
        host="smtp.example.com",
        port=25,
        from_address="noreply@example.com",
        username="operator",
        password="super-secret",
    )

    with (
        patch("toolkit.core.ops.notifications.socket.getaddrinfo", return_value=[("address",)]),
        patch("toolkit.core.ops.notifications.smtplib.SMTP", smtp),
    ):
        result = probe_smtp_transport(transport)

    assert result.ok is False
    assert result.stage == "auth"
    assert "super-secret" not in result.detail


def test_smtp_probe_reports_rejected_sender_without_sending() -> None:
    server = MagicMock()
    server.ehlo.return_value = (250, b"ok")
    server.mail.return_value = (550, b"sender rejected")
    smtp = MagicMock()
    smtp.return_value.__enter__.return_value = server
    transport = SMTPTransport(
        host="smtp.example.com",
        port=25,
        from_address="wrong@example.com",
    )

    with (
        patch("toolkit.core.ops.notifications.socket.getaddrinfo", return_value=[("address",)]),
        patch("toolkit.core.ops.notifications.smtplib.SMTP", smtp),
    ):
        result = probe_smtp_transport(transport)

    assert result.stage == "envelope"
    assert result.ok is False
    server.sendmail.assert_not_called()


def test_send_email_uses_verified_starttls_context(tmp_path) -> None:
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

    assert server.starttls.call_args.kwargs["context"].check_hostname is True


def test_send_email_uses_verified_implicit_tls_client(tmp_path) -> None:
    cfg = Config(
        domain="example.com",
        email="operator@example.com",
        notifications=NotificationsConfig(
            smtp=SMTPNotificationConfig(
                mode="external",
                host="smtp.example.com",
                port=465,
                starttls=False,
            )
        ),
    )
    server = MagicMock()
    smtp_ssl = MagicMock()
    smtp_ssl.return_value.__enter__.return_value = server

    with (
        patch("toolkit.core.ops.notifications.load_config", return_value=cfg),
        patch("toolkit.core.ops.notifications._load_secrets", return_value={}),
        patch("toolkit.core.ops.notifications.smtplib.SMTP_SSL", smtp_ssl),
    ):
        send_email("Status", "All systems operational", tmp_path)

    smtp_ssl.assert_called_once()
    assert smtp_ssl.call_args.kwargs["context"].check_hostname is True


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
    server.starttls.assert_called_once()
    assert server.starttls.call_args.kwargs["context"].check_hostname is True
    server.login.assert_called_once_with("operator", "secret")
    assert server.sendmail.call_args.args[:2] == ("noreply@example.com", ["operator@example.com"])


def test_send_email_redacts_provider_error_from_logs(tmp_path) -> None:
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
    server.login.side_effect = smtplib.SMTPAuthenticationError(535, b"password=super-secret")
    smtp = MagicMock()
    smtp.return_value.__enter__.return_value = server

    with (
        patch("toolkit.core.ops.notifications.load_config", return_value=cfg),
        patch("toolkit.core.ops.notifications._load_secrets", return_value={"SMTP_PASSWORD": "super-secret"}),
        patch("toolkit.core.ops.notifications.smtplib.SMTP", smtp),
        patch("toolkit.core.ops.notifications.click.secho") as secho,
    ):
        send_email("Status", "All systems operational", tmp_path)

    message = secho.call_args.args[0]
    assert "super-secret" not in message
    assert "<redacted>" in message
