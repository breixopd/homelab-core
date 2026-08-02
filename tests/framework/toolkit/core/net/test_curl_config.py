from __future__ import annotations

import pytest
from toolkit.core.net.curl_config import render_curl_config


def test_render_curl_config_quotes_headers_and_request_body() -> None:
    config = render_curl_config(
        "https://service.example.test/api",
        method="POST",
        headers={"Authorization": 'Bearer token\\with"quotes'},
        body='{"name":"value"}',
        timeout=12,
        insecure_tls=True,
    )

    assert 'header = "Authorization: Bearer token\\\\with\\"quotes"' in config
    assert 'data-raw = "{\\"name\\":\\"value\\"}"' in config
    assert 'request = "POST"' in config
    assert 'url = "https://service.example.test/api"' in config
    assert "insecure" in config


def test_render_curl_config_supports_a_validated_ca_bundle_path() -> None:
    config = render_curl_config(
        "https://service.example.test/api",
        headers={"Authorization": "Bearer token"},
        ca_file="/etc/service/certs/root-ca.pem",
    )

    assert 'cacert = "/etc/service/certs/root-ca.pem"' in config


def test_render_curl_config_supports_validated_cookie_files() -> None:
    config = render_curl_config(
        "https://service.example.test/api",
        cookie_file="/tmp/session.cookies",
        cookie_jar="/tmp/session.cookies",
    )

    assert 'cookie = "/tmp/session.cookies"' in config
    assert 'cookie-jar = "/tmp/session.cookies"' in config


def test_render_curl_config_applies_optional_response_limit() -> None:
    config = render_curl_config("https://service.example.test", max_response_bytes=4096)
    assert "max-filesize = 4096" in config


def test_render_curl_config_keeps_response_limit_opt_in() -> None:
    config = render_curl_config("https://service.example.test")
    assert "max-filesize" not in config


@pytest.mark.parametrize("limit", [0, -1, True, "4096"])
def test_render_curl_config_rejects_invalid_response_limit(limit) -> None:
    with pytest.raises(ValueError):
        render_curl_config("https://service.example.test", max_response_bytes=limit)


@pytest.mark.parametrize(
    ("url", "headers"),
    [
        ("ftp://service.example.test", {}),
        ("https://operator:password@service.example.test", {}),
        ("https://service.example.test:99999", {}),
        ("https://service.example.test/\nunsafe", {}),
        ("https://service.example.test", {"Bad Header": "value"}),
        ("https://service.example.test", {"Authorization": "Bearer value\r\nInjected: yes"}),
    ],
)
def test_render_curl_config_rejects_unsafe_request_values(url: str, headers: dict[str, str]) -> None:
    with pytest.raises(ValueError):
        render_curl_config(url, headers=headers)


@pytest.mark.parametrize("ca_file", ["relative-ca.pem", "/etc/service/unsafe\nca.pem"])
def test_render_curl_config_rejects_unsafe_ca_bundle_paths(ca_file: str) -> None:
    with pytest.raises(ValueError):
        render_curl_config("https://service.example.test", ca_file=ca_file)


@pytest.mark.parametrize("cookie_file", ["relative.cookies", "/tmp/unsafe\ncookies"])
def test_render_curl_config_rejects_unsafe_cookie_paths(cookie_file: str) -> None:
    with pytest.raises(ValueError):
        render_curl_config("https://service.example.test", cookie_file=cookie_file)
