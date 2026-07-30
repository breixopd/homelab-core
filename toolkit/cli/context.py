"""Typed Click-context access without importing the command registry."""

from __future__ import annotations

from pathlib import Path

import click

from toolkit.controller.client import ControllerClient, controller_client_from_environment
from toolkit.core.config.config import Config, load_config
from toolkit.core.config.storage import config_path


def load_root_config(ctx: click.Context) -> tuple[Path, Config]:
    root = Path(ctx.obj["root"])
    return root, load_config(config_path(root))


def load_controller_client(ctx: click.Context) -> ControllerClient:
    client = ctx.obj.get("controller")
    if client is None:
        factory = ctx.obj.get("controller_factory", controller_client_from_environment)
        try:
            client = factory()
        except (OSError, ValueError) as exc:
            root, cfg = load_root_config(ctx)
            if not cfg.is_multi_node:
                raise click.ClickException(
                    "Controller connection is not configured. Start the local controller service."
                ) from exc
            client = ControllerClient.for_managed_ssh(cfg, root)
        ctx.obj["controller"] = client
        ctx.call_on_close(client.close)
    return client
