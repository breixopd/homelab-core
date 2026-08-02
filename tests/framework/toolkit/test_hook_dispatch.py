from __future__ import annotations

from threading import Barrier
from types import SimpleNamespace

from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.ops.hook_verify import verify_hooks
from toolkit.core.verify.models import VerifyCheck


def test_verify_hooks_dispatches_plugins_to_the_manifest_owned_node(monkeypatch, tmp_path):
    """The framework dispatches generic plugin verification without service branches."""
    cfg = Config(
        domain="example.com",
        services=ServicesConfig(
            media=False,
            cloud=True,
            notifications=False,
            email=False,
            security=False,
        ),
    )
    plugin = SimpleNamespace(
        service="synthetic-service",
        runtime_address=lambda _cfg: "http://synthetic-service:8080",
        verify=lambda _cfg, _secrets, address, _root: [
            VerifyCheck("synthetic-service", "health", address == "http://synthetic-service:8080", "ok")
        ],
    )
    monkeypatch.setattr("toolkit.services.enabled_service_plugins", lambda *_args, **_kwargs: [("test", plugin)])
    monkeypatch.setattr("toolkit.core.manifest.placement.service_node", lambda *_args, **_kwargs: "apps")
    # Cross-service checks must not prevent the plugin sweep from completing.
    monkeypatch.setattr(
        "toolkit.core.ops.hook_verify._check_sssd_active", lambda *_a, **_k: VerifyCheck("sssd", "apps", True, "ok")
    )
    monkeypatch.setattr(
        "toolkit.core.ops.hook_verify._check_ldap_getent", lambda *_a, **_k: VerifyCheck("ldap", "apps", True, "ok")
    )
    monkeypatch.setattr("toolkit.core.ops.hook_verify._check_forward_auth_routes", lambda *_a, **_k: [])
    monkeypatch.setattr("toolkit.core.ops.hook_verify._check_repo_parity", lambda *_a, **_k: [])
    monkeypatch.setattr("toolkit.core.ops.monitoring_verify.verify_monitoring_stack", lambda *_a, **_k: [])
    monkeypatch.setattr(
        "toolkit.core.ops.hook_verify._check_cloudflare_public_dns_parity",
        lambda *_a, **_k: VerifyCheck("cloudflare", "public_dns", True, "ok"),
    )
    monkeypatch.setattr(
        "toolkit.core.ops.hook_verify._check_private_fqdns_not_in_cloudflare",
        lambda *_a, **_k: VerifyCheck("cloudflare", "private_dns", True, "ok"),
    )

    progress: list[str] = []
    result = verify_hooks(cfg, {}, tmp_path, vm="apps", on_progress=progress.append)

    synthetic_health = [c for c in result.checks if c.service == "synthetic-service" and c.check == "health"]
    assert synthetic_health and synthetic_health[0].passed
    assert progress == ["Verifying synthetic-service (1/1)"]


def test_verify_hooks_can_retry_only_selected_plugins(monkeypatch, tmp_path):
    cfg = Config(domain="example.com", services=ServicesConfig(email=False))
    calls: list[str] = []

    def plugin(name: str):
        return SimpleNamespace(
            service=name,
            runtime_address=lambda _cfg: "localhost",
            verify=lambda *_args: calls.append(name) or [VerifyCheck(name, "health", True, "ok")],
        )

    monkeypatch.setattr(
        "toolkit.services.enabled_service_plugins",
        lambda *_args, **_kwargs: [("test", plugin("healthy")), ("test", plugin("retry-me"))],
    )
    monkeypatch.setattr("toolkit.core.manifest.placement.service_node", lambda *_args, **_kwargs: "apps")
    monkeypatch.setattr(
        "toolkit.core.ops.hook_verify._check_sssd_active", lambda *_a, **_k: VerifyCheck("sssd", "apps", True, "ok")
    )
    monkeypatch.setattr(
        "toolkit.core.ops.hook_verify._check_ldap_getent", lambda *_a, **_k: VerifyCheck("ldap", "apps", True, "ok")
    )
    monkeypatch.setattr("toolkit.core.ops.hook_verify._check_forward_auth_routes", lambda *_a, **_k: [])
    monkeypatch.setattr("toolkit.core.ops.monitoring_verify.verify_monitoring_stack", lambda *_a, **_k: [])

    result = verify_hooks(cfg, {}, tmp_path, vm="apps", only_services=frozenset({"retry-me"}))

    assert calls == ["retry-me"]
    assert any(check.service == "retry-me" for check in result.checks)


def test_verify_hooks_runs_plugin_probes_concurrently_and_keeps_result_order(monkeypatch, tmp_path):
    cfg = Config(domain="example.com", services=ServicesConfig(email=False))
    barrier = Barrier(2, timeout=2)

    def plugin(name: str):
        def verify(*_args):
            barrier.wait()
            return [VerifyCheck(name, "health", True, "ok")]

        return SimpleNamespace(
            service=name,
            runtime_address=lambda _cfg: "localhost",
            verify=verify,
        )

    monkeypatch.setattr(
        "toolkit.services.enabled_service_plugins",
        lambda *_args, **_kwargs: [("test", plugin("first")), ("test", plugin("second"))],
    )
    monkeypatch.setattr("toolkit.core.manifest.placement.service_node", lambda *_args, **_kwargs: "apps")
    monkeypatch.setattr(
        "toolkit.core.ops.hook_verify._check_sssd_active", lambda *_a, **_k: VerifyCheck("sssd", "apps", True, "ok")
    )
    monkeypatch.setattr(
        "toolkit.core.ops.hook_verify._check_ldap_getent", lambda *_a, **_k: VerifyCheck("ldap", "apps", True, "ok")
    )
    monkeypatch.setattr("toolkit.core.ops.hook_verify._check_forward_auth_routes", lambda *_a, **_k: [])
    monkeypatch.setattr("toolkit.core.ops.monitoring_verify.verify_monitoring_stack", lambda *_a, **_k: [])

    result = verify_hooks(cfg, {}, tmp_path, vm="apps")

    plugin_checks = [check.service for check in result.checks if check.check == "health"]
    assert plugin_checks == ["first", "second"]
