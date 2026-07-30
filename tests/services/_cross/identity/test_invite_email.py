from __future__ import annotations

from email import message_from_string
from unittest.mock import MagicMock, patch

from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.identity.invite_email import (
    build_welcome_email_context,
    deliver_welcome_email,
    invite_activate_url,
    send_welcome_email,
)
from toolkit.core.identity.service_groups import invite_sections_for_groups
from toolkit.core.manifest.catalog import ServiceCatalog
from toolkit.core.manifest.schema import ServiceManifest


def test_invite_sections_cloud_includes_vault_signup():
    cfg = Config(domain="example.com", services=ServicesConfig(cloud=True, media=True))
    sections = invite_sections_for_groups(
        cfg,
        ["homelab-cloud", "homelab-media"],
    )
    labels = {card.label for tier, cards in sections for card in cards}
    assert "Vaultwarden" in labels
    vault = next(card for tier, cards in sections for card in cards if card.label == "Vaultwarden")
    assert "/#/signup" in vault.url
    gitea = next(card for tier, cards in sections for card in cards if card.label == "Gitea")
    assert gitea.url == "https://git.example.com"


def test_custom_service_contributes_invite_card_without_core_change() -> None:
    manifest = ServiceManifest.model_validate(
        {
            "name": "example",
            "label": "Example",
            "description": "Custom cloud service",
            "icon": "box",
            "category": "cloud",
            "placement": "apps",
            "priority": 50,
            "routes": [
                {
                    "subdomain": "custom",
                    "upstream": "example:8080",
                    "exposure": "private",
                    "auth": {"mode": "forward_auth"},
                }
            ],
            "identity": {
                "invite": {
                    "group": "homelab-cloud",
                    "priority": 10,
                    "path": "/welcome",
                    "blurb": "Custom app",
                    "sign_in": "Sign in with Authelia.",
                }
            },
        }
    )

    sections = invite_sections_for_groups(
        Config(domain="example.com", services={"cloud": True}),
        ["homelab-cloud"],
        catalog=ServiceCatalog((manifest,)),
    )

    assert [(name, [(card.label, card.url) for card in cards]) for name, cards in sections] == [
        ("Cloud", [("Example", "https://custom.example.com/welcome")])
    ]


def test_welcome_email_context_includes_activate_url():
    cfg = Config(domain="example.com", services=ServicesConfig(cloud=True, email=True))
    secrets = {"INVITE_TOKEN_SECRET": "x" * 48}
    mock_redis = MagicMock()
    mock_redis.setex = MagicMock()
    with patch("toolkit.core.identity.invite_token._redis_client", return_value=mock_redis):
        ctx = build_welcome_email_context(
            cfg,
            secrets,
            email="u@example.com",
            user_id="u",
            display_name="User",
            groups=["homelab-media"],
        )
    assert "activate_url" in ctx
    assert ctx["activate_url"].startswith("https://homelab.example.com/invite/activate?token=")
    assert invite_activate_url(cfg, "abc").endswith("token=abc")


def test_send_welcome_email_single_smtp_message():
    cfg = Config(
        domain="example.com",
        services=ServicesConfig(cloud=True, email=True),
    )
    secrets = {"INVITE_TOKEN_SECRET": "x" * 48}
    mock_redis = MagicMock()
    mock_redis.setex = MagicMock()
    sent: list[tuple[str, list[str], str]] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float = 20):
            self.host = host

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def sendmail(self, from_addr: str, to_addrs: list[str], message: str) -> None:
            sent.append((from_addr, to_addrs, message))

    with (
        patch("toolkit.core.identity.invite_token._redis_client", return_value=mock_redis),
        patch("toolkit.core.identity.invite_email._render_template", side_effect=lambda name, **ctx: f"<{name}>"),
        patch("toolkit.core.identity.invite_email.smtplib.SMTP", FakeSMTP),
    ):
        logs = send_welcome_email(
            cfg,
            secrets,
            email="u@example.com",
            user_id="u",
            display_name="User",
            groups=["homelab-media"],
            delivery_id="identity-job-1234",
        )

    assert any("sent" in line for line in logs)
    assert len(sent) == 1
    from_addr, to_addrs, raw = sent[0]
    assert from_addr == "homelab@example.com"
    assert to_addrs == ["u@example.com"]
    assert "Subject:" in raw
    assert "Message-ID: <" in raw
    assert "To: u@example.com" in raw
    assert raw.count("Content-Type: text/plain") == 1
    assert raw.count("Content-Type: text/html") == 1


def test_welcome_delivery_returns_typed_failure_without_exception_details() -> None:
    cfg = Config(domain="example.com", services=ServicesConfig(email=True))
    secrets = {"INVITE_TOKEN_SECRET": "x" * 48}

    with patch("toolkit.core.identity.invite_email._render_template", side_effect=RuntimeError("secret-canary")):
        result = deliver_welcome_email(
            cfg,
            secrets,
            email="u@example.com",
            user_id="u",
            display_name="User",
            groups=["homelab-media"],
        )

    assert result.status == "failed"
    assert result.reason == "template"
    assert "secret-canary" not in repr(result)


def test_replayed_delivery_uses_identical_message_and_activation_link() -> None:
    cfg = Config(domain="example.com", services=ServicesConfig(email=True))
    secrets = {"INVITE_TOKEN_SECRET": "x" * 48}
    sent: list[str] = []

    class FakeSMTP:
        def __init__(self, host: str, port: int, timeout: float = 20):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def sendmail(self, _from: str, _to: list[str], message: str) -> None:
            sent.append(message)

    with (
        patch("toolkit.core.identity.invite_email.create_invite_token", return_value="stable-token"),
        patch(
            "toolkit.core.identity.invite_email._render_template",
            side_effect=lambda name, **ctx: f"{name}:{ctx['activate_url']}",
        ),
        patch("toolkit.core.identity.invite_email.smtplib.SMTP", FakeSMTP),
    ):
        for _attempt in range(2):
            result = deliver_welcome_email(
                cfg,
                secrets,
                email="u@example.com",
                user_id="u",
                display_name="User",
                groups=["homelab-media"],
                delivery_id="identity-job-1234",
            )
            assert result.status == "sent"

    assert sent[0] == sent[1]
    parsed = message_from_string(sent[0])
    bodies = [
        part.get_payload(decode=True).decode("utf-8") for part in parsed.walk() if part.get_content_maintype() == "text"
    ]
    assert all("stable-token" in body for body in bodies)
