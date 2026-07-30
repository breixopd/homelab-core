from __future__ import annotations

import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from toolkit.core.compose.docker import ContainerStatus
from toolkit.core.config.config import Config, ProjectEntry, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.generate.generate import generate_all
from toolkit.core.machines import MachineSpec
from toolkit.core.ops.verify import _check_runtime_images, verify_vm


def _image_check(desired: str, loaded: str, *, inspect_output: str = "sha256:id", inspect_rc: int = 0):
    dc = MagicMock()
    dc._run.return_value = SimpleNamespace(
        returncode=0,
        stdout=f'{{"services":{{"web":{{"image":"{desired}"}}}}}}',
    )
    container = ContainerStatus(name="web-1", service="web", state="running", health="", image=loaded)
    completed = SimpleNamespace(returncode=inspect_rc, stdout=inspect_output, stderr="")
    with patch("toolkit.core.ops.verify.subprocess.run", return_value=completed):
        return _check_runtime_images(dc, [], ["web"], {"web": container})


def test_runtime_image_exact_reference_passes() -> None:
    assert _image_check("example/web:1@sha256:abc", "example/web:1@sha256:abc") == []


def test_runtime_image_mismatched_immutable_reference_fails() -> None:
    dc = MagicMock()
    dc._run.return_value = SimpleNamespace(
        returncode=0,
        stdout='{"services":{"web":{"image":"example/web:1@sha256:abc"}}}',
    )
    container = ContainerStatus(
        name="web-1", service="web", state="running", health="", image="example/web:2@sha256:def"
    )
    with patch(
        "toolkit.core.ops.verify.subprocess.run",
        side_effect=[
            SimpleNamespace(returncode=0, stdout="sha256:loaded", stderr=""),
            SimpleNamespace(returncode=0, stdout="sha256:desired", stderr=""),
        ],
    ):
        assert _check_runtime_images(dc, [], ["web"], {"web": container})


def test_runtime_image_digest_reference_matches_loaded_tag_by_id() -> None:
    dc = MagicMock()
    dc._run.return_value = SimpleNamespace(
        returncode=0,
        stdout='{"services":{"web":{"image":"example/web:1@sha256:abc"}}}',
    )
    container = ContainerStatus(name="web-1", service="web", state="running", health="", image="example/web:1")
    with patch(
        "toolkit.core.ops.verify.subprocess.run",
        side_effect=[
            SimpleNamespace(returncode=0, stdout="sha256:matching", stderr=""),
            SimpleNamespace(returncode=0, stdout="sha256:matching", stderr=""),
        ],
    ):
        assert _check_runtime_images(dc, [], ["web"], {"web": container}) == []


def test_runtime_image_digest_reference_fails_on_id_mismatch() -> None:
    dc = MagicMock()
    dc._run.return_value = SimpleNamespace(
        returncode=0,
        stdout='{"services":{"web":{"image":"example/web:1@sha256:abc"}}}',
    )
    container = ContainerStatus(name="web-1", service="web", state="running", health="", image="example/web:1")
    with patch(
        "toolkit.core.ops.verify.subprocess.run",
        side_effect=[
            SimpleNamespace(returncode=0, stdout="sha256:loaded", stderr=""),
            SimpleNamespace(returncode=0, stdout="sha256:desired", stderr=""),
        ],
    ):
        assert _check_runtime_images(dc, [], ["web"], {"web": container})


def test_runtime_image_mutable_alias_requires_matching_id() -> None:
    assert _image_check("example/web:latest", "example/web:latest") == []

    dc = MagicMock()
    dc._run.return_value = SimpleNamespace(returncode=0, stdout='{"services":{"web":{"image":"example/web:latest"}}}')
    container = ContainerStatus(name="web-1", service="web", state="running", health="", image="example/web:old")
    with patch(
        "toolkit.core.ops.verify.subprocess.run",
        side_effect=[
            SimpleNamespace(returncode=0, stdout="sha256:id", stderr=""),
            SimpleNamespace(returncode=0, stdout="sha256:id", stderr=""),
        ],
    ):
        assert _check_runtime_images(dc, [], ["web"], {"web": container}) == []
    with patch(
        "toolkit.core.ops.verify.subprocess.run",
        side_effect=[
            SimpleNamespace(returncode=0, stdout="sha256:old", stderr=""),
            SimpleNamespace(returncode=0, stdout="sha256:new", stderr=""),
        ],
    ):
        assert _check_runtime_images(dc, [], ["web"], {"web": container})


