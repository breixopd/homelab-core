"""Build, push, and sync custom Docker images without CI."""

from __future__ import annotations

import json
from pathlib import Path

import click

from toolkit.cli.context import load_root_config
from toolkit.core.images.catalog import DEFAULT_REGISTRY, DEFAULT_TAG, custom_images, nodes_for_image, resolve_image_tag
from toolkit.core.images.locks import (
    apply_image_locks,
    discover_image_locks,
    load_image_lock_cache,
    resolve_image_locks,
    save_image_lock_cache,
)
from toolkit.core.images.publish import (
    audit_images,
    build_images,
    export_image_bundle,
    push_images,
    smoke_test_images,
    sync_images_to_guests,
    verify_guest_images,
)


@click.group()
def images() -> None:
    """Lock runtime images and build, publish, or sync custom images."""


@images.command("list")
@click.option("--ci", "ci_only", is_flag=True, help="Include only images enabled for CI")
@click.option("--json", "as_json", is_flag=True, help="Emit a compact JSON build matrix")
@click.option("--names-only", is_flag=True, help="Emit one image name per line")
@click.pass_context
def images_list(ctx, ci_only, as_json, names_only):
    """Show custom images and target nodes."""
    if as_json and names_only:
        raise click.ClickException("--json and --names-only are mutually exclusive")
    root = Path(ctx.obj["root"])
    selected = [image for image in custom_images(root) if image.ci or not ci_only]
    if as_json:
        plan = [
            {
                "name": image.name,
                "repository": image.repository,
                "context": image.context,
                "dockerfile": image.dockerfile or f"{image.context.rstrip('/')}/Dockerfile",
                "platforms": ",".join(image.platforms),
            }
            for image in selected
        ]
        click.echo(json.dumps(plan, separators=(",", ":")))
        return
    if names_only:
        for image in selected:
            click.echo(image.name)
        return
    _root, cfg = load_root_config(ctx)
    click.echo(f"{'name':<16} {'nodes':<20} context")
    for image in selected:
        click.echo(f"{image.name:<16} {','.join(nodes_for_image(cfg, image, root)):<20} {image.context}")


