"""Timestamped deploy run logs and human-readable progress parsing."""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from toolkit.core.state.paths import deploy_log_dir

ProgressCallback = Callable[[dict[str, str]], None]

_TASK_RE = re.compile(r"^\s*TASK \[([^\]]+)\]")
_PLAY_RE = re.compile(r"^\s*PLAY \[([^\]]+)\]")
_WAVE_WAIT_RE = re.compile(r"Waiting for wave '([^']+)'")
_WAVE_OK_RE = re.compile(r"Wave '([^']+)' (?:healthy|did not)")
_WAVE_NODE_RE = re.compile(r"=== (\w+) Node")
_ANSIBLE_RESULT_RE = re.compile(
    r"^(?:ok|changed|skipping|included|rescued|ignored|failed|unreachable):\s*\[",
    re.IGNORECASE,
)
_OPERATOR_PROGRESS_PREFIXES = (
    "Building ",
    "Checking ",
    "Ensuring ",
    "Generating ",
    "Loading ",
    "Pulling ",
    "Running ",
    "Step:",
    "Transferring ",
    "Using ",
    "Waiting ",
)


@dataclass
class DeployProgressSnapshot:
    """Live deploy progress for CLI echo and WebUI status panel."""

    step: str = ""
    node: str = ""
    ansible_task: str = ""
    compose_wave: str = ""
    detail: str = ""
    percent: str = ""

    def as_dict(self) -> dict[str, str]:
        return {k: v for k, v in asdict(self).items() if v}


def default_deploy_log_path(root: Path) -> Path:
    """Return a fresh log path for the current deploy run."""
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    log_dir = deploy_log_dir(root)
    log_dir.mkdir(parents=True, exist_ok=True)
    return log_dir / f"deploy-{ts}.log"


def should_echo_deploy_line(text: str) -> bool:
    """Keep the console progress-oriented while the operation log stays complete."""
    stripped = text.strip()
    if not stripped:
        return False
    lower = stripped.lower()
    if stripped.startswith("▶") or stripped.startswith(_OPERATOR_PROGRESS_PREFIXES):
        return True
    if stripped.startswith(("✗", "⚠")) or "warning:" in lower or "plugin error:" in lower:
        return True
    if "fatal:" in lower or "failed!" in lower or "unreachable!" in lower:
        return True
    if stripped.startswith("Summary:"):
        return True
    if stripped.startswith("[") and any(marker in stripped for marker in ("→", "✓", "✗", "WARNING", "Plugin error")):
        return True
    if _ANSIBLE_RESULT_RE.match(stripped):
        return False
    if _PLAY_RE.match(stripped) or _TASK_RE.match(stripped) or stripped.startswith("PLAY RECAP"):
        return False
    return False


class DeployLogWriter:
    """Append deploy output to a file and optionally echo to a secondary sink."""

    def __init__(self, log_path: Path, *, echo: Callable[[str], None] | None = None) -> None:
        self.log_path = log_path.resolve()
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self._echo = echo
        self._fp = self.log_path.open("a", encoding="utf-8")

    def write(self, msg: str) -> None:
        self._fp.write(msg + "\n")
        self._fp.flush()
        if self._echo is not None:
            self._echo(msg)

    def close(self) -> None:
        self._fp.close()

    def __enter__(self) -> DeployLogWriter:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def format_progress_line(snap: DeployProgressSnapshot) -> str:
    """Single-line human summary for stdout / deploy log."""
    parts: list[str] = []
    if snap.step:
        parts.append(snap.step)
    if snap.node:
        parts.append(f"node={snap.node}")
    if snap.compose_wave:
        parts.append(f"wave={snap.compose_wave}")
    elif snap.ansible_task:
        parts.append(f"task={snap.ansible_task}")
    if snap.detail:
        parts.append(snap.detail[:140])
    return "▶ " + " · ".join(parts) if parts else ""


def parse_deploy_stream_line(
    text: str,
    snap: DeployProgressSnapshot,
    hostname_to_node: dict[str, str] | None = None,
) -> bool:
    """Update *snap* from ansible/compose output. Return True if progress changed."""
    changed = False
    stripped = text.strip()
    if not stripped:
        return False

    play = _PLAY_RE.match(stripped)
    if play:
        host = play.group(1).strip()
        node = (hostname_to_node or {}).get(host, host)
        if node != snap.node:
            snap.node = node
            snap.ansible_task = ""
            snap.compose_wave = ""
            changed = True

    task = _TASK_RE.match(stripped)
    if task:
        name = task.group(1).strip()
        if name != snap.ansible_task:
            snap.ansible_task = name
            changed = True

    node_header = _WAVE_NODE_RE.search(stripped)
    if node_header:
        snap.node = node_header.group(1).lower()
        changed = True

    for pat in (_WAVE_WAIT_RE, _WAVE_OK_RE):
        wave = pat.search(stripped)
        if wave:
            snap.compose_wave = wave.group(1)
            changed = True

    if "Launch staggered Docker Compose" in stripped or "staggered-compose" in stripped.lower():
        snap.compose_wave = "starting"
        changed = True

    if "fatal:" in stripped.lower():
        snap.detail = stripped[:200]
        changed = True
    elif snap.detail and not stripped.startswith("▶"):
        snap.detail = ""

    return changed


class DeployProgressReporter:
    """Feed raw deploy output; emit concise progress lines and optional callbacks."""

    def __init__(
        self,
        *,
        on_log: Callable[[str], None],
        on_progress: ProgressCallback | None = None,
        step: str = "",
        hostname_to_node: dict[str, str] | None = None,
    ) -> None:
        self._on_log = on_log
        self._on_progress = on_progress
        self._hostname_to_node = dict(hostname_to_node or {})
        self._snap = DeployProgressSnapshot(step=step)
        self._last_progress_line = ""

    @property
    def snapshot(self) -> DeployProgressSnapshot:
        return self._snap

    def set_step(self, step: str) -> None:
        if step != self._snap.step:
            self._snap.step = step
            self._emit_progress()

    def feed(self, text: str, *, log_full: bool = True, prefix: str = "") -> None:
        line = text.rstrip()
        if log_full and line:
            self._on_log(f"{prefix}{line}")
        if parse_deploy_stream_line(line, self._snap, self._hostname_to_node):
            self._emit_progress()

    def note(self, detail: str) -> None:
        self._snap.detail = detail
        self._emit_progress()

    def _emit_progress(self) -> None:
        progress_line = format_progress_line(self._snap)
        if not progress_line or progress_line == self._last_progress_line:
            return
        self._last_progress_line = progress_line
        self._on_log(progress_line)
        if self._on_progress is not None:
            self._on_progress(self._snap.as_dict())
