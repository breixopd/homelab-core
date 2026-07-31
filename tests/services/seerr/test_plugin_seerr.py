from pathlib import Path
from types import SimpleNamespace

from toolkit.services._arr import verify_seerr_connections, verify_seerr_status


def test_missing_container_fails(monkeypatch, tmp_path: Path):
    cfg = SimpleNamespace(domain="example.com", is_multi_node=True)
    monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: False)

    check = verify_seerr_status(cfg, "10.0.0.2", tmp_path)

    assert not check.passed
    assert check.status.value == "fail"


def test_missing_api_key_is_not_ready(monkeypatch, tmp_path: Path):
    cfg = SimpleNamespace(domain="example.com", is_multi_node=False)

    check = verify_seerr_connections(cfg, {}, "10.0.0.2", tmp_path)

    assert not check.passed
    assert check.status.value == "not_ready"
