from __future__ import annotations

from pathlib import Path

from toolkit.core.bootstrap.runtime_assets import ensure_runtime_assets

ROOT = Path(__file__).resolve().parents[3]


def test_packaged_platform_asset_matches_source_topology() -> None:
    packaged = ROOT / "toolkit/core/bootstrap/assets/platform.yaml"
    assert packaged.read_bytes() == (ROOT / "stacks/platform.yaml").read_bytes()


def test_runtime_assets_seed_clean_root_and_are_idempotent(tmp_path: Path, monkeypatch) -> None:
    copied = ensure_runtime_assets(tmp_path)

    platform = tmp_path / "stacks/platform.yaml"
    service = tmp_path / "toolkit/services/portal/service.yaml"
    dockerfile = tmp_path / "toolkit/Dockerfile"
    assert platform.is_file()
    assert service.is_file()
    assert dockerfile.is_file()
    assert platform in copied
    original = platform.read_bytes()
    service.write_text("local customization\n", encoding="utf-8")

    assert ensure_runtime_assets(tmp_path) == []
    assert platform.read_bytes() == original
    assert service.read_text(encoding="utf-8") == "local customization\n"

    monkeypatch.setattr("toolkit.core.bootstrap.runtime_assets._package_version", lambda: "new-version")
    refreshed = ensure_runtime_assets(tmp_path)
    assert platform in refreshed
    assert platform.read_bytes() == original
    assert service.read_text(encoding="utf-8") == "local customization\n"


def test_runtime_assets_reject_symlinked_service_directory(tmp_path: Path) -> None:
    target = tmp_path / "outside"
    target.mkdir()
    services = tmp_path / "toolkit/services"
    services.parent.mkdir(parents=True)
    services.symlink_to(target, target_is_directory=True)

    import pytest

    with pytest.raises(RuntimeError, match="cannot be a symlink"):
        ensure_runtime_assets(tmp_path)
