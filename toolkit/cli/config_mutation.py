"""CLI adapter for desired-state mutation conflicts."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import click

from toolkit.core.config.mutations import ConfigurationBusyError, configuration_mutation


@contextmanager
def cli_configuration_mutation(root: Path, operation: str) -> Iterator[None]:
    try:
        with configuration_mutation(root, operation):
            yield
    except ConfigurationBusyError as exc:
        raise click.ClickException(str(exc)) from exc
