from unittest.mock import Mock

from toolkit.core.config.config import Config, NotificationsConfig
from toolkit.core.deploy.deploy_notify import (
    build_deploy_notification_body,
    resolve_deploy_notify_post_url,
    resolve_deploy_notify_url,
    send_deploy_notification,
)
from toolkit.services.ntfy.client import normalize_ntfy_url


def test_normalize_ntfy_url_from_topic():
    assert normalize_ntfy_url("my-topic") == "https://ntfy.sh/my-topic"


def test_normalize_ntfy_url_full():
    assert normalize_ntfy_url("https://ntfy.sh/homelab") == "https://ntfy.sh/homelab"


def test_resolve_deploy_notify_url_prefers_secret():
    cfg = Config(notifications=NotificationsConfig(deploy_ntfy_url="https://ntfy.sh/config"))
    assert resolve_deploy_notify_url(cfg, {"DEPLOY_NTFY_URL": "https://ntfy.sh/secret"}) == ("https://ntfy.sh/secret")


def test_first_party_ntfy_url_uses_internal_endpoint(monkeypatch):
    cfg = Config(domain="example.com")
    monkeypatch.setattr(
        "toolkit.services.ntfy.client.resolve_infra_ntfy_url",
        lambda _cfg: "http://10.0.0.2:8090",
    )

    assert (
        resolve_deploy_notify_post_url(cfg, "https://ntfy.example.com/deploy-topic")
        == "http://10.0.0.2:8090/deploy-topic"
    )


def test_external_ntfy_url_remains_public():
    cfg = Config(domain="example.com")

    assert resolve_deploy_notify_post_url(cfg, "https://ntfy.sh/deploy-topic") == ("https://ntfy.sh/deploy-topic")


def test_ntfy_sh_failure_falls_back_to_internal_service(monkeypatch):
    cfg = Config(domain="example.com")
    monkeypatch.delenv("HOMELAB_NTFY_URL", raising=False)
    monkeypatch.delenv("HOMELAB_CONTROLLER_ROLE", raising=False)
    monkeypatch.setattr(
        "toolkit.services.ntfy.client.resolve_infra_ntfy_url",
        lambda _cfg: "http://10.0.0.2:8090",
    )
    post_mock = Mock(side_effect=[False, True])
    monkeypatch.setattr("toolkit.core.deploy.deploy_notify.post_ntfy_url", post_mock)

    assert send_deploy_notification(
        cfg,
        {"DEPLOY_NTFY_URL": "deploy-topic"},
        success=True,
        message="Done",
        notification_type="positive",
        step_status={"verify": "ok"},
    )
    assert [entry.args[0] for entry in post_mock.call_args_list] == [
        "https://ntfy.sh/deploy-topic",
        "http://10.0.0.2:8090/deploy-topic",
    ]
    assert post_mock.call_args_list[1].kwargs["trust_env"] is False


def test_ntfy_sh_fallback_prefers_declared_service_integration(monkeypatch):
    cfg = Config(domain="example.com")
    monkeypatch.delenv("HOMELAB_CONTROLLER_ROLE", raising=False)
    monkeypatch.setenv("HOMELAB_NTFY_URL", "http://ntfy:80")
    post_mock = Mock(side_effect=[False, True])
    monkeypatch.setattr("toolkit.core.deploy.deploy_notify.post_ntfy_url", post_mock)

    assert send_deploy_notification(
        cfg,
        {"DEPLOY_NTFY_URL": "deploy-topic"},
        success=True,
        message="Done",
        notification_type="positive",
        step_status={"verify": "ok"},
    )
    assert post_mock.call_args_list[1].args[0] == "http://ntfy:80/deploy-topic"


def test_ntfy_sh_fallback_uses_local_dns_for_controller(monkeypatch):
    cfg = Config(domain="example.com")
    monkeypatch.setenv("HOMELAB_NTFY_URL", "http://172.31.89.3:80")
    monkeypatch.setenv("HOMELAB_CONTROLLER_ROLE", "local")
    monkeypatch.setattr(
        "toolkit.services.ntfy.client.resolve_local_ntfy_base",
        lambda: "http://ntfy:80",
    )
    post_mock = Mock(side_effect=[False, True])
    monkeypatch.setattr("toolkit.core.deploy.deploy_notify.post_ntfy_url", post_mock)

    assert send_deploy_notification(
        cfg,
        {"DEPLOY_NTFY_URL": "deploy-topic"},
        success=True,
        message="Done",
        notification_type="positive",
        step_status={"verify": "ok"},
    )
    assert post_mock.call_args_list[1].args[0] == "http://ntfy:80/deploy-topic"


def test_build_deploy_notification_no_secrets():
    cfg = Config(domain="example.com")
    title, body, priority = build_deploy_notification_body(
        cfg,
        success=True,
        message="Done",
        notification_type="positive",
        step_status={"preflight": "ok", "generate": "ok"},
    )
    assert "example.com" in title
    assert "password" not in body.lower()
    assert priority == "default"
