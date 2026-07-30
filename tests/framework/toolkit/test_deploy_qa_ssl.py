"""Cloudflare SSL QA fails closed when provider state is unverifiable."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from toolkit.core.config.config import Config, DNSConfig
from toolkit.core.deploy.deploy_qa import _check_cloudflare_ssl


def test_ssl_api_denied_is_unverified_and_fails(tmp_path: Path):
    cfg = Config(domain="example.com", dns=DNSConfig())
    logs: list[str] = []

    client = MagicMock()
    client._zone_id = "zone"
    client.get_zone_setting.side_effect = RuntimeError("HTTP 403 Forbidden")

    with patch("toolkit.core.secrets.secrets.load_secrets_plaintext", return_value={"CLOUDFLARE_API_TOKEN": "x"}):
        with patch("toolkit.core.ops.dns.CloudflareDNS", return_value=client):
            ok = _check_cloudflare_ssl(cfg, tmp_path, logs.append)

    assert ok is False
    assert any("unverified" in ln for ln in logs)
    assert not any("assuming Full" in ln for ln in logs)


def test_ssl_without_api_token_is_unverified_and_fails(tmp_path: Path):
    cfg = Config(domain="example.com", dns=DNSConfig())
    logs: list[str] = []

    with patch("toolkit.core.secrets.secrets.load_secrets_plaintext", return_value={}):
        ok = _check_cloudflare_ssl(cfg, tmp_path, logs.append)

    assert ok is False
    assert logs == ["Cloudflare SSL: unverified (Cloudflare API token is not configured)"]


def test_ssl_full_mode_is_verified(tmp_path: Path):
    cfg = Config(domain="example.com", dns=DNSConfig())
    logs: list[str] = []
    client = MagicMock()
    client._zone_id = "zone"
    client.get_zone_setting.return_value = "full"

    with patch("toolkit.core.secrets.secrets.load_secrets_plaintext", return_value={"CLOUDFLARE_API_TOKEN": "x"}):
        with patch("toolkit.core.ops.dns.CloudflareDNS", return_value=client):
            ok = _check_cloudflare_ssl(cfg, tmp_path, logs.append)

    assert ok is True
    assert logs == ["Cloudflare SSL mode: full"]
