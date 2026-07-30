"""Poll remote staggered-compose status from the Ansible controller."""

from __future__ import annotations

import argparse
import shlex
import sys
import time
from pathlib import Path

from toolkit.core.ansible.ansible_inventory import resolve_node_host_ip
from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
from toolkit.core.config.config import config_path, load_config
from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT

# Large manifest-driven nodes can legitimately spend several minutes in each
# health wave. Dead units and stale logs are detected below, so the outer
# deadline should bound a progressing deployment rather than preempt it.
DEFAULT_POLLS = 240  # 240 polls × 30s = 2 hours
DEFAULT_INTERVAL = 30
DEFAULT_SSH_RETRIES = 4
STALE_RUNNING_POLLS = 2
STALE_LOG_SECONDS = 300


def wait_for_compose_status(
    root: Path,
    vm: str,
    repo_dest: str = DEFAULT_HOMELAB_ROOT,
    *,
    max_polls: int = DEFAULT_POLLS,
    interval: int = DEFAULT_INTERVAL,
    ssh_retries: int = DEFAULT_SSH_RETRIES,
) -> int:
    """Return 0 when status file is ok, 1 on failed/timeout."""
    root = root.resolve()
    cfg = load_config(config_path(root))
    ip = resolve_node_host_ip(root, vm, cfg) or cfg.node_ip(vm)
    marker = f"{repo_dest}/.compose-up.{vm}.status"
    log_path = f"{repo_dest}/.compose-up.{vm}.log"
    marker_arg = shlex.quote(marker)
    log_arg = shlex.quote(log_path)
    process_pattern = shlex.quote(f"[h]omelab-toolkit.*deploy up --node {vm}")
    unit = shlex.quote(f"homelab-staggered-{vm}")

    stale_running = 0
    for _ in range(max_polls):
        st = _read_status(cfg, ip, marker, root=root, retries=ssh_retries)
        if st == "ok":
            return 0
        if st == "failed":
            tail = _read_remote(cfg, ip, f"tail -40 {log_arg} 2>/dev/null", root=root, retries=ssh_retries)
            sys.stderr.write(tail or "compose failed\n")
            return 1
        if st == "running":
            stale_running += 1
            if stale_running >= STALE_RUNNING_POLLS:
                pid_check = _read_remote(
                    cfg,
                    ip,
                    (
                        f"(pgrep -f {process_pattern} >/dev/null "
                        f"|| systemctl is-active --quiet {unit}) && echo alive || echo dead"
                    ),
                    root=root,
                    retries=ssh_retries,
                ).strip()
                if pid_check == "dead":
                    tail = _read_remote(cfg, ip, f"tail -40 {log_arg} 2>/dev/null", root=root, retries=ssh_retries)
                    sys.stderr.write(tail or f"staggered compose marker=running but no process for vm={vm}\n")
                    return 1
                marker_age = _read_remote(
                    cfg,
                    ip,
                    (
                        f"test -f {marker_arg} && echo $(($(date +%s)-"
                        f"$(stat -c %Y {marker_arg} 2>/dev/null || echo 0))) || echo -1"
                    ),
                    root=root,
                    retries=ssh_retries,
                ).strip()
                if marker_age.isdigit() and int(marker_age) > STALE_LOG_SECONDS:
                    log_age = _read_remote(
                        cfg,
                        ip,
                        (
                            f"test -f {log_arg} && echo $(($(date +%s)-"
                            f"$(stat -c %Y {log_arg} 2>/dev/null || echo 0))) || echo -1"
                        ),
                        root=root,
                        retries=ssh_retries,
                    ).strip()
                    if log_age.isdigit() and int(log_age) > STALE_LOG_SECONDS:
                        tail = _read_remote(cfg, ip, f"tail -40 {log_arg} 2>/dev/null", root=root, retries=ssh_retries)
                        sys.stderr.write(
                            tail or f"staggered compose stale ({marker_age}s) with status=running for vm={vm}\n"
                        )
                        return 1
                stale_running = 0
        else:
            stale_running = 0
        time.sleep(interval)

    tail = _read_remote(cfg, ip, f"tail -40 {log_arg} 2>/dev/null", root=root, retries=ssh_retries)
    sys.stderr.write(tail or f"compose timed out after {max_polls * interval}s\n")
    return 1


def _read_remote(
    cfg,
    ip: str,
    command: str,
    *,
    root: Path,
    retries: int,
) -> str:
    for attempt in range(1, retries + 1):
        rc, out, _ = ssh_run_on_vm(cfg, ip, command, root=root, timeout=45, retries=1)
        if rc == 0:
            return out or ""
        if attempt < retries:
            time.sleep(min(5 * attempt, 20))
    return ""


def _read_status(cfg, ip: str, marker: str, *, root: Path, retries: int) -> str:
    raw = _read_remote(
        cfg,
        ip,
        f"cat {shlex.quote(marker)} 2>/dev/null || echo pending",
        root=root,
        retries=retries,
    )
    return (raw or "pending").strip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Wait for remote staggered compose status")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--node", required=True)
    parser.add_argument("--repo-dest", default=DEFAULT_HOMELAB_ROOT)
    parser.add_argument("--polls", type=int, default=DEFAULT_POLLS)
    parser.add_argument("--interval", type=int, default=DEFAULT_INTERVAL)
    args = parser.parse_args(argv)
    return wait_for_compose_status(
        args.root,
        args.node,
        args.repo_dest,
        max_polls=args.polls,
        interval=args.interval,
    )


if __name__ == "__main__":
    raise SystemExit(main())
