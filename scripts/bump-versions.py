#!/usr/bin/env python3
"""Check Docker images in docker-compose.yml against registries for newer tags.

Usage:
    scripts/bump-versions.py                  # report outdated images
    scripts/bump-versions.py --image sonarr   # check single image
"""

from __future__ import annotations

import argparse
import json
import re
import signal
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from toolkit.core.ops.version_policy import select_latest_compatible


class _TimeoutError(Exception):
    """Raised when check_images exceeds the global timeout."""


def _timeout_handler(signum: int, _frame) -> None:
    raise _TimeoutError(f"check_images timed out after {signum}s")


REPO_ROOT = Path(__file__).resolve().parent.parent
COMPOSE_FILE = REPO_ROOT / "docker-compose.yml"
CACHE_FILE = REPO_ROOT / "generated" / "updates-cache.json"
CACHE_TTL = 86400  # 24 hours in seconds

# Registries known to require special API handling.
REGISTRY_API = {
    "docker.io": "https://hub.docker.com/v2/repositories/{path}/tags?page_size=50&ordering=last_updated",
    "ghcr.io": "https://ghcr.io/v2/{path}/tags/list",
    "gcr.io": "https://gcr.io/v2/{path}/tags/list",
    "registry.gitlab.com": "https://registry.gitlab.com/v2/{path}/tags/list",
    "lscr.io": "https://hub.docker.com/v2/repositories/{path}/tags?page_size=50&ordering=last_updated",
    "quay.io": "https://quay.io/v2/{path}/tags/list",
}

# Docker Hub library images (single-name like ``postgres``, ``redis``).
LIBRARY_NAMESPACE = "library"


def _changelog_url(registry: str, path: str, tag: str) -> str:
    """Generate a changelog/releases URL for an image based on its registry."""
    if registry == "docker.io":
        # Docker Hub: library images (official) vs user repos
        if path.startswith("library/"):
            repo_name = path.split("/", 1)[1]
            return f"https://hub.docker.com/_/{repo_name}/tags"
        return f"https://hub.docker.com/r/{path}/tags"
    if registry == "ghcr.io":
        # GitHub Container Registry: path is org/repo
        return f"https://github.com/{path}/releases"
    if registry == "lscr.io":
        # LinuxServer.io: path is linuxserver/imagename
        parts = path.split("/")
        if len(parts) == 2 and parts[0] == "linuxserver":
            return f"https://github.com/linuxserver/docker-{parts[1]}/releases"
        return f"https://fleet.linuxserver.io/image?name={path}"
    if registry == "quay.io":
        return f"https://quay.io/repository/{path}?tag={tag}&tab=tags"
    if registry == "gcr.io":
        return f"https://gcr.io/{path}/tags"
    return ""


def _read_cache(cache_file: Path = CACHE_FILE) -> dict | None:
    """Read cached update data if it exists and is fresh."""
    if not cache_file.exists():
        return None
    try:
        data = json.loads(cache_file.read_text())
        cached_at = data.get("cached_at", 0)
        age = time.time() - cached_at
        if age < CACHE_TTL:
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return None


def _write_cache(report: list[dict], cache_file: Path = CACHE_FILE) -> None:
    """Write update report to cache file."""
    cache_data = {
        "cached_at": time.time(),
        "cached_at_iso": datetime.now(UTC).isoformat(),
        "updates": [
            {
                "service": r["service"],
                "image": r["image"],
                "registry": r.get("registry", ""),
                "current": r["current"],
                "latest": r.get("latest"),
                "changelog_url": r.get("changelog_url", ""),
                "needs_update": r.get("needs_update", False),
                "checked": r.get("checked", False),
                "error": r.get("error"),
                "tags_found": r.get("tags_found", 0),
            }
            for r in report
        ],
    }
    cache_file.parent.mkdir(parents=True, exist_ok=True)
    cache_file.write_text(json.dumps(cache_data, indent=2, default=str) + "\n")


# ── helpers ──────────────────────────────────────────────────────────────────


def _parse_image_ref(image: str) -> tuple[str, str, str, str]:
    """Parse an image reference into ``(registry, path, tag, full_ref)``."""
    tagged_ref = image.split("@", 1)[0]
    last_slash = tagged_ref.rfind("/")
    last_colon = tagged_ref.rfind(":")
    if last_colon > last_slash:
        path, tag = tagged_ref[:last_colon], tagged_ref[last_colon + 1 :]
    else:
        path, tag = tagged_ref, "latest"

    registry = "docker.io"
    if "/" in path and ("." in path.split("/")[0] or ":" in path.split("/")[0]):
        registry, path = path.split("/", 1)

    # Docker Hub library image (no slash in path).
    if registry == "docker.io" and "/" not in path:
        path = f"{LIBRARY_NAMESPACE}/{path}"

    return registry, path, tag, image


