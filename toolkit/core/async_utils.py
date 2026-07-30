"""Async helpers with an application-owned bounded blocking executor."""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from typing import ParamSpec, TypeVar

P = ParamSpec("P")
R = TypeVar("R")

_BLOCKING_EXECUTOR = ThreadPoolExecutor(max_workers=8, thread_name_prefix="homelab-blocking")


async def run_blocking(function: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs) -> R:
    """Run blocking work without attaching an executor to the event-loop lifecycle."""
    context = contextvars.copy_context()
    call = partial(function, *args, **kwargs)
    return await asyncio.get_running_loop().run_in_executor(
        _BLOCKING_EXECUTOR,
        context.run,
        call,
    )
