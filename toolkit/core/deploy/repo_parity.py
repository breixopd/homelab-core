"""Repository parity verification for managed machines.

C6 Phase 3 exit criterion: "guest_sync / repo parity on guests — Commit hash
match". The push path lives in :mod:`toolkit.core.deploy.repo_sync` (tarball +
scp + extract, then stamps ``<repo_dest>/.homelab-state/commit-sha`` with the
controller's HEAD). This module compares the stamped "expected" hash against
both the stamp file and the live ``git rev-parse HEAD`` on each guest so that
manual drift on a guest is also caught.
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from pathlib import Path

from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
from toolkit.core.config.config import Config, config_path, load_config
from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT
from toolkit.core.deploy.repo_sync import STAMP_REL, controller_commit_sha


@dataclass
class ParityResult:
    """Outcome of a single guest's repo parity check."""

    vm: str
    controller_sha: str
    expected_sha: str
    guest_sha: str
    in_parity: bool
    detail: str = ""

    def __str__(self) -> str:
        mark = "OK" if self.in_parity else "DRIFT"
        return (
            f"[{mark}] {self.vm}: controller={self.controller_sha[:12]} "
            f"expected={self.expected_sha[:12]} guest={self.guest_sha[:12]}"
            + (f" ({self.detail})" if self.detail else "")
        )


def _guest_state_file(repo_dest: str) -> str:
    return f"{repo_dest}/{STAMP_REL}"


def _probe_guest(cfg: Config, vm_ip: str, root: Path, repo_dest: str) -> tuple[str, str, str]:
    """SSH to a guest and read (expected_sha, guest_sha, error).

    ``expected_sha`` comes from the stamped commit-sha file (set on push).
    ``guest_sha`` is the live ``git rev-parse HEAD`` in the guest repo, which
    catches manual ``git pull``/``git checkout`` drift that bypasses sync.

    Note: tarball sync does not ship ``.git/``, so ``guest_sha`` is normally
    empty on a synced guest — the stamp file is authoritative in that case.
    """
    stamp_file = _guest_state_file(repo_dest)
    # Read stamp file and live git HEAD in one round-trip; never fail the
    # shell so a missing file or non-git directory surfaces as empty strings
    # rather than a non-zero exit that swallows the stamp output.
    cmd = (
        f"expected=$(cat {shlex.quote(stamp_file)} 2>/dev/null | tr -d ' \\n\\r'); "
        f"guest=$(git -C {shlex.quote(repo_dest)} rev-parse HEAD 2>/dev/null | tr -d ' \\n\\r'); "
        f'printf \'%s\\t%s\\n\' "$expected" "$guest"'
    )
    rc, out, err = ssh_run_on_vm(cfg, vm_ip, cmd, root=root, timeout=30)
    if rc != 0:
        return "", "", (err.strip() or f"ssh exit {rc}")[:200]
    # Do NOT strip the whole line — that would erase a leading tab when the
    # stamp is empty. Split on tab first, then strip each field.
    raw_lines = (out or "").splitlines()
    line = raw_lines[-1] if raw_lines else ""
    parts = line.split("\t")
    expected = parts[0].strip() if len(parts) > 0 else ""
    guest = parts[1].strip() if len(parts) > 1 else ""
    error = ""
    if not expected and not guest:
        error = "stamp file missing and git HEAD unreadable"
    elif not expected:
        error = "stamp file missing (run `machines sync <machine-id>`)"
    # When expected is set but guest is empty, that is the normal tarball-sync
    # case (no .git on the guest) — no error; the stamp is authoritative.
    return expected, guest, error


def verify_repo_parity(
    root: Path,
    *,
    vm_name: str | None = None,
    cfg: Config | None = None,
    repo_dest: str = DEFAULT_HOMELAB_ROOT,
) -> list[ParityResult]:
    """Compare controller HEAD to each guest's stamped + live commit hash.

    Parameters
    ----------
    root:
        Controller repo root (where ``git rev-parse HEAD`` runs).
    vm_name:
        Restrict the check to a single guest (e.g. ``"infra"``). ``None``
        fans out to every enabled VM.
    cfg:
        Loaded config. If omitted, loaded from ``<root>/config.yaml``.
    repo_dest:
        Guest path the repo was synced to. Defaults to ``/opt/homelab``.
    """
    cfg = cfg or load_config(config_path(root))
    controller_sha = controller_commit_sha(root) or ""
    targets = [vm_name] if vm_name else cfg.enabled_nodes

    results: list[ParityResult] = []
    for vm in targets:
        try:
            vm_ip = cfg.node_ip(vm)
        except KeyError:
            results.append(
                ParityResult(
                    vm=vm,
                    controller_sha=controller_sha,
                    expected_sha="",
                    guest_sha="",
                    in_parity=False,
                    detail="no machine address configured",
                )
            )
            continue

        expected, guest, error = _probe_guest(cfg, vm_ip, root, repo_dest)
        # Determine parity. The stamp (expected) is authoritative for a
        # tarball-synced guest (which carries no .git/). A readable live git
        # HEAD that disagrees with the controller is drift, but only when the
        # stamp itself is healthy — otherwise the stamp-level error is the
        # more actionable message.
        if not controller_sha:
            in_parity = False
            error = error or "controller git HEAD unavailable"
        elif not expected:
            # Stamp missing — cannot trust the guest is synced, regardless of
            # what live git HEAD says.
            in_parity = False
            error = error or "stamp file missing (run `machines sync <machine-id>`)"
        elif expected != controller_sha:
            in_parity = False
            error = f"stale stamp: expected {expected[:12]}"
        elif guest and guest != controller_sha:
            # Stamp matches controller but live git HEAD on guest disagrees →
            # someone committed/checked-out on the guest after the sync.
            in_parity = False
            error = f"guest git HEAD drifts: {guest[:12]}"
        else:
            # Stamp matches controller; guest git HEAD either matches or is
            # empty (normal tarball-sync case with no .git on the guest).
            in_parity = True
            error = ""
        results.append(
            ParityResult(
                vm=vm,
                controller_sha=controller_sha,
                expected_sha=expected,
                guest_sha=guest,
                in_parity=in_parity,
                detail=error,
            )
        )
    return results


def format_parity_report(results: list[ParityResult]) -> str:
    """Render a one-line-per-VM table suitable for CLI output."""
    if not results:
        return "repo parity: no VMs checked"
    lines: list[str] = []
    for r in results:
        lines.append(str(r))
    all_ok = all(r.in_parity for r in results)
    header = f"repo parity: {'OK' if all_ok else 'DRIFT'} ({sum(r.in_parity for r in results)}/{len(results)})"
    return "\n".join([header] + lines)