@dataclass(frozen=True, slots=True)
class RegistryTags:
    tags: tuple[str, ...] = ()
    error: str | None = None


def _read_json(request: urllib.request.Request, timeout: int) -> dict[str, Any]:
    for attempt in range(3):
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = json.loads(response.read().decode())
            break
        except urllib.error.HTTPError as exc:
            try:
                if exc.code not in {429, 500, 502, 503, 504} or attempt == 2:
                    raise
                retry_after = exc.headers.get("Retry-After", "")
                try:
                    delay = min(max(float(retry_after), 0.0), 5.0)
                except ValueError:
                    delay = 0.25 * (2**attempt)
                time.sleep(delay)
            finally:
                exc.close()
    if not isinstance(payload, dict):
        raise ValueError("registry response is not a JSON object")
    return payload


def _bearer_request(
    request: urllib.request.Request,
    challenge: str,
    *,
    path: str,
    timeout: int,
) -> dict[str, Any]:
    if not challenge.lower().startswith("bearer "):
        raise ValueError("registry did not provide a Bearer challenge")
    parameters = dict(re.findall(r'(\w+)="([^"]*)"', challenge[7:]))
    realm = parameters.pop("realm", "")
    parsed_realm = urllib.parse.urlsplit(realm)
    if parsed_realm.scheme != "https" or not parsed_realm.netloc or parsed_realm.username:
        raise ValueError("registry provided an invalid token endpoint")
    parameters.setdefault("scope", f"repository:{path}:pull")
    separator = "&" if parsed_realm.query else "?"
    token_request = urllib.request.Request(
        f"{realm}{separator}{urllib.parse.urlencode(parameters)}",
        headers={"Accept": "application/json"},
    )
    token_payload = _read_json(token_request, timeout)
    token = token_payload.get("token") or token_payload.get("access_token")
    if not isinstance(token, str) or not token:
        raise ValueError("registry token response is missing a token")
    request.add_header("Authorization", f"Bearer {token}")
    return _read_json(request, timeout)


