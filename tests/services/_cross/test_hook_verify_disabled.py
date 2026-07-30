"""Fast verify branches (disabled services / early exits)."""

from __future__ import annotations

from pathlib import Path

from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.ops.hook_verify import _check_mail_dns_records
from toolkit.services.tdarr.plugin import _check_tdarr_flow_assets, _check_tdarr_flows


def test_mail_dns_records_skip_when_email_disabled(tmp_path: Path):
    cfg = Config(domain="example.com", services=ServicesConfig(email=False))
    secrets: dict[str, str] = {}
    mail = _check_mail_dns_records(cfg, secrets, tmp_path)
    assert mail.passed and "not enabled" in mail.detail


def test_mail_dns_records_require_every_expected_record(tmp_path: Path, monkeypatch):
    cfg = Config(domain="example.com", services=ServicesConfig(email=True))
    monkeypatch.setattr("toolkit.core.ops.dns.resolve_public_dns_ip", lambda _cfg: ("1.2.3.4", "test"))
    monkeypatch.setattr("toolkit.services.mailserver.bootstrap.fetch_dms_dkim_txt", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(
        "toolkit.core.ops.dns.email_dns_records",
        lambda *_args, **_kwargs: [
            type("Record", (), {"name": "example.com", "type": "MX"})(),
            type("Record", (), {"name": "example.com", "type": "TXT"})(),
        ],
    )

    calls = 0

    def fake_host(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        return type("Process", (), {"returncode": 0 if calls == 1 else 1, "stdout": "ok", "stderr": ""})()

    monkeypatch.setattr("toolkit.core.ops.hook_verify.subprocess.run", fake_host)

    check = _check_mail_dns_records(cfg, {}, tmp_path)

    assert not check.passed
    assert check.detail == "1/2 record types resolve"


def test_tdarr_flows_are_verified_without_dri(tmp_path: Path, monkeypatch):
    cfg = Config(domain="example.com", services=ServicesConfig(media=True))
    monkeypatch.setattr(
        "toolkit.services.sdk.docker_curl",
        lambda *_args, **_kwargs: (0, '[{"name":"Homelab flow"}]'),
    )

    check = _check_tdarr_flows(cfg, "10.0.0.11", tmp_path)
    assert check.passed
    assert check.detail == "1 flow(s) configured"


def test_tdarr_flow_assets_use_supported_search_api(tmp_path: Path, monkeypatch):
    cfg = Config(domain="example.com", services=ServicesConfig(media=True))
    monkeypatch.setattr(
        "toolkit.services.sdk.docker_curl",
        lambda *_args, **_kwargs: (0, '[[{"name":"Community template"}],"Community"]'),
    )

    check = _check_tdarr_flow_assets(cfg, "10.0.0.11", tmp_path)

    assert check.passed
    assert check.detail == "1 community flow template(s)"
