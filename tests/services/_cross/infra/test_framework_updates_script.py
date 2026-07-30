from __future__ import annotations

import gzip
import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest


def _load_script():
    path = Path(__file__).resolve().parents[4] / "scripts" / "check-framework-updates.py"
    spec = importlib.util.spec_from_file_location("homelab_framework_updates", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_parse_uv_lock_changes_reports_only_resolver_compatible_updates() -> None:
    module = _load_script()

    assert module._parse_uv_lock_changes("Resolved 20 packages\nUpdate click v8.3.1 -> v8.4.2\n") == [
        {"name": "click", "current": "8.3.1", "latest": "8.4.2", "source": "python-lock"}
    ]


def test_check_python_lock_uses_upgrade_dry_run(tmp_path: Path) -> None:
    module = _load_script()
    with patch.object(module, "_run", return_value=(0, "Update pydantic v2.12.0 -> v2.13.4")) as run:
        updates = module.check_python_lock(tmp_path)

    assert updates[0]["name"] == "pydantic"
    assert run.call_args.args[0] == ["uv", "lock", "--project", str(tmp_path), "--upgrade", "--dry-run"]


def test_empty_system_report_replaces_stale_cache(tmp_path: Path) -> None:
    module = _load_script()
    cache = tmp_path / "framework-updates-cache.json"
    cache.write_text('{"updates":[{"name":"stale"}]}', encoding="utf-8")

    with patch.object(module, "CACHE_FILE", cache):
        module._write_cache([])

    payload = json.loads(cache.read_text(encoding="utf-8"))
    assert payload["updates"] == []
    assert payload["cached_at_iso"].endswith("+00:00")


def test_check_all_uses_project_sources_instead_of_runtime_packages(tmp_path: Path) -> None:
    module = _load_script()
    with (
        patch.object(module, "check_python_lock", return_value=[{"name": "python"}]) as python,
        patch.object(module, "check_tool_pins", return_value=[{"name": "tool"}]) as tools,
        patch.object(module, "check_ansible_collections", return_value=[{"name": "ansible"}]) as ansible,
        patch.object(module, "check_host_binary_pins", return_value=[{"name": "host"}]) as host,
        patch.object(module, "check_crowdsec_agent", return_value=[{"name": "crowdsec"}]) as crowdsec,
        patch.object(module, "check_caddy_module_pins", return_value=[{"name": "caddy"}]) as caddy,
        patch.object(module, "check_cloudflare_proxy_cidrs", return_value=[{"name": "cloudflare"}]) as cloudflare,
    ):
        assert module.check_all(tmp_path) == [
            {"name": "python"},
            {"name": "tool"},
            {"name": "ansible"},
            {"name": "host"},
            {"name": "crowdsec"},
            {"name": "caddy"},
            {"name": "cloudflare"},
        ]

    python.assert_called_once_with(tmp_path)
    tools.assert_called_once_with(tmp_path)
    ansible.assert_called_once_with(tmp_path)
    host.assert_called_once_with(tmp_path)
    crowdsec.assert_called_once_with(tmp_path)
    caddy.assert_called_once_with(tmp_path)
    cloudflare.assert_called_once_with(tmp_path)


def test_host_binary_check_reports_version_updates_and_checksum_drift(tmp_path: Path) -> None:
    module = _load_script()
    tailscale_defaults = tmp_path / "automation/ansible/roles/vpn_client/defaults/main.yml"
    komodo_defaults = tmp_path / "automation/ansible/roles/komodo_periphery/defaults/main.yml"
    tailscale_defaults.parent.mkdir(parents=True)
    komodo_defaults.parent.mkdir(parents=True)
    tailscale_defaults.write_text(
        "tailscale_version: '1.2.3'\n"
        "tailscale_releases:\n"
        "  x86_64: {asset_arch: amd64, sha256: '" + "a" * 64 + "'}\n"
        "  aarch64: {asset_arch: arm64, sha256: '" + "b" * 64 + "'}\n",
        encoding="utf-8",
    )
    komodo_defaults.write_text(
        "komodo_periphery_version: '2.0.0'\n"
        "komodo_periphery_releases:\n"
        "  x86_64: {asset: periphery-x86_64, sha256: '" + "c" * 64 + "'}\n"
        "  aarch64: {asset: periphery-aarch64, sha256: '" + "d" * 64 + "'}\n",
        encoding="utf-8",
    )

    def read_json(url: str):
        if "pkgs.tailscale.com" in url:
            return {"TarballsVersion": "1.2.4"}
        if url.endswith("/releases/latest"):
            return {"tag_name": "v2.1.0"}
        return {
            "assets": [
                {"name": "periphery-x86_64", "digest": "sha256:" + "c" * 64},
                {"name": "periphery-aarch64", "digest": "sha256:" + "e" * 64},
            ]
        }

    def read_text(url: str):
        return "a" * 64 if "amd64" in url else "f" * 64

    with (
        patch.object(module, "_read_json", side_effect=read_json),
        patch.object(module, "_read_text", side_effect=read_text),
    ):
        report = module.check_host_binary_pins(tmp_path)

    by_name = {item["name"]: item for item in report}
    assert by_name["tailscale"]["latest"] == "1.2.4"
    assert by_name["komodo-periphery"]["latest"] == "2.1.0"
    assert by_name["tailscale-arm64-integrity"]["source"] == "host-binary-integrity"
    assert by_name["komodo-periphery-aarch64-integrity"]["latest"] == "e" * 64


def test_crowdsec_check_uses_bookworm_index_and_key_digest(tmp_path: Path) -> None:
    module = _load_script()
    defaults = tmp_path / "automation/ansible/roles/crowdsec_agent/defaults/main.yml"
    defaults.parent.mkdir(parents=True)
    defaults.write_text(
        "crowdsec_package_version: '1.7.8'\n"
        "crowdsec_package_key_url: 'https://packagecloud.io/crowdsec/crowdsec/gpgkey'\n"
        "crowdsec_package_key_sha256: '" + "a" * 64 + "'\n",
        encoding="utf-8",
    )
    package = gzip.compress(b"Package: crowdsec\nVersion: 1.7.9\n\nPackage: other\nVersion: 99.0.0\n")
    key = b"trusted key bytes"
    with patch.object(module, "_read_bytes", side_effect=[package, key]):
        report = module.check_crowdsec_agent(tmp_path)
    assert report[0]["source"] == "crowdsec-package"
    assert report[0]["latest"] == "1.7.9"
    assert report[1]["source"] == "crowdsec-integrity"


def test_crowdsec_package_decompression_is_bounded(monkeypatch) -> None:
    module = _load_script()
    monkeypatch.setattr(module, "_MAX_PACKAGE_INDEX_BYTES", 16)

    with pytest.raises(module.SystemUpdateCheckError, match="decompression limit"):
        module._latest_crowdsec_package(gzip.compress(b"x" * 17))


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, size: int = -1) -> bytes:
        return self.payload if size < 0 else self.payload[:size]