def _fetch_tags(registry: str, path: str, timeout: int = 5) -> RegistryTags:
    """Fetch available tags for a repo from the registry API."""
    api_url = REGISTRY_API.get(registry)
    if not api_url:
        return RegistryTags(error=f"registry {registry} is not supported")

    url = api_url.format(path=path)
    headers = {"Accept": "application/json"}
    req = urllib.request.Request(url, headers=headers)

    try:
        data = _read_json(req, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 401:
            error = f"HTTP {exc.code}"
            print(f"  [warn] failed to fetch tags from {url}: {error}", file=sys.stderr)
            return RegistryTags(error=error)
        try:
            data = _bearer_request(
                req,
                exc.headers.get("WWW-Authenticate", ""),
                path=path,
                timeout=timeout,
            )
        except (urllib.error.HTTPError, urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as auth_exc:
            error = f"authentication failed: {auth_exc}"
            print(f"  [warn] failed to fetch tags from {url}: {error}", file=sys.stderr)
            return RegistryTags(error=error)
    except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
        error = str(exc)[:200]
        print(f"  [warn] failed to fetch tags from {url}: {error}", file=sys.stderr)
        return RegistryTags(error=error)

    # Docker Hub paginated response.
    if "results" in data:
        results = data["results"]
        if not isinstance(results, list):
            return RegistryTags(error="registry results are not a list")
        return RegistryTags(
            tags=tuple(r["name"] for r in results if isinstance(r, dict) and isinstance(r.get("name"), str))
        )
    # OCI Distribution Spec (ghcr.io, gcr.io, quay.io).
    if "tags" in data:
        tags = data["tags"]
        if not isinstance(tags, list):
            return RegistryTags(error="registry tags are not a list")
        return RegistryTags(tags=tuple(tag for tag in tags if isinstance(tag, str)))
    return RegistryTags(error="registry response contains no tags")


def _find_latest_stable(tags: list[str], current_tag: str) -> str | None:
    """Return the newest compatible version tag without changing release channels."""
    return select_latest_compatible(tags, current_tag)


# ── compose parsing ──────────────────────────────────────────────────────────


def _iter_service_images(compose_path: Path) -> list[dict[str, Any]]:
    """Yield each service image entry from docker-compose.yml."""
    document = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    services = document.get("services") if isinstance(document, dict) else None
    if not isinstance(services, dict):
        return []
    return [
        {"service": name, "image": service["image"]}
        for name, service in services.items()
        if isinstance(name, str) and isinstance(service, dict) and isinstance(service.get("image"), str)
    ]


def _display_path(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


# ── checking ─────────────────────────────────────────────────────────────────


def check_images(compose_file: Path, *, image_filter: str | None = None) -> list[dict]:
    """Check all service images and return a report."""
    entries = _iter_service_images(compose_file)
    report: list[dict] = []

    for entry in entries:
        svc_name = entry["service"]
        raw_image = entry["image"]

        # Resolve YAML anchors to their concrete value.
        if raw_image.startswith("*"):
            continue

        if image_filter and image_filter.lower() not in svc_name.lower():
            continue

        # Skip local-only images.
        if ":local" in raw_image or raw_image.startswith("${HOMELAB_"):
            continue

        registry, path, current_tag, _ = _parse_image_ref(raw_image)

        registry_result = _fetch_tags(registry, path)
        tags = list(registry_result.tags)
        latest = _find_latest_stable(tags, current_tag) if tags else None

        needs_update = bool(latest and latest != current_tag)

        changelog_url = _changelog_url(registry, path, current_tag) if latest else ""

        report.append(
            {
                "service": svc_name,
                "image": raw_image,
                "registry": registry,
                "current": current_tag,
                "latest": latest,
                "changelog_url": changelog_url,
                "tags_found": len(tags),
                "checked": registry_result.error is None,
                "error": registry_result.error,
                "needs_update": needs_update,
                "entry": entry,
            }
        )

    return report


def print_report(report: list[dict]) -> None:
    """Print a human-readable table of version checks."""
    outdated = [r for r in report if r["needs_update"]]
    up_to_date = [r for r in report if not r["needs_update"] and r.get("checked")]

    if outdated:
        print(f"\n{'Outdated images':━^72}")
        print(f"{'Service':<24} {'Current':<20} {'Latest':<20}")
        print("─" * 64)
        for r in sorted(outdated, key=lambda x: x["service"]):
            print(f"{r['service']:<24} {r['current']:<20} {r['latest']:<20}")
        print()

    if up_to_date:
        print(f"{'Up-to-date images':━^72}")
        for r in sorted(up_to_date, key=lambda x: x["service"]):
            mark = "✓"
            print(f"  {mark} {r['service']:<22} {r['current']}")
        print()

    unchecked = [r for r in report if not r.get("checked")]
    if unchecked:
        print(f"{'Unchecked (no registry data)':━^72}")
        for r in sorted(unchecked, key=lambda x: x["service"]):
            print(f"  ? {r['service']:<22} {r['current']} ({r['registry']}): {r.get('error', 'unknown error')}")
        print()

    print(
        f"Total: {len(report)} images, "
        f"{len(outdated)} outdated, "
        f"{len(up_to_date)} up-to-date, "
        f"{len(unchecked)} unchecked"
    )


# ── CLI ──────────────────────────────────────────────────────────────────────


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check Docker images in docker-compose.yml for newer versions",
    )
    parser.add_argument("--image", "-i", help="Check only images matching this service name")
    parser.add_argument(
        "--compose-file", default=str(COMPOSE_FILE), help=f"Path to docker-compose.yml (default: {COMPOSE_FILE})"
    )
    parser.add_argument("--cache-file", default=str(CACHE_FILE), help="Path to the update discovery cache")
    parser.add_argument("--json", action="store_true", help="Output JSON report to stdout")
    parser.add_argument("--cache", action="store_true", help="Use cached results if fresh (skip API calls)")
    parser.add_argument("--refresh", action="store_true", help="Force refresh cache (bypass TTL)")
    parser.add_argument("--timeout", type=int, default=120, help="Global timeout in seconds (default: 120)")
    args = parser.parse_args(argv)

    compose_path = Path(args.compose_file).resolve()
    cache_file = Path(args.cache_file).resolve()
    if not compose_path.exists():
        print(f"Error: {compose_path} not found", file=sys.stderr)
        return 1

    use_cache = args.cache and not args.refresh
    if use_cache:
        cached = _read_cache(cache_file)
        if cached is not None:
            if args.json:
                json.dump(cached["updates"], sys.stdout, indent=2, default=str)
                print()
                return 0
            print(f"Using cached results from {cached.get('cached_at_iso', 'unknown')}")
            report = cached["updates"]
            print_report(report)  # type: ignore[arg-type]
            return 0

    if not args.json:
        print(f"Checking images in {_display_path(compose_path)} …\n")

    # Set a global timeout so a hanging registry never wedges the script.
    signal.signal(signal.SIGALRM, _timeout_handler)
    signal.alarm(args.timeout)
    try:
        report = check_images(compose_path, image_filter=args.image)
    except _TimeoutError:
        print(
            f"Error: check timed out after {args.timeout}s — some registries may be unreachable.\n"
            "Use --image to check a single service or --timeout to increase the limit.",
            file=sys.stderr,
        )
        return 1
    finally:
        signal.alarm(0)

    # Write cache for future use.
    if report:
        _write_cache(report, cache_file)

    if args.json:
        json.dump(report, sys.stdout, indent=2, default=str)
        print()
        return 0

    if not report:
        print("No images found to check.")
        return 0

    print_report(report)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
