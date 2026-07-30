from __future__ import annotations

from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.ops.hook_verify import verify_hooks


def test_verify_hooks_dispatches_seaweedfs_plugin_to_manifest_node(monkeypatch, tmp_path):
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
    # Cloud is enabled, so the manifest-owned apps node is active.
    seen_urls: list[tuple[str, str]] = []

    def fake_docker_curl(_cfg, vm_ip, container, url, **_kwargs):
        assert vm_ip == cfg.node_ip("apps")
        seen_urls.append((container, url))
        return 0, '{"Leader": "seaweedfs:9333"}'

    monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_docker_curl)
    monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
        lambda *_args, **_kwargs: (1, "", "test transport unavailable"),
    )
    # Forward-auth + repo-parity + cloudflare checks must not derail the sweep.
    monkeypatch.setattr("toolkit.core.ops.hook_verify._check_forward_auth_routes", lambda *_a, **_k: [])

    result = verify_hooks(cfg, {}, tmp_path, vm="apps")

    seaweed_health = [c for c in result.checks if c.service == "seaweedfs" and c.check == "cluster_leader"]
    assert seaweed_health and seaweed_health[0].passed
    assert ("seaweedfs", "http://localhost:9333/cluster/status") in seen_urls


# ── media-cache.backends — external-storage gate ────────────────────────────


class TestMediaCacheBackendsGate:
    """The standing `media-cache.backends` failure: verify reported FAIL for
    ``0 backend(s)`` whenever ``cfg.media.cache`` is on but NO external_hosts
    entry carries the ``media-cache`` service. The user's intent is that
    media-cache is an *external-storage-gated* optional service: when no
    storage host exists, the check should verify-skip (not page-able)."""

    def test_skips_when_no_external_storage_host(self, tmp_path, monkeypatch):
        """cfg.media.cache=True but external_hosts=[] → ok=True (skipped),
        NOT a page-able failure. No remote probe to ``/api/backends`` should fire."""
        from toolkit.core.config.config import Config, ServicesConfig

        # media + cloud enabled ⇒ is_multi_vm True (enabled_nodes > 1)
        cfg = Config(
            domain="example.com",
            services=ServicesConfig(media=True, cloud=True),
            service_settings={"media-cache": {"enabled": True}},
        )
        cfg.external_hosts = []  # no NAS / cache host configured

        def _explode_docker_curl(*_a, **_kw):
            raise AssertionError("docker_curl must not fire when no storage host is configured")

        monkeypatch.setattr("toolkit.services.sdk.docker_curl", _explode_docker_curl)
        monkeypatch.setattr("httpx.get", lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError()))

        from tests.helpers.plugins import load_plugin

        check = load_plugin("media-cache").check_backends(cfg, "10.10.10.11", tmp_path)

        assert check.service == "media-cache"
        assert check.check == "backends"
        assert check.passed is True  # skipped, not failed
        assert "skip" in check.detail.lower() or "no external" in check.detail.lower()

    def test_probes_api_when_external_storage_host_present(self, tmp_path, monkeypatch):
        """When an external host with `media-cache` service IS configured, the
        check must probe the ``/api/backends`` API and report a real result."""
        from toolkit.core.config.config import Config, ExternalHost, ServicesConfig

        cfg = Config(
            domain="example.com",
            services=ServicesConfig(media=True, cloud=True),
            service_settings={"media-cache": {"enabled": True}},
        )
        cfg.external_hosts = [
            ExternalHost(
                name="nas",
                ip="10.20.0.5",
                services=["media-cache"],
                integrations={"media-cache": {"path": "/srv/media"}},
            ),
        ]

        calls: list[tuple[str, str]] = []

        def fake_docker_curl(_cfg, _vm_ip, container, url, **_kwargs):
            calls.append((container, url))
            return 0, '{"backends": ["b2"]}'

        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_docker_curl)

        from tests.helpers.plugins import load_plugin

        check = load_plugin("media-cache").check_backends(cfg, "10.10.10.11", tmp_path)

        assert check.passed is True
        assert "1 backend" in check.detail
        assert ("media-cache", "http://localhost:8686/api/backends") in calls

    def test_reports_failure_when_external_storage_host_but_zero_backends(self, tmp_path, monkeypatch):
        """Host present AND API responds with zero backends → real FAIL."""
        from toolkit.core.config.config import Config, ExternalHost, ServicesConfig

        cfg = Config(
            domain="example.com",
            services=ServicesConfig(media=True, cloud=True),
            service_settings={"media-cache": {"enabled": True}},
        )
        cfg.external_hosts = [
            ExternalHost(
                name="nas",
                ip="10.20.0.5",
                services=["media-cache"],
                integrations={"media-cache": {"path": "/srv/media"}},
            ),
        ]

        monkeypatch.setattr("toolkit.services.sdk.docker_curl", lambda *_a, **_kw: (0, '{"backends": []}'))

        from tests.helpers.plugins import load_plugin

        check = load_plugin("media-cache").check_backends(cfg, "10.10.10.11", tmp_path)

        assert check.passed is False
        assert "0 backend" in check.detail