@images.command("build")
@click.option("--registry", default=None, help=f"Image registry prefix (default: config or {DEFAULT_REGISTRY})")
@click.option("--tag", default=None, help=f"Image tag (default: config or {DEFAULT_TAG})")
@click.option("--image", "names", multiple=True, help="Build only these images (default: all)")
@click.pass_context
def images_build(ctx, registry, tag, names):
    """Build custom images locally with registry tags."""
    root, cfg = load_root_config(ctx)
    reg = registry or cfg.images.registry
    tg = resolve_image_tag(root, tag or cfg.images.tag)
    name_tuple = tuple(names) if names else None

    def on_log(msg: str) -> None:
        click.echo(msg)

    try:
        built = build_images(root, registry=reg, tag=tg, names=name_tuple, on_log=on_log)
    except (RuntimeError, FileNotFoundError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Built {len(built)} image(s)")


@images.command("push")
@click.option("--registry", default=None)
@click.option("--tag", default=None)
@click.option("--image", "names", multiple=True)
@click.pass_context
def images_push(ctx, registry, tag, names):
    """Push built images to a registry (`docker login` first)."""
    root, cfg = load_root_config(ctx)
    reg = registry or cfg.images.registry
    tg = resolve_image_tag(root, tag or cfg.images.tag)
    name_tuple = tuple(names) if names else None

    def on_log(msg: str) -> None:
        click.echo(msg)

    try:
        pushed = push_images(root=root, registry=reg, tag=tg, names=name_tuple, on_log=on_log)
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Pushed {len(pushed)} image(s) to {reg}")


@images.command("sync")
@click.option(
    "--node",
    "vms",
    multiple=True,
    help="Target machine ID; repeat for multiple machines (default all enabled)",
)
@click.option("--image", "names", multiple=True, help="Sync only these service-owned images (default: all)")
@click.option("--registry", default=None)
@click.option("--tag", default=None)
@click.option(
    "--source",
    type=click.Choice(("auto", "registry", "local")),
    default=None,
    help="Image source (default: config; auto pulls then builds only registry misses)",
)
@click.pass_context
def images_sync(ctx, vms, names, registry, tag, source):
    """Reconcile custom images on managed machines."""
    root, cfg = load_root_config(ctx)
    reg = registry or cfg.images.registry
    tg = resolve_image_tag(root, tag or cfg.images.tag)
    vm_tuple = tuple(vms) if vms else None
    unknown = sorted(set(vm_tuple or ()) - set(cfg.enabled_nodes))
    if unknown:
        raise click.ClickException(f"Unknown or disabled machine(s): {', '.join(unknown)}")

    def on_log(msg: str) -> None:
        click.echo(msg)

    try:
        lines = sync_images_to_guests(
            root,
            cfg,
            registry=reg,
            tag=tg,
            vms=vm_tuple,
            names=tuple(names) if names else None,
            source=source or cfg.images.source,
            on_log=on_log,
        )
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    for line in lines:
        click.echo(line)
    click.echo("Image sync complete")


@images.command("export")
@click.argument("dest", type=click.Path(path_type=str))
@click.option("--registry", default=None)
@click.option("--tag", default=None)
@click.option("--image", "names", multiple=True)
@click.option("--no-build", is_flag=True)
@click.pass_context
def images_export(ctx, dest, registry, tag, names, no_build):
    """Save image tarball for offline transfer."""
    from pathlib import Path

    root, cfg = load_root_config(ctx)
    reg = registry or cfg.images.registry
    tg = resolve_image_tag(root, tag or cfg.images.tag)
    name_tuple = tuple(names) if names else None
    try:
        path = export_image_bundle(
            root,
            Path(dest),
            registry=reg,
            tag=tg,
            names=name_tuple,
            build=not no_build,
        )
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Exported to {path}")


@images.command("verify")
@click.option("--node", "vms", multiple=True, help="Target machine ID; repeat for multiple machines")
@click.option("--image", "names", multiple=True, help="Verify only these service-owned images (default: all)")
@click.option("--registry", default=None)
@click.option("--tag", default=None)
@click.pass_context
def images_verify(ctx, vms, names, registry, tag):
    """Verify custom images exist on managed machines."""
    root, cfg = load_root_config(ctx)
    reg = registry or cfg.images.registry
    tg = resolve_image_tag(root, tag or cfg.images.tag)
    vm_tuple = tuple(vms) if vms else None
    unknown = sorted(set(vm_tuple or ()) - set(cfg.enabled_nodes))
    if unknown:
        raise click.ClickException(f"Unknown or disabled machine(s): {', '.join(unknown)}")

    def on_log(msg: str) -> None:
        click.echo(msg)

    try:
        ok, _lines = verify_guest_images(
            cfg,
            root,
            registry=reg,
            tag=tg,
            vms=vm_tuple,
            names=tuple(names) if names else None,
            on_log=on_log,
        )
    except ValueError as exc:
        raise click.ClickException(str(exc)) from exc
    if not ok:
        raise SystemExit(1)


@images.command("test")
@click.option("--registry", default=DEFAULT_REGISTRY)
@click.option("--tag", default=None, help=f"Image tag (default: automatic commit tag; fallback: {DEFAULT_TAG})")
@click.option("--image", "names", multiple=True, required=True)
@click.pass_context
def images_test(ctx, registry, tag, names):
    """Run manifest-declared smoke tests for built images."""
    root = Path(ctx.obj["root"])
    selected_tag = resolve_image_tag(root, tag or "auto")
    try:
        tested = smoke_test_images(root, registry=registry, tag=selected_tag, names=tuple(names), on_log=click.echo)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Tested {len(tested)} image(s)")


@images.command("lock")
@click.option("--write", is_flag=True, help="Atomically write resolved SHA-256 digests to service Compose files")
@click.option("--refresh", is_flag=True, help="Resolve existing locks as well as missing digests")
@click.option("--plugin", "plugins", multiple=True, help="Limit resolution to these service plugins")
@click.option("--workers", type=click.IntRange(1, 16), default=6, show_default=True)
@click.pass_context
def images_lock(ctx, write, refresh, plugins, workers):
    """Resolve service-owned runtime tags to immutable OCI index digests."""
    root = Path(ctx.obj["root"])
    try:
        locks = discover_image_locks(root, include_pinned=refresh)
        if plugins:
            selected = set(plugins)
            locks = tuple(lock for lock in locks if lock.plugin in selected)
        if not locks:
            click.echo("Every selected runtime image already has an immutable digest lock")
            return

        cache = load_image_lock_cache(root)
        references = {lock.version_ref for lock in locks}
        resolved = {reference: image for reference, image in cache.items() if reference in references}
        for image in resolved.values():
            platforms = ", ".join(image.platforms) or "platform metadata unavailable"
            click.echo(f"[cache] {image.version_ref} -> {image.digest} ({platforms})")
        pending = tuple(lock for lock in locks if lock.version_ref not in resolved)

        def on_progress(done, total, image) -> None:
            platforms = ", ".join(image.platforms) or "platform metadata unavailable"
            click.echo(f"[{done}/{total}] {image.version_ref} -> {image.digest} ({platforms})")
            resolved[image.version_ref] = image
            save_image_lock_cache(root, resolved)

        if pending:
            fresh = resolve_image_locks(pending, max_workers=workers, on_progress=on_progress)
            resolved.update(fresh)
        drifted = tuple(
            lock for lock in locks if lock.digest is not None and resolved[lock.version_ref].digest != lock.digest
        )
        if drifted:
            click.echo(f"Detected digest drift in {len(drifted)} runtime declaration(s)")
        if not write:
            click.echo(f"Resolved {len(resolved)} image(s); no files changed without --write")
            return
        changed = apply_image_locks(locks, {reference: image.digest for reference, image in resolved.items()})
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Updated {len(changed)} service Compose file(s)")


@images.command("audit")
@click.option("--image", "names", multiple=True, help="Audit only these images (default: every declared audit)")
@click.pass_context
def images_audit(ctx, names):
    """Run manifest-declared blocking dependency audits."""
    root = Path(ctx.obj["root"])
    try:
        audited = audit_images(root, names=tuple(names) if names else None, on_log=click.echo)
    except (RuntimeError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Audited {len(audited)} image(s)")
