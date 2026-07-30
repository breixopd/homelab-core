from __future__ import annotations

import importlib.util
import json
import sys
import urllib.error
from email.message import Message
from pathlib import Path
from unittest.mock import patch


def _load_script():
    path = Path(__file__).resolve().parents[3] / "scripts" / "bump-versions.py"
    spec = importlib.util.spec_from_file_location("homelab_bump_versions", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


class _Response:
    def __init__(self, payload: dict):
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode()


def test_parse_image_ref_separates_tag_and_digest() -> None:
    module = _load_script()

    assert module._parse_image_ref("ghcr.io/acme/app:v1.2.3@sha256:" + "a" * 64) == (
        "ghcr.io",
        "acme/app",
        "v1.2.3",
        "ghcr.io/acme/app:v1.2.3@sha256:" + "a" * 64,
    )
    assert module._parse_image_ref("registry.example:5000/acme/app:2.0")[:3] == (
        "registry.example:5000",
        "acme/app",
        "2.0",
    )


def test_iter_service_images_uses_yaml_model(tmp_path: Path) -> None:
    module = _load_script()
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        "services:\n  app:\n    labels: [managed]\n    image: ghcr.io/acme/app:v1.2.3\n  built:\n    build: .\n",
        encoding="utf-8",
    )

    assert module._iter_service_images(compose) == [{"service": "app", "image": "ghcr.io/acme/app:v1.2.3"}]


def test_display_path_handles_compose_files_outside_repository(tmp_path: Path) -> None:
    module = _load_script()
    compose = tmp_path / "compose.yaml"

    assert module._display_path(compose) == str(compose)


def test_fetch_tags_follows_bearer_challenge() -> None:
    module = _load_script()
    headers = Message()
    headers["WWW-Authenticate"] = (
        'Bearer realm="https://ghcr.io/token",service="ghcr.io",scope="repository:acme/app:pull"'
    )
    unauthorized = urllib.error.HTTPError(
        "https://ghcr.io/v2/acme/app/tags/list",
        401,
        "Unauthorized",
        headers,
        None,
    )

    with patch.object(
        module.urllib.request,
        "urlopen",
        side_effect=[
            unauthorized,
            _Response({"token": "registry-token"}),
            _Response({"tags": ["v1.0.0", "v1.1.0"]}),
        ],
    ) as urlopen:
        result = module._fetch_tags("ghcr.io", "acme/app")

    assert result.tags == ("v1.0.0", "v1.1.0")
    assert result.error is None
    retry_request = urlopen.call_args_list[2].args[0]
    assert retry_request.get_header("Authorization") == "Bearer registry-token"


def test_registry_requests_retry_transient_rate_limits() -> None:
    module = _load_script()
    headers = Message()
    headers["Retry-After"] = "0"
    rate_limited = urllib.error.HTTPError("https://registry.example/tags", 429, "rate limited", headers, None)
    request = urllib.request.Request("https://registry.example/tags")

    with (
        patch.object(module.urllib.request, "urlopen", side_effect=[rate_limited, _Response({"tags": ["1.0"]})]),
        patch.object(module.time, "sleep") as sleep,
    ):
        assert module._read_json(request, 5) == {"tags": ["1.0"]}

    sleep.assert_called_once()


def test_check_images_surfaces_registry_failure(tmp_path: Path) -> None:
    module = _load_script()
    compose = tmp_path / "compose.yaml"
    compose.write_text("services:\n  app:\n    image: ghcr.io/acme/app:v1.0.0\n", encoding="utf-8")

    with patch.object(module, "_fetch_tags", return_value=module.RegistryTags(error="HTTP 503")):
        report = module.check_images(compose)

    assert report[0]["checked"] is False
    assert report[0]["error"] == "HTTP 503"
    assert report[0]["needs_update"] is False


def test_check_images_skips_project_built_image_variables(tmp_path: Path) -> None:
    module = _load_script()
    compose = tmp_path / "compose.yaml"
    compose.write_text(
        "services:\n  app:\n    image: ${HOMELAB_UI_IMAGE:-ghcr.io/acme/ui:latest}\n",
        encoding="utf-8",
    )

    assert module.check_images(compose) == []


def test_version_scanner_has_no_service_specific_skip_list() -> None:
    source = (Path(__file__).resolve().parents[3] / "scripts" / "bump-versions.py").read_text(encoding="utf-8")

    assert "LOCAL_IMAGES" not in source