def test_runtime_image_inspect_failure_fails_closed() -> None:
    assert _image_check("example/web:latest", "example/web:old", inspect_rc=1)


def test_runtime_image_inspect_timeout_fails_closed() -> None:
    dc = MagicMock()
    dc._run.return_value = SimpleNamespace(
        returncode=0,
        stdout='{"services":{"web":{"image":"example/web:latest"}}}',
    )
    container = ContainerStatus(name="web-1", service="web", state="running", health="", image="example/web:old")
    with patch(
        "toolkit.core.ops.verify.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd=["docker", "inspect"], timeout=30),
    ):
        assert _check_runtime_images(dc, [], ["web"], {"web": container}) == ["web(image inspect failed)"]


def test_verify_result_requires_docker_and_compose_health():
    from toolkit.core.ops.verify import VerifyResult

    assert not VerifyResult(vm="infra", docker_ok=False, compose_ok=True).ok
    assert not VerifyResult(vm="infra", docker_ok=True, compose_ok=False).ok
    assert VerifyResult(vm="infra", docker_ok=True, compose_ok=True).ok


def test_native_docker_listing_uses_compose_service_label() -> None:
    import json

    from toolkit.core.ops.verify import _docker_ps_native

    row = {
        "Names": "homelab-vaultwarden-1",
        "State": "running",
        "Status": "Up 1 minute (healthy)",
        "Image": "vaultwarden/server:1.36.0",
        "Labels": "com.docker.compose.project=homelab,com.docker.compose.service=vaultwarden",
    }
    completed = MagicMock(returncode=0, stdout=json.dumps(row) + "\n")

    with patch("toolkit.core.ops.verify.subprocess.run", return_value=completed):
        containers = _docker_ps_native()

    assert len(containers) == 1
    assert containers[0].name == "homelab-vaultwarden-1"
    assert containers[0].service == "vaultwarden"
    assert containers[0].state == "running"
    assert containers[0].health == "healthy"


