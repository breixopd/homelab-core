"""Shared Ansible playbook runner — kills duplication across deploy/fleet/ldap paths.

Every call site previously inlined the same boilerplate:
  - resolve ansible-playbook binary
  - compile service-owned credentials into protected ephemeral extra vars
  - build cmd with -i inventory + generated_extra_vars + playbook
  - subprocess.run with cwd=automation/ansible, capture_output
  - log stdout+stderr line by line
  - check returncode

This module centralizes that. The async streaming variant (used by the deploy
workflow SSE) is preserved separately because it needs progress reporting.
"""

from __future__ import annotations

import asyncio
import os
import re
import signal
import subprocess
import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import yaml

from toolkit.core.ansible.ansible_inventory import generated_extra_vars
from toolkit.core.ansible.ansible_ssh import resolve_tool
from toolkit.core.ansible.secret_vars import deployment_secret_variables

_ANSIBLE_STREAM_LIMIT = 2**20  # 1 MiB readline limit (matches deploy_workflow)
_ANSIBLE_TIMEOUT_SECONDS = 7_200
_ANSIBLE_VARIABLE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")


class ProgressReporter(Protocol):
    def feed(self, line: str, *, prefix: str = "") -> None: ...


@dataclass
class PlaybookResult:
    """Result of a synchronous playbook run."""

    returncode: int
    logs: list[str]

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def _terminate_process_group(pid: int) -> None:
    """Terminate a runner and descendants, escalating if they ignore SIGTERM."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except ProcessLookupError:
        return
    # The caller waits for the direct child. Escalate immediately so descendants
    # cannot keep stdout/stderr pipes open and strand the runner indefinitely.
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except ProcessLookupError:
        pass


def _as_text(value: str | bytes | None) -> str:
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return value or ""


@contextmanager
def _extra_vars_file(root: Path, values: Mapping[str, object] | None) -> Iterator[list[str]]:
    if not values:
        yield []
        return
    invalid = sorted(key for key in values if _ANSIBLE_VARIABLE.fullmatch(key) is None)
    if invalid:
        raise ValueError(f"Invalid Ansible variable name: {invalid[0]}")

    directory = root.resolve() / ".homelab-state" / "ansible"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    descriptor, temporary_name = tempfile.mkstemp(prefix="extra-vars-", suffix=".yaml", dir=directory)
    path = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            yaml.safe_dump(dict(values), handle, default_flow_style=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        yield ["-e", f"@{path}"]
    finally:
        path.unlink(missing_ok=True)


def _merged_extra_vars(root: Path, values: Mapping[str, object] | None) -> dict[str, object]:
    merged: dict[str, object] = dict(deployment_secret_variables(root))
    for key, value in (values or {}).items():
        if key in merged and merged[key] != value:
            raise ValueError(f"Caller cannot override service-owned Ansible secret variable: {key}")
        merged[key] = value
    return merged


def run_playbook_sync(
    root: Path,
    playbook: Path,
    *,
    inventory: Path | None = None,
    limit: str | None = None,
    extra_vars: Mapping[str, object] | None = None,
    extra_args: list[str] | None = None,
    on_log: Callable[[str], None] | None = None,
    timeout: float = _ANSIBLE_TIMEOUT_SECONDS,
) -> PlaybookResult:
    """Run an Ansible playbook synchronously, capturing and logging output.

    Args:
        root: Homelab repo root (for resolving ansible-playbook binary + extra vars).
        playbook: Path to the playbook .yml file.
        inventory: Path to the inventory file. If None, auto-resolved from
            automation/ansible/inventory/hosts.yml (written first if missing).
        limit: Optional --limit host pattern.
        extra_vars: Optional typed mapping written to an owner-only ephemeral vars file.
        extra_args: Optional list of arbitrary trailing args (e.g. ["--tags", "headscale"]).
        on_log: Optional callback invoked once per output line (also collected in logs).

    Returns:
        PlaybookResult with returncode and collected logs.
    """
    logs: list[str] = []

    def log(msg: str) -> None:
        logs.append(msg)
        if on_log:
            on_log(msg)

    if not playbook.is_file():
        log(f"Playbook not found: {playbook}")
        return PlaybookResult(returncode=1, logs=logs)

    if inventory is None:
        inventory = root / "automation" / "ansible" / "inventory" / "hosts.yml"

    ansible = resolve_tool("ansible-playbook", root) or "ansible-playbook"
    try:
        with _extra_vars_file(root, _merged_extra_vars(root, extra_vars)) as extra_var_args:
            cmd = [
                ansible,
                "-i",
                str(inventory),
                *generated_extra_vars(root),
                *extra_var_args,
                str(playbook),
            ]
            if limit:
                cmd.extend(["--limit", limit])
            if extra_args:
                cmd.extend(extra_args)

            proc = subprocess.Popen(
                cmd,
                cwd=str(root / "automation" / "ansible"),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
            )
            try:
                stdout, stderr = proc.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                _terminate_process_group(proc.pid)
                stdout, stderr = proc.communicate()
                output = _as_text(stdout or exc.stdout) + _as_text(stderr or exc.stderr)
                for line in output.splitlines():
                    if line.strip():
                        log(line)
                log(f"Ansible playbook timed out after {timeout:g}s")
                return PlaybookResult(returncode=124, logs=logs)
    except ValueError as exc:
        log(str(exc))
        return PlaybookResult(returncode=2, logs=logs)
    output = _as_text(stdout) + _as_text(stderr)
    for line in output.splitlines():
        if line.strip():
            log(line)

    return PlaybookResult(returncode=proc.returncode, logs=logs)


async def run_playbook_streaming(
    root: Path,
    playbook: Path,
    inventory: Path,
    on_log: Callable[[str], None],
    *,
    limit: str | None = None,
    extra_vars: Mapping[str, object] | None = None,
    progress: ProgressReporter | None = None,
    on_output: Callable[[str], None] | None = None,
    timeout: float = _ANSIBLE_TIMEOUT_SECONDS,
) -> int:
    """Run an Ansible playbook async, streaming output line-by-line.

    Used by the deploy workflow SSE endpoint where output must stream to the
    browser in real time. `progress` (if provided) must have a `.feed(text, prefix)`
    method; otherwise lines go to on_log with a "  " prefix.

    Returns the process exit code.
    """
    if not playbook.is_file():
        on_log(f"  Playbook not found: {playbook}")
        return 1

    ansible = resolve_tool("ansible-playbook", root) or "ansible-playbook"
    try:
        with _extra_vars_file(root, _merged_extra_vars(root, extra_vars)) as extra_var_args:
            cmd = [
                ansible,
                "-i",
                str(inventory),
                *generated_extra_vars(root),
                *extra_var_args,
                str(playbook),
            ]
            if limit:
                cmd.extend(["--limit", limit])

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(root / "automation" / "ansible"),
                limit=_ANSIBLE_STREAM_LIMIT,
                start_new_session=True,
            )
            stdout_reader = proc.stdout
            if stdout_reader is None:
                proc.kill()
                await proc.wait()
                raise RuntimeError("Ansible output pipe was not created")

            async def consume() -> None:
                while True:
                    try:
                        raw = await stdout_reader.readline()
                    except ValueError:
                        raw = await stdout_reader.read(65536)
                        if not raw:
                            break
                    if not raw:
                        break
                    text = raw.decode(errors="replace").rstrip()
                    if len(text) > 2000:
                        text = text[:2000] + "… [truncated]"
                    if on_output is not None:
                        on_output(text)
                    if progress is not None:
                        progress.feed(text, prefix="  ")
                    else:
                        on_log(f"  {text}")
                await proc.wait()

            try:
                await asyncio.wait_for(consume(), timeout=timeout)
            except TimeoutError:
                _terminate_process_group(proc.pid)
                await proc.wait()
                on_log(f"  Ansible playbook timed out after {timeout:g}s")
                return 124
            except asyncio.CancelledError:
                _terminate_process_group(proc.pid)
                await proc.wait()
                raise
            return int(proc.returncode or 0)
    except ValueError as exc:
        on_log(f"  {exc}")
        return 2
