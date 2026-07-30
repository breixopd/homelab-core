"""Caddyfile validation using the service-owned runtime image."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)

_CADDY_VALIDATE_LOCAL_TAG = "homelab-caddy-validate:local"
_CADDY_FALLBACK_IMAGE = "caddy:2.11.4-alpine"
_CADDY_CF_PLACEHOLDER = "cfut_caddy_validate_placeholder000000000000000"
_CADDY_BOUNCER_PLACEHOLDER = "caddy_validate_bouncer_key_000000000000000000000000"


def _caddyfile_required_modules(caddyfile: Path) -> frozenset[str]:
    content = caddyfile.read_text(encoding="utf-8", errors="replace")
    required: set[str] = set()
    if "dns cloudflare" in content or "acme_dns cloudflare" in content:
        required.add("dns.providers.cloudflare")
    if "crowdsec {" in content or "\n\t\tcrowdsec\n" in content:
        required.add("http.handlers.crowdsec")
    return frozenset(required)


def _docker_image_exists(ref: str) -> bool:
    if not ref:
        return False
    proc = subprocess.run(
        ["docker", "image", "inspect", ref],
        capture_output=True,
        timeout=15,
        check=False,
    )
    return proc.returncode == 0


def _docker_image_supports(ref: str, required_modules: frozenset[str]) -> bool:
    if not _docker_image_exists(ref):
        return False
    if not required_modules:
        return True
    try:
        proc = subprocess.run(
            ["docker", "run", "--rm", ref, "caddy", "list-modules"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    modules = set(proc.stdout.splitlines()) if proc.returncode == 0 else set()
    return required_modules.issubset(modules)


def _caddy_binary_supports(binary: str, required_modules: frozenset[str]) -> bool:
    if not required_modules:
        return True
    try:
        proc = subprocess.run(
            [binary, "list-modules"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    modules = set(proc.stdout.splitlines()) if proc.returncode == 0 else set()
    return required_modules.issubset(modules)


def _configured_caddy_image(repo_root: Path | None) -> str:
    configured = os.environ.get("HOMELAB_CADDY_IMAGE", "").strip()
    if configured or repo_root is None:
        return configured
    try:
        from toolkit.core.config.config import load_config
        from toolkit.core.config.storage import config_path
        from toolkit.core.images.catalog import custom_images, image_ref, resolve_image_tag

        cfg = load_config(config_path(repo_root))
        image = next((item for item in custom_images(repo_root) if item.name == "caddy"), None)
        if image is None:
            return ""
        return image_ref(cfg.images.registry, image.repository, resolve_image_tag(repo_root, cfg.images.tag))
    except (OSError, RuntimeError, ValueError):
        return ""


def _resolve_caddy_validate_image(caddyfile: Path, repo_root: Path | None) -> str:
    """Pick a Caddy image that can parse the generated Caddyfile."""
    required_modules = _caddyfile_required_modules(caddyfile)
    configured_image = _configured_caddy_image(repo_root)
    for candidate in (configured_image, _CADDY_VALIDATE_LOCAL_TAG, "caddy:local-ci"):
        if candidate and _docker_image_supports(candidate, required_modules):
            return candidate

    # Keep the inexpensive stock fallback for ordinary Caddyfiles, but only
    # after checking the configured runtime image. Formatting and validation
    # must use the same custom binary that deployment will run when one is
    # configured; using a host binary here can silently drift in modules and
    # formatter behavior.
    if not required_modules and not configured_image:
        return _CADDY_FALLBACK_IMAGE

    if configured_image:
        pull = subprocess.run(
            ["docker", "pull", configured_image],
            capture_output=True,
            text=True,
            timeout=600,
            check=False,
        )
        if pull.returncode == 0:
            if _docker_image_supports(configured_image, required_modules):
                return configured_image
            logger.info("Configured Caddy image lacks required modules; using local build")
        else:
            detail = (pull.stderr or pull.stdout or "registry pull failed").strip()
            logger.info("Configured Caddy image unavailable; using local fallback: %s", detail[:300])

    caddy_image = None
    caddy_context = None
    if repo_root is not None and any(
        (repo_root / relative).is_file()
        for relative in ("toolkit/services/caddy/service.yaml", "services/caddy/service.yaml")
    ):
        from toolkit.core.images.catalog import custom_images

        caddy_image = next((image for image in custom_images(repo_root) if image.name == "caddy"), None)
        if caddy_image is not None:
            caddy_context = repo_root / caddy_image.context
    if caddy_context is None:
        # Config initialization and tests can generate into a clean target
        # directory rather than the source checkout. The Caddy image is a
        # service-owned package asset, so retain that build path instead of
        # relying on a previously cached local image.
        packaged_context = Path(__file__).resolve().parent / "image"
        if (packaged_context / "Dockerfile").is_file():
            caddy_context = packaged_context
    if caddy_context and (caddy_context / "Dockerfile").is_file():
        if not _docker_image_supports(_CADDY_VALIDATE_LOCAL_TAG, required_modules):
            logger.info("Building %s for Caddyfile validation", _CADDY_VALIDATE_LOCAL_TAG)
            build_command = ["docker", "build", "-t", _CADDY_VALIDATE_LOCAL_TAG]
            if repo_root is not None and caddy_image is not None and caddy_image.dockerfile:
                build_command.extend(["-f", str(repo_root / caddy_image.dockerfile)])
            build_command.append(str(caddy_context))
            proc = subprocess.run(
                build_command,
                capture_output=True,
                text=True,
                timeout=600,
                check=False,
            )
            if proc.returncode != 0:
                detail = (proc.stderr or proc.stdout or "docker build failed").strip()
                raise ValueError(f"Cannot build Caddy validation image: {detail[:500]}")
        if not _docker_image_supports(_CADDY_VALIDATE_LOCAL_TAG, required_modules):
            raise ValueError("Built Caddy validation image is missing required modules")
        return _CADDY_VALIDATE_LOCAL_TAG

    module_list = ", ".join(sorted(required_modules))
    raise ValueError(
        f"Cannot validate Caddyfile because no available Caddy image provides required modules: {module_list}"
    )


def format_generated_caddyfile(
    generated_dir: Path,
    repo_root: Path | None = None,
) -> None:
    """Format a generated Caddyfile with the service-owned runtime image.

    The deployed Caddy image is authoritative for formatting.  A host Caddy
    binary is intentionally never used, since it may not contain the same
    plugins or formatter version.  Formatting is best-effort when Docker is
    unavailable; syntax validation remains responsible for rejecting invalid
    configuration where a runtime is available.
    """
    caddyfile = generated_dir / "Caddyfile"
    if not caddyfile.is_file():
        raise ValueError("Caddyfile not generated")
    if not shutil.which("docker"):
        logger.warning("Caddyfile formatting deferred because Docker is unavailable")
        return

    docker_probe = subprocess.run(
        ["docker", "info"],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    if docker_probe.returncode != 0:
        detail = (docker_probe.stderr or docker_probe.stdout or "Docker daemon unavailable").strip()
        logger.warning("Caddyfile formatting deferred because Docker is unavailable: %s", detail[:300])
        return

    image = _resolve_caddy_validate_image(caddyfile, repo_root)
    proc = subprocess.run(
        [
            "docker",
            "run",
            "--rm",
            "-i",
            image,
            "caddy",
            "fmt",
            "-",
        ],
        input=caddyfile.read_text(encoding="utf-8"),
        capture_output=True,
        text=True,
        timeout=90,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "caddy fmt failed").strip()
        raise ValueError(f"Caddyfile formatting failed (docker): {detail[:500]}")
    if not proc.stdout.strip():
        raise ValueError("Caddyfile formatting failed (docker): formatter returned empty output")
    caddyfile.write_text(proc.stdout, encoding="utf-8")


def validate_generated_caddyfile(
    generated_dir: Path,
    repo_root: Path | None = None,
) -> None:
    """Validate generated Caddyfile syntax before deploy."""
    if os.environ.get("HOMELAB_DEPLOY_CONTROLLER", "").strip().lower() in ("1", "true", "yes") and not shutil.which(
        "caddy"
    ):
        return
    caddyfile = generated_dir / "Caddyfile"
    if not caddyfile.is_file():
        raise ValueError("Caddyfile not generated")

    environment = {
        **os.environ,
        "CF_API_TOKEN": _CADDY_CF_PLACEHOLDER,
        "CADDY_BOUNCER_API_KEY": _CADDY_BOUNCER_PLACEHOLDER,
    }
    working_directory = str(generated_dir.resolve())

    required_modules = _caddyfile_required_modules(caddyfile)
    caddy_binary = shutil.which("caddy")
    if caddy_binary and _caddy_binary_supports(caddy_binary, required_modules):
        proc = subprocess.run(
            [caddy_binary, "validate", "--config", str(caddyfile), "--adapter", "caddyfile"],
            cwd=working_directory,
            capture_output=True,
            text=True,
            env=environment,
            timeout=30,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "caddy validate failed").strip()
            raise ValueError(f"Caddyfile validation failed: {detail[:500]}")
        return
    if caddy_binary:
        logger.info("Host Caddy binary lacks required modules; using the service-owned image")

    if shutil.which("docker"):
        docker_probe = subprocess.run(
            ["docker", "info"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
        if docker_probe.returncode != 0:
            detail = (docker_probe.stderr or docker_probe.stdout or "Docker daemon unavailable").strip()
            logger.warning("Caddyfile validation deferred because Docker is unavailable: %s", detail[:300])
            return
        image = _resolve_caddy_validate_image(caddyfile, repo_root)
        validation_input = caddyfile.read_text(encoding="utf-8")
        if image == _CADDY_FALLBACK_IMAGE:
            import re

            validation_input = re.sub(
                r"^\s*(acme_dns|dns)\s+cloudflare.*$",
                "# cloudflare DNS (stripped for validation)",
                validation_input,
                flags=re.MULTILINE,
            )
            validation_input = re.sub(
                r"tls\s*\{[^}]*\}",
                "# tls block stripped (no CF module)",
                validation_input,
                flags=re.DOTALL,
            )
        proc = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "-i",
                "-e",
                f"CF_API_TOKEN={environment['CF_API_TOKEN']}",
                "-e",
                f"CADDY_BOUNCER_API_KEY={environment['CADDY_BOUNCER_API_KEY']}",
                image,
                "caddy",
                "validate",
                "--config",
                "-",
                "--adapter",
                "caddyfile",
            ],
            input=validation_input,
            capture_output=True,
            text=True,
            timeout=90,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "caddy validate failed").strip()
            raise ValueError(f"Caddyfile validation failed (docker): {detail[:500]}")
        return

    logger.warning("caddy/docker not available; skipping Caddyfile validation")
