from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[3]
ALLOY_IMAGE = "grafana/alloy:v1.17.1@sha256:4f6ddc56ffdcf8a6316748fc5162972e20cb301523cac1bb4a31957df733ae9b"


def test_alloy_service_uses_official_pinned_image_and_persistent_cursor_state() -> None:
    service_dir = ROOT / "toolkit" / "services" / "alloy"
    manifest = yaml.safe_load((service_dir / "service.yaml").read_text(encoding="utf-8"))
    compose = yaml.safe_load((service_dir / "compose.yaml").read_text(encoding="utf-8"))

    assert manifest["runtimes"] == {
        "alloy-agent": {
            "placements": ["@non-primary"],
            "compose_profile": "monitoring-agent",
        },
        "alloy-agent-docker-proxy": {
            "placements": ["@non-primary"],
            "compose_profile": "monitoring-agent",
        },
    }
    assert manifest["data_specs"] == [
        {
            "name": "alloy-data",
            "source_env": "ALLOY_DATA_SOURCE",
            "target": "/var/lib/alloy/data",
            "size_estimate_gb": 1,
            "snapshot": False,
        }
    ]
    for service_name in ("alloy", "alloy-agent"):
        service = compose["services"][service_name]
        assert service["image"] == ALLOY_IMAGE
        assert service["cap_drop"] == ["ALL"]
        assert service["cap_add"] == ["DAC_OVERRIDE"]
        assert all("docker.sock" not in volume for volume in service["volumes"])
        assert "alloy-docker-api" in service["networks"]
        assert service["environment"]["ALLOY_DOCKER_HOST"].startswith("http://alloy")
        assert any(volume.endswith(":/var/lib/alloy/data") for volume in service["volumes"])
        assert service["healthcheck"]["test"][-1].find("/-/healthy") >= 0

    proxy_image = (
        "ghcr.io/tecnativa/docker-socket-proxy:v0.4.2"
        "@sha256:1f3a6f303320723d199d2316a3e82b2e2685d86c275d5e3deeaf182573b47476"
    )
    for service_name in ("alloy-docker-proxy", "alloy-agent-docker-proxy"):
        proxy = compose["services"][service_name]
        assert proxy["image"] == proxy_image
        assert proxy["environment"]["CONTAINERS"] == "1"
        assert proxy["environment"]["EVENTS"] == "1"
        assert proxy["environment"]["NETWORKS"] == "1"
        assert proxy["environment"]["POST"] == "0"
        assert proxy["networks"] == ["alloy-docker-api"]
        assert proxy["volumes"] == ["/var/run/docker.sock:/var/run/docker.sock:ro"]
        assert proxy["cap_drop"] == ["ALL"]
        assert proxy["security_opt"] == ["no-new-privileges:true"]

    assert compose["networks"]["alloy-docker-api"]["internal"] is True


def test_alloy_uses_the_read_only_docker_api_proxy() -> None:
    config = (ROOT / "config" / "alloy" / "config.alloy").read_text(encoding="utf-8")

    assert 'host             = sys.env("ALLOY_DOCKER_HOST")' in config
    assert 'host          = sys.env("ALLOY_DOCKER_HOST")' in config
    assert "unix:///var/run/docker.sock" not in config


@pytest.mark.timeout(240)
def test_alloy_configuration_is_accepted_by_the_pinned_runtime() -> None:
    config = ROOT / "config" / "alloy" / "config.alloy"
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-e",
            "ALLOY_NODE_ROLE=test-node",
            "-e",
            "ALLOY_LOKI_URL=http://127.0.0.1:3100/loki/api/v1/push",
            "-v",
            f"{config}:/etc/alloy/config.alloy:ro",
            ALLOY_IMAGE,
            "validate",
            "/etc/alloy/config.alloy",
        ],
        check=False,
        capture_output=True,
        text=True,
        # A cold CI runner may need to pull the pinned image before starting
        # validation; keep the test deterministic without making it flaky.
        timeout=180,
    )

    assert proc.returncode == 0, proc.stderr or proc.stdout


@pytest.mark.timeout(240)
def test_fleet_alloy_configuration_is_accepted_by_the_pinned_runtime(tmp_path: Path) -> None:
    template = (
        ROOT / "automation" / "ansible" / "roles" / "monitoring_agent" / "templates" / "config.alloy.j2"
    ).read_text(encoding="utf-8")
    rendered = template.replace("{{ inventory_hostname }}", "fleet-test").replace(
        "{{ service_ips['loki'] }}", "10.10.10.10"
    )
    config = tmp_path / "config.alloy"
    config.write_text(rendered, encoding="utf-8")

    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-v",
            f"{config}:/etc/alloy/config.alloy:ro",
            ALLOY_IMAGE,
            "validate",
            "/etc/alloy/config.alloy",
        ],
        check=False,
        capture_output=True,
        text=True,
        timeout=180,
    )

    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_retired_log_shipper_is_absent_from_runtime_sources() -> None:
    searched = (
        ROOT / "toolkit",
        ROOT / "automation",
        ROOT / "config",
        ROOT / "images",
        ROOT / "scripts",
        ROOT / "renovate.json",
    )
    offenders: list[str] = []
    for base in searched:
        paths = [base] if base.is_file() else [path for path in base.rglob("*") if path.is_file()]
        for path in paths:
            try:
                content = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if "prom" + "tail" in content.lower():
                offenders.append(str(path.relative_to(ROOT)))

    assert offenders == []
