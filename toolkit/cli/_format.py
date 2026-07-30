"""Phase C: shared rich-based CLI formatting helpers.

A single place for table/panel rendering so all commands use one visual
language (one separator style, one OK/error glyph convention). Renders plain
text when there's no tty (CI, piped) so test asserts + scripts stay stable.
"""

from __future__ import annotations

from collections.abc import Sequence
from io import StringIO
from typing import cast

from rich.console import Console
from rich.panel import Panel
from rich.table import Table


def _plain_console() -> Console:
    """A Console that renders plain text (no ANSI / no color) — for tests + pipes."""
    return Console(file=StringIO(), force_terminal=False, no_color=True, width=120)


def render_status_table(rows: Sequence[Sequence[str | int]], *, columns: Sequence[str]) -> str:
    """Render rows as a rich table; return the rendered string."""
    table = Table(show_header=True, header_style="bold")
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*[str(c) for c in row])
    console = _plain_console()
    console.print(table)
    return cast(StringIO, console.file).getvalue()


def status_panel(*, title: str, body: str) -> str:
    """Render a titled panel wrapping body; return the rendered string."""
    panel = Panel(body, title=title, border_style="blue")
    console = _plain_console()
    console.print(panel)
    return cast(StringIO, console.file).getvalue()


def echo_table(rows: Sequence[Sequence[str | int]], *, columns: Sequence[str]) -> None:
    """Print a rich table to stdout (with color when tty)."""
    table = Table(show_header=True, header_style="bold")
    for col in columns:
        table.add_column(col)
    for row in rows:
        table.add_row(*[str(c) for c in row])
    Console().print(table)


def echo_panel(*, title: str, body: str) -> None:
    """Print a titled panel to stdout (with color when tty)."""
    Console().print(Panel(body, title=title, border_style="blue"))