@pytest.mark.parametrize("reader", ["_read_json", "_read_json_list"])
def test_registry_json_reads_are_bounded(monkeypatch, reader: str) -> None:
    module = _load_script()
    payload = b"{" + b'"x":"' + b"a" * module._MAX_REGISTRY_BYTES + b'"}'
    monkeypatch.setattr(module.urllib.request, "urlopen", lambda *_args, **_kwargs: _Response(payload))

    with pytest.raises(module.SystemUpdateCheckError, match="payload exceeds"):
        getattr(module, reader)("https://registry.example/update")


def test_crowdsec_package_rejects_malformed_gzip() -> None:
    module = _load_script()
    with pytest.raises(module.SystemUpdateCheckError, match="invalid CrowdSec Packages.gz"):
        module._latest_crowdsec_package(b"not gzip")


def test_caddy_modules_compare_peeled_release_commits(tmp_path: Path) -> None:
    module = _load_script()
    dockerfile = tmp_path / "toolkit/services/caddy/image/Dockerfile"
    dockerfile.parent.mkdir(parents=True)
    dockerfile.write_text(
        "RUN xcaddy build --with github.com/caddy-dns/cloudflare@"
        + "a" * 40
        + " --with github.com/hslatman/caddy-crowdsec-bouncer/http@"
        + "b" * 40,
        encoding="utf-8",
    )
    with patch.object(module, "_fetch_peeled_release_commit", side_effect=[("v1.0.0", "c" * 40), ("v2.0.0", "b" * 40)]):
        report = module.check_caddy_module_pins(tmp_path)
    assert report == [
        {
            "name": "caddy-dns-cloudflare",
            "current": "a" * 40,
            "latest": "c" * 40,
            "release": "v1.0.0",
            "source": "caddy-module",
        }
    ]


def test_cloudflare_cidr_check_fails_closed_on_mixed_family() -> None:
    module = _load_script()
    with patch.object(module, "_read_text", return_value="10.0.0.0/8\n2001:db8::/32\n"):
        with pytest.raises(module.SystemUpdateCheckError, match="mixed address families"):
            module.check_cloudflare_proxy_cidrs(Path("."))
