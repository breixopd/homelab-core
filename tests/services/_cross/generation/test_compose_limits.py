from __future__ import annotations

from pathlib import Path

from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.deploy.compose_limits import write_compose_limits


def test_write_compose_limits_excludes_homepage(tmp_path: Path):
    cfg = Config(domain="test.local", dns={"public_ip": "1.2.3.4"})
    save_config(cfg, config_path(tmp_path))
    compose = tmp_path / "generated" / "infra" / "compose.yaml"
    compose.parent.mkdir(parents=True)
    compose.write_text("services:\n  grafana:\n    image: grafana/grafana\n  homepage:\n    image: x\n")
    out = write_compose_limits(cfg, "infra", tmp_path)
    assert out is not None
    text = out.read_text()
    assert "homepage" not in text
    assert "grafana" in text


def test_write_compose_limits_caps_cpu_to_machine_allocation(tmp_path: Path, monkeypatch):
    """Per-service CPU limits cannot exceed the target machine plugin allocation."""
    cfg = Config(domain="test.local", dns={"public_ip": "1.2.3.4"})
    save_config(cfg, config_path(tmp_path))
    compose = tmp_path / "generated" / "media" / "compose.yaml"
    compose.parent.mkdir(parents=True)
    compose.write_text("services:\n  jellyfin:\n    image: jellyfin/jellyfin\n  sonarr:\n    image: sonarr\n")
    monkeypatch.setattr(
        "toolkit.core.deploy.compose_limits._vm_service_names",
        lambda _cfg, vm: ["jellyfin", "sonarr"] if vm == "media" else ["grafana"],
    )
    out = write_compose_limits(cfg, "media", tmp_path)
    assert out is not None
    import yaml

    data = yaml.safe_load(out.read_text().split("---", 1)[-1])
    jelly_cpus = float(data["services"]["jellyfin"]["cpus"])
    assert jelly_cpus <= 1.9


def test_write_compose_limits_applies_machine_resource_overrides(tmp_path: Path, monkeypatch):
    base = Config(domain="test.local", dns={"public_ip": "1.2.3.4"})
    raw = base.model_dump(mode="python")
    raw["machines"]["apps"]["resource_limits"] = {
        "grafana": {"memory_mb": 768, "cpus": 0.75},
    }
    cfg = Config.model_validate(raw)
    compose = tmp_path / "generated" / "apps" / "compose.yaml"
    compose.parent.mkdir(parents=True)
    compose.write_text("services:\n  grafana:\n    image: grafana/grafana\n")
    monkeypatch.setattr(
        "toolkit.core.deploy.compose_limits._vm_service_names",
        lambda _cfg, vm: ["grafana"] if vm == "apps" else [],
    )

    out = write_compose_limits(cfg, "apps", tmp_path)

    assert out is not None
    import yaml

    data = yaml.safe_load(out.read_text())
    assert data["services"]["grafana"] == {"mem_limit": "768m", "cpus": "0.75"}