def test_env_contains_compose_profiles(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    cfg = Config(
        domain="example.com",
        email="a@example.com",
        service_settings={"jellyfin": {"hardware-transcode": "none"}},
    )
    cfg.proxmox.provision_machines = False
    save_config(cfg, config_path(root))
    (root / "docker-compose.yml").write_text("name: homelab\nservices: {}\n")
    generate_all(root)
    env_text = (root / "generated" / "infra" / ".env").read_text()
    assert "COMPOSE_PROFILES=" in env_text


def test_verify_vm_missing_compose(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    cfg = Config(domain="example.com", email="a@example.com")
    result = verify_vm(root, cfg, "infra")
    assert not result.ok
    assert any("Compose model missing for infra" in e for e in result.errors)


def test_verify_vm_docker_ok(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    cfg = Config(
        domain="example.com",
        email="a@example.com",
        service_settings={"jellyfin": {"hardware-transcode": "none"}},
    )
    save_config(cfg, config_path(root))
    (root / "docker-compose.yml").write_text("name: homelab\nservices: {}\n")
    generate_all(root)
    role_compose = root / "generated" / "infra" / "compose.yaml"
    role_compose.write_text("name: homelab\nservices: {}\n")

    mock_dc = MagicMock()
    mock_dc.preflight.return_value = True
    mock_dc.ps.return_value = []
    mock_dc._run.return_value = MagicMock(returncode=0, stderr="", stdout="")

    # Mock probe_url to avoid real DNS/network calls during tests.
    with (
        patch("toolkit.core.ops.verify.DockerCompose", return_value=mock_dc),
        patch("toolkit.core.net.http_probe.probe_url", return_value=(False, "skip in tests")),
    ):
        result = verify_vm(root, cfg, "infra")
    assert result.docker_ok
    assert result.compose_ok


def test_parse_verify_json_payload():
    from toolkit.core.ops.verify import _parse_verify_json_payload

    text = """{
  "infra": {
    "ok": false,
    "healthy": ["caddy"],
    "unhealthy": ["grafana(unhealthy)"],
    "urls": [{"url": "https://auth.example.com", "ok": true, "detail": "200"}],
    "errors": []
  }
}"""
    result = _parse_verify_json_payload(text, "infra")
    assert result is not None
    assert result.services_healthy == ["caddy"]
    assert result.services_unhealthy == ["grafana(unhealthy)"]
    assert result.url_checks == [("https://auth.example.com", True, "200")]


def test_verify_manifest_one_shot_missing_ok_when_owner_is_healthy():
    from toolkit.core.compose.docker import ContainerStatus
    from toolkit.core.ops.verify import _one_shot_init_ok, runtime_verification_policies

    by_service = {
        "wazuh-indexer": ContainerStatus(
            name="wazuh-indexer",
            service="wazuh-indexer",
            state="running",
            health="healthy",
            image="wazuh/wazuh-indexer:4.14.5",
        ),
    }
    policies = runtime_verification_policies(Config(), "infra")
    assert policies["wazuh-indexer-certs-init"].mode == "oneshot"
    assert policies["wazuh-indexer-certs-init"].owner == "wazuh-indexer"
    assert _one_shot_init_ok("wazuh-indexer-certs-init", None, by_service, policies)


def test_verify_manifest_one_shot_missing_fails_without_owner():
    from toolkit.core.ops.verify import _one_shot_init_ok, runtime_verification_policies

    policies = runtime_verification_policies(Config(), "infra")
    assert not _one_shot_init_ok("wazuh-indexer-certs-init", None, {}, policies)


def test_pending_start_services_come_from_service_manifests():
    from toolkit.core.ops.verify import pending_start_services

    cfg = Config()
    pending = {service for node in cfg.enabled_nodes for service in pending_start_services(cfg, node)}

    assert pending == {
        "immich-machine-learning",
        "navidrome",
        "nextcloud",
        "romm",
        "roundcube",
        "wazuh-dashboard",
    }


def test_verify_classifies_manifest_permitted_starting_service_as_pending(tmp_path: Path):
    from toolkit.core.compose.docker import ContainerStatus

    root = tmp_path / "homelab"
    generated = root / "generated" / "infra"
    generated.mkdir(parents=True)
    (generated / "compose.yaml").write_text("name: homelab\nservices: {}\n")
    (generated / ".env").write_text("COMPOSE_PROFILES=\n")
    cfg = Config(domain="localhost")

    mock_dc = MagicMock()
    mock_dc.preflight.return_value = True
    mock_dc._run.side_effect = [
        MagicMock(returncode=0, stderr="", stdout=""),
        MagicMock(returncode=0, stderr="", stdout="wazuh-dashboard\n"),
        MagicMock(
            returncode=0,
            stderr="",
            stdout='{"services":{"wazuh-dashboard":{"image":"wazuh/wazuh-dashboard:4.14.6"}}}',
        ),
    ]
    starting = ContainerStatus(
        name="wazuh-dashboard",
        service="wazuh-dashboard",
        state="running",
        health="starting",
        image="wazuh/wazuh-dashboard:4.14.6",
    )

    with (
        patch("toolkit.core.ops.verify.DockerCompose", return_value=mock_dc),
        patch("toolkit.core.ops.verify._docker_ps_native", return_value=[starting]),
        patch(
            "toolkit.core.ops.verify.subprocess.run",
            side_effect=[
                SimpleNamespace(returncode=0, stdout="sha256:wazuh", stderr=""),
                SimpleNamespace(returncode=0, stdout="sha256:wazuh", stderr=""),
            ],
        ),
    ):
        result = verify_vm(root, cfg, "infra")

    assert result.ok
    assert result.services_pending == ["wazuh-dashboard"]
    assert result.services_healthy == []


def test_verify_requires_enabled_tdarr_on_cpu_only_nodes(tmp_path: Path):
    root = tmp_path / "homelab"
    generated = root / "generated" / "media"
    generated.mkdir(parents=True)
    (generated / "compose.yaml").write_text("name: homelab\nservices: {}\n")
    (generated / ".env").write_text("COMPOSE_PROFILES=\n")
    cfg = Config(domain="localhost")

    mock_dc = MagicMock()
    mock_dc.preflight.return_value = True
    mock_dc.ps.return_value = []
    mock_dc._run.side_effect = [
        MagicMock(returncode=0, stderr="", stdout=""),
        MagicMock(returncode=0, stderr="", stdout="tdarr\n"),
        MagicMock(returncode=0, stderr="", stdout='{"services":{"tdarr":{"image":"ghcr.io/haveagitgat/tdarr:2"}}}'),
    ]

    with (
        patch("toolkit.core.ops.verify.DockerCompose", return_value=mock_dc),
        patch("toolkit.core.ops.verify._docker_ps_native", return_value=[]),
        patch("toolkit.core.ansible.ansible_ssh.ssh_run_on_vm", return_value=(1, "", "no GPU")),
    ):
        result = verify_vm(root, cfg, "media")

    assert result.services_healthy == []
    assert result.services_unhealthy == ["tdarr(missing)"]


def test_verify_all_json_output(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    cfg = Config(
        domain="localhost",
        email="a@example.com",
        service_settings={"jellyfin": {"hardware-transcode": "none"}},
    )
    cfg.proxmox.provision_machines = False
    save_config(cfg, config_path(root))
    (root / "docker-compose.yml").write_text("name: homelab\nservices: {}\n")
    generate_all(root)

    mock_dc = MagicMock()
    mock_dc.preflight.return_value = True
    mock_dc.ps.return_value = []
    mock_dc._run.return_value = MagicMock(returncode=0, stderr="", stdout="")

    import json

    from toolkit.core.ops.verify import verify_all

    with patch("toolkit.core.ops.verify.DockerCompose", return_value=mock_dc):
        results = verify_all(root, cfg)
    assert results
    payload = {
        vm: {
            "ok": r.ok,
            "healthy": r.services_healthy,
            "unhealthy": r.services_unhealthy,
            "urls": [{"url": u, "ok": ok, "detail": d} for u, ok, d in r.url_checks],
            "errors": r.errors,
        }
        for vm, r in results.items()
    }
    out = json.dumps(payload, indent=2)
    assert '"infra"' in out
    assert '"healthy"' in out


def test_check_urls_runs_concurrently():
    from toolkit.core.ops.verify import _check_urls

    seen: list[str] = []

    def fake_check(url: str, timeout: int = 6):
        seen.append(url)
        return True, "200"

    with patch("toolkit.core.ops.verify._check_url", side_effect=fake_check):
        results = _check_urls(["https://a.example", "https://b.example"])
    assert len(results) == 2
    assert all(ok for _, ok, _ in results)
    assert set(seen) == {"https://a.example", "https://b.example"}


def test_https_cache_does_not_reuse_transient_failures(tmp_path: Path):
    import json
    import time

    from toolkit.core.ops.verify import _check_urls, _https_cache_path

    cache_path = _https_cache_path(tmp_path)
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(
        json.dumps(
            {
                "https://recovering.example": {
                    "ok": False,
                    "detail": "[Errno 111] Connection refused",
                    "ts": time.time(),
                }
            }
        ),
        encoding="utf-8",
    )

    with patch("toolkit.core.ops.verify._check_url", return_value=(True, "302")) as check:
        results = _check_urls(["https://recovering.example"], root=tmp_path)

    assert results == [("https://recovering.example", True, "302")]
    check.assert_called_once()


def test_https_probe_uses_ingress_for_controller_hairpin_failure(tmp_path: Path):
    from toolkit.core.ops.verify import _check_urls

    cfg = Config(domain="example.test")
    with (
        patch(
            "toolkit.core.ops.verify._check_url",
            return_value=(False, "[Errno 111] Connection refused"),
        ),
        patch(
            "toolkit.core.ops.verify._check_url_via_ingress",
            return_value=(True, "302"),
        ) as ingress,
    ):
        results = _check_urls(["https://mail.example.test"], root=tmp_path, cfg=cfg)

    assert results == [
        (
            "https://mail.example.test",
            True,
            "ingress 302; controller edge probe unavailable: [Errno 111] Connection refused",
        )
    ]
    ingress.assert_called_once_with(cfg, tmp_path, "https://mail.example.test")


def test_https_probe_does_not_hide_http_or_tls_failures_with_ingress_fallback(tmp_path: Path):
    from toolkit.core.ops.verify import _check_urls

    cfg = Config(domain="example.test")
    with (
        patch("toolkit.core.ops.verify._check_url", return_value=(False, "HTTP 503")),
        patch("toolkit.core.ops.verify._check_url_via_ingress") as ingress,
    ):
        results = _check_urls(["https://broken.example.test"], root=tmp_path, cfg=cfg)

    assert results == [("https://broken.example.test", False, "HTTP 503")]
    ingress.assert_not_called()


def test_public_project_is_included_in_default_https_probes() -> None:
    from toolkit.core.ops.verify import _default_urls

    cfg = Config(domain="example.test")
    cfg.projects.entries = [
        ProjectEntry(
            subdomain="status",
            auth_mode="forward_auth",
            exposure="public",
            docker_image="docker.io/library/nginx:1@sha256:" + "a" * 64,
            container_port=45678,
            placement="apps",
        )
    ]

    assert "https://status.example.test" in _default_urls(cfg)


def test_remote_https_probes_attach_to_declared_control_node(tmp_path: Path) -> None:
    from toolkit.core.ops.verify import verify_remote

    cfg = Config(
        domain="example.test",
        machines={
            "gateway": MachineSpec(
                hostname="gateway-01",
                address="10.20.30.10",
                gateway="10.20.30.1",
                vmid=910,
                labels=("control", "ingress"),
            )
        },
    )
    inventory = tmp_path / "automation/ansible/inventory/hosts.yml"
    inventory.parent.mkdir(parents=True)
    inventory.write_text("all: {}\n")
    remote_payload = '{"gateway":{"healthy":[],"unhealthy":[],"pending":[],"urls":[],"errors":[]}}'
    process = MagicMock(returncode=0, stdout=remote_payload, stderr="")

    with (
        patch("toolkit.core.ansible.ansible_ssh.resolve_tool", return_value="ansible"),
        patch("toolkit.core.ops.verify.subprocess.run", return_value=process) as run,
        patch(
            "toolkit.core.ops.verify._check_urls",
            return_value=[("https://status.example.test", True, "200")],
        ),
    ):
        results = verify_remote(
            tmp_path,
            cfg,
            extra_urls=["https://status.example.test"],
        )

    assert results["gateway"].url_checks == [("https://status.example.test", True, "200")]
    assert run.call_args.args[0][1] == "gateway-01"


def test_remote_verify_rejects_non_json_success_output(tmp_path: Path) -> None:
    from toolkit.core.ops.verify import verify_remote

    cfg = Config(
        domain="example.test",
        machines={
            "gateway": MachineSpec(
                hostname="custom-control",
                address="10.20.30.10",
                gateway="10.20.30.1",
                vmid=910,
                labels=("control", "ingress"),
            )
        },
    )
    inventory = tmp_path / "automation/ansible/inventory/hosts.yml"
    inventory.parent.mkdir(parents=True)
    inventory.write_text("all: {}\n")
    process = MagicMock(returncode=0, stdout="verification completed without protocol output", stderr="")

    with (
        patch("toolkit.core.ansible.ansible_ssh.resolve_tool", return_value="ansible"),
        patch("toolkit.core.ops.verify.subprocess.run", return_value=process) as run,
    ):
        result = verify_remote(tmp_path, cfg)["gateway"]

    assert result.ok is False
    assert result.errors == ["remote verify returned an invalid JSON payload"]
    assert run.call_args.args[0][1] == "custom-control"
