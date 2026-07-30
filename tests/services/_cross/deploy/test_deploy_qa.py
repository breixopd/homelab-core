from __future__ import annotations

from pathlib import Path

from toolkit.core.config.config import Config, DNSConfig, ServicesConfig
from toolkit.core.deploy.deploy_qa import run_deploy_qa
from toolkit.core.ops.hook_verify import HookVerifyResult
from toolkit.core.ops.verify import VerifyResult


def test_run_deploy_qa_aggregates_sections(monkeypatch, tmp_path: Path):
    cfg = Config(
        domain="example.com",
        dns=DNSConfig(proxy_enabled=False),
        services=ServicesConfig(management=True, security=False, media=False),
    )
    verify = VerifyResult(vm="infra", docker_ok=True, compose_ok=True)

    monkeypatch.setattr("toolkit.core.deploy.deploy_qa.load_secrets_plaintext", lambda _p: {})
    monkeypatch.setattr("toolkit.core.deploy.deploy_qa.verify_all", lambda *_a, **_k: {"infra": verify})
    monkeypatch.setattr(
        "toolkit.core.deploy.deploy_qa.verify_hooks",
        lambda *_a, **_k: HookVerifyResult(),
    )
    monkeypatch.setattr("toolkit.core.deploy.deploy_qa._check_custom_images", lambda *_a, **_k: True)

    result = run_deploy_qa(tmp_path, cfg)

    assert result.ok
    assert result.sections["verify"]
    assert result.sections["hooks"]
    assert result.sections["custom_images"]
    assert "grafana" not in result.sections
    assert "wazuh" not in result.sections
    assert "vpn" not in result.sections
    assert any("Container & URL verify" in line for line in result.logs)


def test_run_deploy_qa_fails_when_hooks_fail(monkeypatch, tmp_path: Path):
    from toolkit.core.ops.hook_verify import VerifyCheck

    cfg = Config(domain="example.com", services=ServicesConfig(security=False, media=False))
    verify = VerifyResult(vm="infra", docker_ok=True, compose_ok=True)
    hook_result = HookVerifyResult(checks=[VerifyCheck("prowlarr", "indexers", False, "API unreachable")])

    monkeypatch.setattr("toolkit.core.deploy.deploy_qa.load_secrets_plaintext", lambda _p: {})
    monkeypatch.setattr("toolkit.core.deploy.deploy_qa.verify_all", lambda *_a, **_k: {"infra": verify})
    monkeypatch.setattr("toolkit.core.deploy.deploy_qa.verify_hooks", lambda *_a, **_k: hook_result)
    monkeypatch.setattr("toolkit.core.deploy.deploy_qa._check_cloudflare_ssl", lambda *_a, **_k: True)
    monkeypatch.setattr("toolkit.core.deploy.deploy_qa._check_custom_images", lambda *_a, **_k: True)

    result = run_deploy_qa(tmp_path, cfg)

    assert not result.ok
    assert not result.sections["hooks"]
