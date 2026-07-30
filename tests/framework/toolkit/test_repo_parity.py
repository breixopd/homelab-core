"""Tests for repo parity verification (C6)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from tests.helpers.machines import machines_with_addresses
from toolkit.core.config.config import Config
from toolkit.core.deploy.repo_parity import (
    ParityResult,
    format_parity_report,
    verify_repo_parity,
)
from toolkit.core.deploy.repo_sync import STAMP_REL, controller_commit_sha

SHA_A = "a" * 40
SHA_B = "b" * 40


def _make_root(tmp_path: Path) -> Path:
    root = tmp_path / "homelab"
    root.mkdir()
    (root / "config.yaml").write_text("domain: example.com\n")
    return root


def _multi_vm_cfg() -> Config:
    return Config(
        domain="example.com",
        machines=machines_with_addresses(infra="10.0.0.1", media="10.0.0.2", apps="10.0.0.3"),
    )


def test_controller_commit_sha_success(tmp_path: Path):
    root = _make_root(tmp_path)
    fake_result = type("R", (), {"returncode": 0, "stdout": SHA_A + "\n"})()
    with patch("toolkit.core.deploy.repo_sync.subprocess.run", return_value=fake_result):
        assert controller_commit_sha(root) == SHA_A


def test_controller_commit_sha_failure(tmp_path: Path):
    root = _make_root(tmp_path)
    fake_result = type("R", (), {"returncode": 128, "stdout": ""})()
    with patch("toolkit.core.deploy.repo_sync.subprocess.run", return_value=fake_result):
        first = controller_commit_sha(root)
        assert first is not None
        assert len(first) == 64

        (root / "config.yaml").write_text("domain: changed.example.com\n")
        assert controller_commit_sha(root) != first


def test_parity_when_hashes_match(tmp_path: Path):
    """Guest stamp + live git HEAD both equal the controller SHA → in parity."""
    root = _make_root(tmp_path)
    cfg = _multi_vm_cfg()
    # SSH returns tab-separated "expected\tguest" = SHA_A\tSHA_A
    ssh_out = (0, f"{SHA_A}\t{SHA_A}\n", "")

    with (
        patch("toolkit.core.deploy.repo_parity.controller_commit_sha", return_value=SHA_A),
        patch("toolkit.core.deploy.repo_parity.ssh_run_on_vm", return_value=ssh_out),
    ):
        results = verify_repo_parity(root, cfg=cfg)

    assert len(results) == 3
    assert {r.vm for r in results} == {"infra", "media", "apps"}
    for r in results:
        assert r.controller_sha == SHA_A
        assert r.expected_sha == SHA_A
        assert r.guest_sha == SHA_A
        assert r.in_parity is True
        assert r.detail == ""


def test_parity_drift_when_guest_hash_differs(tmp_path: Path):
    """Live guest git HEAD differs from controller → drift, even if stamp matches."""
    root = _make_root(tmp_path)
    cfg = _multi_vm_cfg()
    ssh_out = (0, f"{SHA_A}\t{SHA_B}\n", "")  # stamp=SHA_A, guest=SHA_B

    with (
        patch("toolkit.core.deploy.repo_parity.controller_commit_sha", return_value=SHA_A),
        patch("toolkit.core.deploy.repo_parity.ssh_run_on_vm", return_value=ssh_out),
    ):
        results = verify_repo_parity(root, cfg=cfg)

    assert len(results) == 3
    for r in results:
        assert r.in_parity is False
        assert r.guest_sha == SHA_B
        assert "drift" in r.detail.lower()


def test_parity_drift_when_stamp_differs(tmp_path: Path):
    """Stamped expected SHA differs from controller → drift (stale sync)."""
    root = _make_root(tmp_path)
    cfg = _multi_vm_cfg()
    ssh_out = (0, f"{SHA_B}\t{SHA_A}\n", "")  # stamp=SHA_B (stale), guest=SHA_A

    with (
        patch("toolkit.core.deploy.repo_parity.controller_commit_sha", return_value=SHA_A),
        patch("toolkit.core.deploy.repo_parity.ssh_run_on_vm", return_value=ssh_out),
    ):
        results = verify_repo_parity(root, cfg=cfg)

    assert len(results) == 3
    for r in results:
        assert r.in_parity is False
        assert r.expected_sha == SHA_B


def test_parity_missing_stamp_file(tmp_path: Path):
    """Missing stamp file on guest → not in parity, with a clear detail."""
    root = _make_root(tmp_path)
    cfg = _multi_vm_cfg()
    # Both expected and guest are empty (no stamp, no git)
    ssh_out = (0, "\t\n", "")

    with (
        patch("toolkit.core.deploy.repo_parity.controller_commit_sha", return_value=SHA_A),
        patch("toolkit.core.deploy.repo_parity.ssh_run_on_vm", return_value=ssh_out),
    ):
        results = verify_repo_parity(root, cfg=cfg)

    assert len(results) == 3
    for r in results:
        assert r.in_parity is False
        assert r.expected_sha == ""
        assert r.guest_sha == ""
        assert "stamp" in r.detail.lower() or "missing" in r.detail.lower()


def test_parity_missing_stamp_but_guest_matches(tmp_path: Path):
    """Stamp file missing but live guest HEAD matches controller → still drift.

    A missing stamp means `machines sync` was never run (or stamp was deleted), so
    we cannot trust the guest is at the synced commit even if git HEAD happens
    to match today.
    """
    root = _make_root(tmp_path)
    cfg = _multi_vm_cfg()
    ssh_out = (0, f"\t{SHA_A}\n", "")  # no stamp, but guest=SHA_A

    with (
        patch("toolkit.core.deploy.repo_parity.controller_commit_sha", return_value=SHA_A),
        patch("toolkit.core.deploy.repo_parity.ssh_run_on_vm", return_value=ssh_out),
    ):
        results = verify_repo_parity(root, cfg=cfg)

    for r in results:
        assert r.in_parity is False
        assert "stamp" in r.detail.lower() or "missing" in r.detail.lower()


def test_parity_ssh_failure_marks_drift(tmp_path: Path):
    """SSH failure (non-zero rc) → drift with the ssh error in detail."""
    root = _make_root(tmp_path)
    cfg = _multi_vm_cfg()
    ssh_out = (255, "", "connection timed out")

    with (
        patch("toolkit.core.deploy.repo_parity.controller_commit_sha", return_value=SHA_A),
        patch("toolkit.core.deploy.repo_parity.ssh_run_on_vm", return_value=ssh_out),
    ):
        results = verify_repo_parity(root, cfg=cfg)

    for r in results:
        assert r.in_parity is False
        assert r.expected_sha == ""
        assert r.guest_sha == ""
        assert "connection timed out" in r.detail


def test_parity_vm_name_filter(tmp_path: Path):
    """vm_name filter restricts the check to a single guest."""
    root = _make_root(tmp_path)
    cfg = _multi_vm_cfg()
    ssh_out = (0, f"{SHA_A}\t{SHA_A}\n", "")

    with (
        patch("toolkit.core.deploy.repo_parity.controller_commit_sha", return_value=SHA_A),
        patch("toolkit.core.deploy.repo_parity.ssh_run_on_vm", return_value=ssh_out),
    ):
        results = verify_repo_parity(root, vm_name="media", cfg=cfg)

    assert len(results) == 1
    assert results[0].vm == "media"
    assert results[0].in_parity is True


def test_parity_unknown_vm_name(tmp_path: Path):
    """Unknown vm_name → result with no IP and drift."""
    root = _make_root(tmp_path)
    cfg = Config(domain="example.com")

    with patch("toolkit.core.deploy.repo_parity.controller_commit_sha", return_value=SHA_A):
        results = verify_repo_parity(root, vm_name="nonexistent", cfg=cfg)

    assert len(results) == 1
    assert results[0].vm == "nonexistent"
    assert results[0].in_parity is False
    assert "no machine address" in results[0].detail


def test_parity_no_controller_sha(tmp_path: Path):
    """If controller git HEAD is unavailable, every guest is drift."""
    root = _make_root(tmp_path)
    cfg = _multi_vm_cfg()
    ssh_out = (0, f"{SHA_A}\t{SHA_A}\n", "")

    with (
        patch("toolkit.core.deploy.repo_parity.controller_commit_sha", return_value=None),
        patch("toolkit.core.deploy.repo_parity.ssh_run_on_vm", return_value=ssh_out),
    ):
        results = verify_repo_parity(root, cfg=cfg)

    for r in results:
        assert r.controller_sha == ""
        assert r.in_parity is False
        assert "controller" in r.detail.lower() or "unavailable" in r.detail.lower()


def test_format_parity_report_ok():
    results = [
        ParityResult(vm="infra", controller_sha=SHA_A, expected_sha=SHA_A, guest_sha=SHA_A, in_parity=True),
    ]
    report = format_parity_report(results)
    assert "OK" in report
    assert "1/1" in report
    assert "infra" in report


def test_format_parity_report_drift():
    results = [
        ParityResult(vm="infra", controller_sha=SHA_A, expected_sha=SHA_A, guest_sha=SHA_A, in_parity=True),
        ParityResult(
            vm="media",
            controller_sha=SHA_A,
            expected_sha=SHA_B,
            guest_sha=SHA_B,
            in_parity=False,
            detail="stale stamp",
        ),
    ]
    report = format_parity_report(results)
    assert "DRIFT" in report
    assert "1/2" in report
    assert "stale stamp" in report


def test_format_parity_report_empty():
    assert format_parity_report([]) == "repo parity: no VMs checked"


def test_stamp_rel_constant():
    """Stamp path is the documented relative path under repo_dest."""
    assert STAMP_REL == ".homelab-state/commit-sha"
