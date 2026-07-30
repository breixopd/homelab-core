#!/usr/bin/env python3
"""Check source-controlled framework dependencies for compatible updates.

Runtime containers are immutable and guest operating systems use unattended
security upgrades. This scanner therefore checks only dependencies an operator
can update through the framework release process: the uv lock, checksummed
toolkit binaries, and pinned Ansible collections.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import ipaddress
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import zlib
from datetime import UTC, datetime
from pathlib import Path

import yaml
from toolkit.core.state.files import atomic_write_json

REPO_ROOT = Path(__file__).resolve().parent.parent
CACHE_FILE = REPO_ROOT / "generated" / "framework-updates-cache.json"
CACHE_TTL = 86_400
_UV_UPDATE = re.compile(r"^Update (?P<name>[A-Za-z0-9_.-]+) v(?P<current>\S+) -> v(?P<latest>\S+)$")
_TOOL_RELEASES = {
    "COMPOSE_VERSION": ("docker-compose", "docker/compose"),
    "OPENTOFU_VERSION": ("opentofu", "opentofu/opentofu"),
    "SOPS_VERSION": ("sops", "getsops/sops"),
    "AGE_VERSION": ("age", "FiloSottile/age"),
}
_MAX_REGISTRY_BYTES = 16 * 1024 * 1024
_MAX_PACKAGE_INDEX_BYTES = 64 * 1024 * 1024


class SystemUpdateCheckError(RuntimeError):
    """A source dependency could not be checked reliably."""


def _read_cache() -> dict | None:
    if not CACHE_FILE.exists():
        return None
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
        age = time.time() - float(data.get("cached_at", 0))
        updates = data.get("updates")
        if 0 <= age < CACHE_TTL and isinstance(updates, list) and all(isinstance(item, dict) for item in updates):
            return data
    except (json.JSONDecodeError, OSError, TypeError, ValueError):
        pass
    return None


def _write_cache(report: list[dict]) -> None:
    data = {
        "cached_at": time.time(),
        "cached_at_iso": datetime.now(UTC).isoformat(),
        "updates": report,
    }
    atomic_write_json(CACHE_FILE, data)


def _run(cmd: list[str], timeout: int = 30) -> tuple[int, str]:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        return 1, str(exc)
    return result.returncode, "\n".join(part for part in (result.stdout, result.stderr) if part)


def _read_json(url: str, *, timeout: int = 15) -> dict:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "homelab-toolkit"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(_MAX_REGISTRY_BYTES + 1)
            if len(raw) > _MAX_REGISTRY_BYTES:
                raise SystemUpdateCheckError(
                    f"dependency registry payload exceeds {_MAX_REGISTRY_BYTES} bytes for {url}"
                )
            payload = json.loads(raw.decode("utf-8"))
    except SystemUpdateCheckError:
        raise
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemUpdateCheckError(f"dependency registry request failed for {url}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemUpdateCheckError(f"dependency registry returned invalid data for {url}")
    return payload


def _read_json_list(url: str, *, timeout: int = 15) -> list:
    request = urllib.request.Request(
        url,
        headers={"Accept": "application/json", "User-Agent": "homelab-toolkit"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(_MAX_REGISTRY_BYTES + 1)
            if len(raw) > _MAX_REGISTRY_BYTES:
                raise SystemUpdateCheckError(
                    f"dependency registry payload exceeds {_MAX_REGISTRY_BYTES} bytes for {url}"
                )
            payload = json.loads(raw.decode("utf-8"))
    except SystemUpdateCheckError:
        raise
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SystemUpdateCheckError(f"dependency registry request failed for {url}: {exc}") from exc
    if not isinstance(payload, list):
        raise SystemUpdateCheckError(f"dependency registry returned invalid data for {url}")
    return payload


def _read_text(url: str, *, timeout: int = 15) -> str:
    request = urllib.request.Request(url, headers={"User-Agent": "homelab-toolkit"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read(4096).decode("utf-8").strip()
    except (urllib.error.HTTPError, urllib.error.URLError, OSError, UnicodeError) as exc:
        raise SystemUpdateCheckError(f"dependency registry request failed for {url}: {exc}") from exc


def _read_bytes(url: str, *, timeout: int = 15, max_bytes: int = _MAX_REGISTRY_BYTES) -> bytes:
    """Read a registry payload without silently truncating binary metadata."""
    request = urllib.request.Request(url, headers={"User-Agent": "homelab-toolkit"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = response.read(max_bytes + 1)
    except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
        raise SystemUpdateCheckError(f"dependency registry request failed for {url}: {exc}") from exc
    if len(payload) > max_bytes:
        raise SystemUpdateCheckError(f"dependency registry payload exceeds {max_bytes} bytes for {url}")
    return payload


def _fetch_latest_github_release(repository: str) -> str:
    payload = _read_json(f"https://api.github.com/repos/{repository}/releases/latest")
    raw = payload.get("tag_name") or payload.get("name")
    if not isinstance(raw, str) or not raw:
        raise SystemUpdateCheckError(f"GitHub returned no latest release for {repository}")
    return raw.removeprefix("v")


def _fetch_peeled_release_commit(repository: str) -> tuple[str, str]:
    """Return the stable release tag and the commit it ultimately points to."""
    try:
        payload = _read_json(f"https://api.github.com/repos/{repository}/releases/latest")
        raw_tag = payload.get("tag_name")
    except SystemUpdateCheckError as exc:
        # Some Go modules publish stable tags without creating GitHub Release objects.
        # The tags endpoint is still an official GitHub source; only use it for a
        # confirmed 404 so outages and malformed release responses remain fail-closed.
        if "HTTP Error 404" not in str(exc):
            raise
        tags = _read_json_list(f"https://api.github.com/repos/{repository}/tags?per_page=100")
        candidates: list[str] = [
            item["name"]
            for item in tags
            if isinstance(item, dict)
            and isinstance(item.get("name"), str)
            and re.fullmatch(r"v?[0-9]+(?:\.[0-9]+){1,3}", item["name"])
        ]
        if not candidates:
            raise SystemUpdateCheckError(f"GitHub returned no stable release tags for {repository}") from exc
        raw_tag = max(candidates, key=lambda value: _version_key(value.removeprefix("v")))
    if not isinstance(raw_tag, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/+\-]*", raw_tag):
        raise SystemUpdateCheckError(f"GitHub returned no valid stable release tag for {repository}")
    tag = urllib.parse.quote(raw_tag, safe="")
    ref = _read_json(f"https://api.github.com/repos/{repository}/git/ref/tags/{tag}")
    obj = ref.get("object")
    if not isinstance(obj, dict) or not isinstance(obj.get("sha"), str):
        raise SystemUpdateCheckError(f"GitHub returned an invalid release ref for {repository}")
    sha = obj["sha"].lower()
    for _ in range(3):
        kind = obj.get("type")
        if kind == "commit":
            if not re.fullmatch(r"[0-9a-f]{40}", sha):
                raise SystemUpdateCheckError(f"GitHub returned an invalid release commit for {repository}")
            return raw_tag.removeprefix("v"), sha
        if kind != "tag":
            raise SystemUpdateCheckError(f"GitHub returned an unpeelable release tag for {repository}")
        tag_payload = _read_json(f"https://api.github.com/repos/{repository}/git/tags/{sha}")
        obj = tag_payload.get("object")
        if not isinstance(obj, dict) or not isinstance(obj.get("sha"), str):
            raise SystemUpdateCheckError(f"GitHub returned an invalid annotated tag for {repository}")
        sha = obj["sha"].lower()
    raise SystemUpdateCheckError(f"GitHub release tag is nested too deeply for {repository}")


def _fetch_latest_galaxy_version(namespace: str, name: str) -> str:
    payload = _read_json(
        "https://galaxy.ansible.com/api/v3/plugin/ansible/content/published/collections/"
        f"index/{namespace}/{name}/versions/?limit=20&offset=0"
    )
    versions = payload.get("data")
    if not isinstance(versions, list):
        raise SystemUpdateCheckError(f"Ansible Galaxy returned no versions for {namespace}.{name}")
    stable = [
        value
        for item in versions
        if isinstance(item, dict)
        and isinstance((value := item.get("version")), str)
        and re.fullmatch(r"[0-9]+(?:\.[0-9]+)+", value)
    ]
    if not stable:
        raise SystemUpdateCheckError(f"Ansible Galaxy returned no stable version for {namespace}.{name}")
    return max(stable, key=lambda value: tuple(int(part) for part in value.split(".")))


def _parse_uv_lock_changes(output: str) -> list[dict]:
    updates: list[dict] = []
    for line in output.splitlines():
        match = _UV_UPDATE.fullmatch(line.strip())
        if match is None:
            continue
        updates.append(
            {
                "name": match.group("name"),
                "current": match.group("current"),
                "latest": match.group("latest"),
                "source": "python-lock",
            }
        )
    return updates


def check_python_lock(root: Path) -> list[dict]:
    """Resolve the newest versions allowed by project constraints without writing."""
    command = ["uv", "lock", "--project", str(root), "--upgrade", "--dry-run"]
    returncode, output = _run(command, timeout=180)
    if returncode != 0:
        raise SystemUpdateCheckError(f"uv lock update check failed: {output.strip()[:300]}")
    return _parse_uv_lock_changes(output)


def check_tool_pins(root: Path) -> list[dict]:
    """Compare checksummed toolkit binary pins with official stable releases."""
    dockerfile = root / "toolkit" / "Dockerfile"
    try:
        content = dockerfile.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemUpdateCheckError(f"cannot read toolkit Dockerfile: {exc}") from exc
    updates: list[dict] = []
    for argument, (name, repository) in _TOOL_RELEASES.items():
        match = re.search(rf"^ARG {argument}=(\S+)$", content, re.MULTILINE)
        if match is None:
            raise SystemUpdateCheckError(f"toolkit Dockerfile is missing {argument}")
        current = match.group(1)
        latest = _fetch_latest_github_release(repository)
        if current != latest:
            updates.append({"name": name, "current": current, "latest": latest, "source": "toolkit-binary"})
    return updates


def check_ansible_collections(root: Path) -> list[dict]:
    """Compare pinned Galaxy collections with their latest stable releases."""
    requirements = root / "automation" / "ansible" / "requirements.yml"
    try:
        document = yaml.safe_load(requirements.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SystemUpdateCheckError(f"cannot read Ansible collection requirements: {exc}") from exc
    collections = document.get("collections") if isinstance(document, dict) else None
    if not isinstance(collections, list):
        raise SystemUpdateCheckError("Ansible collection requirements are invalid")
    updates: list[dict] = []
    for item in collections:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("version"), str)
        ):
            raise SystemUpdateCheckError("Ansible collection requirement is invalid")
        collection = item["name"]
        parts = collection.split(".")
        if len(parts) != 2 or not all(re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
            raise SystemUpdateCheckError(f"Ansible collection name is invalid: {collection}")
        current = item["version"]
        latest = _fetch_latest_galaxy_version(parts[0], parts[1])
        if current != latest:
            updates.append(
                {
                    "name": collection,
                    "current": current,
                    "latest": latest,
                    "source": "ansible-collection",
                }
            )
    return updates


def _role_defaults(root: Path, role: str) -> dict:
    path = root / "automation" / "ansible" / "roles" / role / "defaults" / "main.yml"
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise SystemUpdateCheckError(f"cannot read {role} role defaults: {exc}") from exc
    if not isinstance(document, dict):
        raise SystemUpdateCheckError(f"{role} role defaults are invalid")
    return document


def _validate_release_map(role: str, releases: object, *, asset_key: str) -> dict[str, dict[str, str]]:
    if not isinstance(releases, dict) or set(releases) != {"x86_64", "aarch64"}:
        raise SystemUpdateCheckError(f"{role} release map must cover x86_64 and aarch64")
    validated: dict[str, dict[str, str]] = {}
    for architecture, release in releases.items():
        if not isinstance(release, dict):
            raise SystemUpdateCheckError(f"{role} release for {architecture} is invalid")
        asset = release.get(asset_key)
        digest = release.get("sha256")
        if (
            not isinstance(asset, str)
            or not re.fullmatch(r"[A-Za-z0-9_-]+", asset)
            or not isinstance(digest, str)
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise SystemUpdateCheckError(f"{role} release for {architecture} has an invalid asset or checksum")
        validated[str(architecture)] = {asset_key: asset, "sha256": digest}
    return validated


def _version_update(name: str, current: str, latest: str) -> dict | None:
    if current == latest:
        return None
    return {"name": name, "current": current, "latest": latest, "source": "host-binary"}


def check_host_binary_pins(root: Path) -> list[dict]:
    """Check latest host-agent versions and verify every current release digest."""
    report: list[dict] = []

    tailscale = _role_defaults(root, "vpn_client")
    tailscale_version = tailscale.get("tailscale_version")
    if not isinstance(tailscale_version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", tailscale_version):
        raise SystemUpdateCheckError("vpn_client has an invalid Tailscale version")
    tailscale_releases = _validate_release_map(
        "vpn_client", tailscale.get("tailscale_releases"), asset_key="asset_arch"
    )
    tailscale_latest_payload = _read_json("https://pkgs.tailscale.com/stable/?mode=json")
    tailscale_latest = tailscale_latest_payload.get("TarballsVersion")
    if not isinstance(tailscale_latest, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", tailscale_latest):
        raise SystemUpdateCheckError("Tailscale package registry returned no stable tarball version")
    if update := _version_update("tailscale", tailscale_version, tailscale_latest):
        report.append(update)
    for architecture, release in tailscale_releases.items():
        asset_arch = release["asset_arch"]
        actual = _read_text(
            f"https://pkgs.tailscale.com/stable/tailscale_{tailscale_version}_{asset_arch}.tgz.sha256"
        ).split()[0]
        if not re.fullmatch(r"[0-9a-f]{64}", actual):
            raise SystemUpdateCheckError(f"Tailscale returned an invalid {asset_arch} checksum")
        if actual != release["sha256"]:
            report.append(
                {
                    "name": f"tailscale-{asset_arch}-integrity",
                    "current": release["sha256"],
                    "latest": actual,
                    "source": "host-binary-integrity",
                }
            )

    komodo = _role_defaults(root, "komodo_periphery")
    komodo_version = komodo.get("komodo_periphery_version")
    if not isinstance(komodo_version, str) or not re.fullmatch(r"[0-9]+(?:\.[0-9]+){2}", komodo_version):
        raise SystemUpdateCheckError("komodo_periphery has an invalid version")
    komodo_releases = _validate_release_map(
        "komodo_periphery", komodo.get("komodo_periphery_releases"), asset_key="asset"
    )
    komodo_latest_payload = _read_json("https://api.github.com/repos/moghtech/komodo/releases/latest")
    raw_latest = komodo_latest_payload.get("tag_name")
    if not isinstance(raw_latest, str) or not re.fullmatch(r"v[0-9]+(?:\.[0-9]+){2}", raw_latest):
        raise SystemUpdateCheckError("GitHub returned no stable Komodo release")
    komodo_latest = raw_latest.removeprefix("v")
    if update := _version_update("komodo-periphery", komodo_version, komodo_latest):
        report.append(update)

    pinned_payload = _read_json(f"https://api.github.com/repos/moghtech/komodo/releases/tags/v{komodo_version}")
    assets = pinned_payload.get("assets")
    if not isinstance(assets, list):
        raise SystemUpdateCheckError("GitHub returned no Komodo release assets")
    digests = {
        str(asset.get("name")): str(asset.get("digest", "")).removeprefix("sha256:")
        for asset in assets
        if isinstance(asset, dict)
    }
    for architecture, release in komodo_releases.items():
        asset = release["asset"]
        actual = digests.get(asset, "")
        if not re.fullmatch(r"[0-9a-f]{64}", actual):
            raise SystemUpdateCheckError(f"GitHub returned no valid digest for Komodo asset {asset}")
        if actual != release["sha256"]:
            report.append(
                {
                    "name": f"komodo-periphery-{architecture}-integrity",
                    "current": release["sha256"],
                    "latest": actual,
                    "source": "host-binary-integrity",
                }
            )
    return report


def _version_key(value: str) -> tuple[int, ...]:
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+){0,3})(?:[-+].*)?", value)
    if match is None:
        raise SystemUpdateCheckError(f"registry returned an invalid package version: {value}")
    return tuple(int(part) for part in match.group(1).split("."))


def _latest_crowdsec_package(payload: bytes) -> str:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(payload)) as archive:
            decompressed = archive.read(_MAX_PACKAGE_INDEX_BYTES + 1)
        if len(decompressed) > _MAX_PACKAGE_INDEX_BYTES:
            raise SystemUpdateCheckError("Packagecloud CrowdSec index exceeds the decompression limit")
        text = decompressed.decode("utf-8")
    except (OSError, EOFError, UnicodeError, zlib.error) as exc:
        raise SystemUpdateCheckError(f"Packagecloud returned invalid CrowdSec Packages.gz: {exc}") from exc
    versions: list[str] = []
    for stanza in re.split(r"\n\s*\n", text):
        fields = dict(re.findall(r"^(Package|Version):[ \t]*(.+)$", stanza, re.MULTILINE))
        if fields.get("Package") == "crowdsec" and re.fullmatch(
            r"[0-9]+(?:\.[0-9]+){2}(?:-[A-Za-z0-9.+~-]+)?", fields.get("Version", "")
        ):
            versions.append(fields["Version"])
    if not versions:
        raise SystemUpdateCheckError("Packagecloud returned no stable CrowdSec package for Bookworm")
    return max(versions, key=_version_key)


def check_crowdsec_agent(root: Path) -> list[dict]:
    """Check the Bookworm package index and the pinned repository key digest."""
    defaults = _role_defaults(root, "crowdsec_agent")
    current = defaults.get("crowdsec_package_version")
    key_url = defaults.get("crowdsec_package_key_url")
    key_digest = defaults.get("crowdsec_package_key_sha256")
    if not isinstance(current, str) or _version_key(current) is None:
        raise SystemUpdateCheckError("crowdsec_agent has an invalid package version")
    if (
        key_url != "https://packagecloud.io/crowdsec/crowdsec/gpgkey"
        or not isinstance(key_digest, str)
        or not re.fullmatch(r"[0-9a-f]{64}", key_digest)
    ):
        raise SystemUpdateCheckError("crowdsec_agent has an invalid Packagecloud signing-key pin")
    package_url = "https://packagecloud.io/crowdsec/crowdsec/debian/dists/bookworm/main/binary-amd64/Packages.gz"
    latest = _latest_crowdsec_package(_read_bytes(package_url, timeout=30))
    report: list[dict] = []
    if current != latest:
        report.append({"name": "crowdsec-agent", "current": current, "latest": latest, "source": "crowdsec-package"})
    actual_digest = hashlib.sha256(_read_bytes(key_url)).hexdigest()
    if actual_digest != key_digest:
        report.append(
            {
                "name": "crowdsec-package-signing-key",
                "current": key_digest,
                "latest": actual_digest,
                "source": "crowdsec-integrity",
            }
        )
    return report


_CADDY_MODULES = {
    "caddy-dns-cloudflare": ("caddy-dns/cloudflare", "github.com/caddy-dns/cloudflare"),
    "caddy-crowdsec-bouncer": ("hslatman/caddy-crowdsec-bouncer", "github.com/hslatman/caddy-crowdsec-bouncer/http"),
}


def check_caddy_module_pins(root: Path) -> list[dict]:
    dockerfile = root / "toolkit" / "services" / "caddy" / "image" / "Dockerfile"
    try:
        content = dockerfile.read_text(encoding="utf-8")
    except OSError as exc:
        raise SystemUpdateCheckError(f"cannot read Caddy image Dockerfile: {exc}") from exc
    report: list[dict] = []
    for name, (repository, module) in _CADDY_MODULES.items():
        match = re.search(rf"--with {re.escape(module)}@([0-9a-f]{{40}})\b", content)
        if match is None:
            raise SystemUpdateCheckError(f"Caddy Dockerfile is missing immutable pin for {module}")
        current = match.group(1)
        tag, latest = _fetch_peeled_release_commit(repository)
        if current != latest:
            report.append(
                {"name": name, "current": current, "latest": latest, "release": tag, "source": "caddy-module"}
            )
    return report


def _parse_cloudflare_cidrs(body: str, family: int, url: str) -> tuple[str, ...]:
    values = tuple(line.strip() for line in body.splitlines() if line.strip())
    if not values or len(values) != len(set(values)):
        raise SystemUpdateCheckError(f"Cloudflare returned invalid proxy ranges from {url}")
    try:
        networks = tuple(ipaddress.ip_network(value, strict=True) for value in values)
    except ValueError as exc:
        raise SystemUpdateCheckError(f"Cloudflare returned invalid proxy ranges from {url}: {exc}") from exc
    if any(network.version != family for network in networks):
        raise SystemUpdateCheckError(f"Cloudflare returned mixed address families from {url}")
    return tuple(str(network) for network in networks)


def check_cloudflare_proxy_cidrs(root: Path) -> list[dict]:
    """Compare Caddy's trust allow-list with Cloudflare's authoritative ranges."""
    del root
    from toolkit.services.caddy.plugin import CLOUDFLARE_PROXY_CIDRS

    configured = {
        4: tuple(value for value in CLOUDFLARE_PROXY_CIDRS if ":" not in value),
        6: tuple(value for value in CLOUDFLARE_PROXY_CIDRS if ":" in value),
    }
    report: list[dict] = []
    for family, url in ((4, "https://www.cloudflare.com/ips-v4"), (6, "https://www.cloudflare.com/ips-v6")):
        latest = _parse_cloudflare_cidrs(_read_text(url), family, url)
        current = tuple(sorted(configured[family]))
        if current != tuple(sorted(latest)):
            report.append(
                {
                    "name": f"cloudflare-proxy-cidrs-ipv{family}",
                    "current": list(current),
                    "latest": list(latest),
                    "source": "cloudflare-proxy-cidrs",
                }
            )
    return report


def check_all(root: Path = REPO_ROOT) -> list[dict]:
    report: list[dict] = []
    report.extend(check_python_lock(root))
    report.extend(check_tool_pins(root))
    report.extend(check_ansible_collections(root))
    report.extend(check_host_binary_pins(root))
    report.extend(check_crowdsec_agent(root))
    report.extend(check_caddy_module_pins(root))
    report.extend(check_cloudflare_proxy_cidrs(root))
    return report


def print_report(report: list[dict]) -> None:
    if not report:
        print("Framework dependencies are current.")
        return
    labels = {
        "python-lock": "Python Lock",
        "ansible-collection": "Ansible Collections",
        "toolkit-binary": "Toolkit Binaries",
        "host-binary": "Host Binaries",
        "host-binary-integrity": "Host Binary Integrity",
        "crowdsec-package": "CrowdSec Package",
        "crowdsec-integrity": "CrowdSec Integrity",
        "caddy-module": "Caddy Modules",
        "cloudflare-proxy-cidrs": "Cloudflare Proxy CIDRs",
    }
    for source in (
        "python-lock",
        "ansible-collection",
        "toolkit-binary",
        "host-binary",
        "host-binary-integrity",
        "crowdsec-package",
        "crowdsec-integrity",
        "caddy-module",
        "cloudflare-proxy-cidrs",
    ):
        items = [item for item in report if item["source"] == source]
        if not items:
            continue
        print(f"\n{labels[source]:━^72}")
        print(f"{'Name':<36} {'Current':<16} {'Latest':<16}")
        print("─" * 72)
        for item in sorted(items, key=lambda value: value["name"]):
            print(f"{item['name']:<36} {item['current']:<16} {item['latest']:<16}")
    print(f"\nTotal: {len(report)} framework dependency update(s)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check source-controlled framework dependencies")
    parser.add_argument("--json", action="store_true", help="Output JSON report to stdout")
    parser.add_argument("--cache", action="store_true", help="Use cached results if fresh")
    parser.add_argument("--refresh", action="store_true", help="Force refresh cache")
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="Framework root")
    args = parser.parse_args(argv)

    if args.cache and not args.refresh:
        cached = _read_cache()
        if cached is not None:
            if args.json:
                json.dump(cached["updates"], sys.stdout, indent=2)
                print()
            else:
                print(f"Using cached results from {cached.get('cached_at_iso', 'unknown')}")
                print_report(cached["updates"])
            return 0

    try:
        report = check_all(args.root.resolve())
    except SystemUpdateCheckError as exc:
        print(f"Framework update check failed: {exc}", file=sys.stderr)
        return 1
    _write_cache(report)
    if args.json:
        json.dump(report, sys.stdout, indent=2, default=str)
        print()
    else:
        print_report(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
