from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest
from toolkit.services.caddy.artifacts import (
    _caddyfile_required_modules,
    _resolve_caddy_validate_image,
    format_generated_caddyfile,
    validate_generated_caddyfile,
)

ROOT = Path(__file__).resolve().parents[3]


def test_validation_image_can_build_with_legacy_docker_builder() -> None:
    dockerfile = (ROOT / "toolkit" / "services" / "caddy" / "image" / "Dockerfile").read_text()

    assert "--platform=$BUILDPLATFORM" not in dockerfile


def _fake_which(name: str) -> str | None:
    return "/usr/bin/caddy" if name == "caddy" else None


def test_validate_generated_caddyfile_uses_caddy_binary(tmp_path):
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "Caddyfile").write_text("example.com {\n\trespond ok\n}\n")

    with (
        patch("toolkit.services.caddy.artifacts.shutil.which", side_effect=_fake_which),
        patch("toolkit.services.caddy.artifacts.subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr="")
        validate_generated_caddyfile(generated)
        mock_run.assert_called_once()
        assert mock_run.call_args.args[0][0] == "/usr/bin/caddy"


def test_validate_generated_caddyfile_raises_on_failure(tmp_path):
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "Caddyfile").write_text("bad syntax\n")

    with (
        patch("toolkit.services.caddy.artifacts.shutil.which", side_effect=_fake_which),
        patch("toolkit.services.caddy.artifacts.subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="Error: invalid Caddyfile"
        )
        with pytest.raises(ValueError, match="Caddyfile validation failed"):
            validate_generated_caddyfile(generated)


def test_format_generated_caddyfile_uses_runtime_image_and_never_host_binary(tmp_path: Path) -> None:
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "Caddyfile").write_text("example.com { respond ok }\n", encoding="utf-8")

    with (
        patch("toolkit.services.caddy.artifacts.shutil.which", return_value="/usr/bin/docker"),
        patch(
            "toolkit.services.caddy.artifacts._resolve_caddy_validate_image", return_value="custom-caddy:test"
        ) as resolve,
        patch("toolkit.services.caddy.artifacts.subprocess.run") as run,
    ):
        run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="example.com {\n\trespond ok\n}\n", stderr=""
        )
        format_generated_caddyfile(generated, repo_root=tmp_path)

    resolve.assert_called_once_with(generated / "Caddyfile", tmp_path)
    assert run.call_count == 2
    assert run.call_args_list[0].args[0] == ["docker", "info"]
    command = run.call_args_list[1].args[0]
    assert command[:3] == ["docker", "run", "--rm"]
    assert "-i" in command
    assert "-v" not in command
    assert "custom-caddy:test" in command
    assert command[-3:] == ["caddy", "fmt", "-"]
    assert run.call_args_list[1].kwargs["input"] == "example.com { respond ok }\n"
    assert (generated / "Caddyfile").read_text() == "example.com {\n\trespond ok\n}\n"


def test_format_generated_caddyfile_defers_without_docker(tmp_path: Path, caplog) -> None:
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "Caddyfile").write_text("example.com {\n\trespond ok\n}\n", encoding="utf-8")

    with patch("toolkit.services.caddy.artifacts.shutil.which", return_value=None):
        format_generated_caddyfile(generated)

    assert "formatting deferred" in caplog.text.lower()


def test_format_generated_caddyfile_defers_when_image_pull_is_disabled(tmp_path: Path, caplog) -> None:
    generated = tmp_path / "generated"
    generated.mkdir()
    caddyfile = generated / "Caddyfile"
    original = "example.com { respond ok }\n"
    caddyfile.write_text(original, encoding="utf-8")

    with (
        patch("toolkit.services.caddy.artifacts.shutil.which", return_value="/usr/bin/docker"),
        patch(
            "toolkit.services.caddy.artifacts.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ) as run,
        patch("toolkit.services.caddy.artifacts._resolve_caddy_validate_image", return_value=""),
    ):
        format_generated_caddyfile(generated)

    assert run.call_count == 1
    assert caddyfile.read_text(encoding="utf-8") == original
    assert "pulls are disabled" in caplog.text.lower()


def test_validate_generated_caddyfile_defers_when_docker_daemon_is_unavailable(tmp_path, caplog):
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "Caddyfile").write_text("example.com {\n\trespond ok\n}\n")

    with (
        patch(
            "toolkit.services.caddy.artifacts.shutil.which",
            side_effect=lambda name: "/usr/bin/docker" if name == "docker" else None,
        ),
        patch("toolkit.services.caddy.artifacts.subprocess.run") as mock_run,
    ):
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=1, stdout="", stderr="permission denied connecting to docker.sock"
        )
        validate_generated_caddyfile(generated)

    mock_run.assert_called_once()
    assert mock_run.call_args.args[0] == ["docker", "info"]
    assert "deferred" in caplog.text.lower()


def test_validate_uses_service_image_without_forwarding_real_secrets(tmp_path: Path, monkeypatch) -> None:
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "Caddyfile").write_text("{\n  crowdsec {\n    api_key {env.CADDY_BOUNCER_API_KEY}\n  }\n}\n")
    monkeypatch.setenv("CF_API_TOKEN", "real-cloudflare-secret")
    monkeypatch.setenv("CADDY_BOUNCER_API_KEY", "real-bouncer-secret")

    with (
        patch(
            "toolkit.services.caddy.artifacts.shutil.which",
            side_effect=lambda name: f"/usr/bin/{name}" if name in {"caddy", "docker"} else None,
        ),
        patch("toolkit.services.caddy.artifacts._caddy_binary_supports", return_value=False),
        patch(
            "toolkit.services.caddy.artifacts._resolve_caddy_validate_image", return_value="custom-caddy:test"
        ) as resolve,
        patch(
            "toolkit.services.caddy.artifacts.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ) as run,
    ):
        validate_generated_caddyfile(generated)

    resolve.assert_called_once()
    assert run.call_args.args[0][0:3] == ["docker", "run", "--rm"]
    command = run.call_args.args[0]
    assert "-i" in command
    assert "-v" not in command
    assert command[-4:] == ["--config", "-", "--adapter", "caddyfile"]
    assert run.call_args.kwargs["input"] == (generated / "Caddyfile").read_text()
    assert all("real-cloudflare-secret" not in value for value in command)
    assert all("real-bouncer-secret" not in value for value in command)


def test_caddy_validation_builds_manifest_owned_runtime_image(tmp_path: Path) -> None:
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("{\n  acme_dns cloudflare {env.CF_API_TOKEN}\n}\n", encoding="utf-8")

    with (
        patch("toolkit.services.caddy.artifacts._configured_caddy_image", return_value=""),
        patch("toolkit.services.caddy.artifacts._docker_image_supports", side_effect=[False, False, False, True]),
        patch(
            "toolkit.services.caddy.artifacts.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ) as run,
    ):
        image = _resolve_caddy_validate_image(caddyfile, ROOT)

    assert image == "homelab-caddy-validate:local"
    assert run.call_args.args[0][-1] == str(ROOT / "toolkit/services/caddy/image")


def test_caddy_validation_uses_packaged_image_from_clean_install_root(tmp_path: Path) -> None:
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("{\n  crowdsec {\n  }\n}\n", encoding="utf-8")

    with (
        patch("toolkit.services.caddy.artifacts._configured_caddy_image", return_value=""),
        patch("toolkit.services.caddy.artifacts._docker_image_supports", side_effect=[False, False, False, True]),
        patch(
            "toolkit.services.caddy.artifacts.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="", stderr=""),
        ) as run,
    ):
        image = _resolve_caddy_validate_image(caddyfile, tmp_path)

    assert image == "homelab-caddy-validate:local"
    assert run.call_args.args[0][-1] == str(ROOT / "toolkit/services/caddy/image")


def test_caddy_validation_pulls_configured_runtime_before_building(tmp_path: Path, monkeypatch) -> None:
    # CI keeps unrelated service tests offline, but this test specifically owns
    # and verifies the configured-image pull path.
    monkeypatch.delenv("HOMELAB_CADDY_SKIP_IMAGE_PULL", raising=False)
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("{\n  acme_dns cloudflare {env.CF_API_TOKEN}\n}\n", encoding="utf-8")
    image_ref = "ghcr.io/example/caddy:sha-123"
    monkeypatch.setenv("HOMELAB_CADDY_IMAGE", image_ref)

    with (
        patch("toolkit.services.caddy.artifacts._docker_image_supports", side_effect=[False, False, False, True]),
        patch(
            "toolkit.services.caddy.artifacts.subprocess.run",
            return_value=subprocess.CompletedProcess(args=[], returncode=0, stdout="pulled", stderr=""),
        ) as run,
    ):
        image = _resolve_caddy_validate_image(caddyfile, ROOT)

    assert image == image_ref
    run.assert_called_once()
    assert run.call_args.args[0] == ["docker", "pull", image_ref]


def test_generation_defers_configured_image_pull_when_explicitly_disabled(tmp_path: Path, monkeypatch) -> None:
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text("{\n  acme_dns cloudflare {env.CF_API_TOKEN}\n}\n", encoding="utf-8")
    monkeypatch.setenv("HOMELAB_CADDY_SKIP_IMAGE_PULL", "1")
    monkeypatch.delenv("HOMELAB_CADDY_IMAGE", raising=False)

    with (
        patch(
            "toolkit.services.caddy.artifacts._configured_caddy_image",
            return_value="ghcr.io/example/caddy:configured",
        ),
        patch("toolkit.services.caddy.artifacts._docker_image_supports", return_value=False),
        patch("toolkit.services.caddy.artifacts.subprocess.run") as run,
    ):
        image = _resolve_caddy_validate_image(caddyfile, ROOT)

    assert image == ""
    assert not any(call.args[0][:2] == ["docker", "pull"] for call in run.call_args_list)


def test_caddy_validation_rejects_stale_cached_image_and_requires_security_modules(tmp_path: Path) -> None:
    caddyfile = tmp_path / "Caddyfile"
    caddyfile.write_text(
        "{\n  acme_dns cloudflare {env.CF_API_TOKEN}\n  crowdsec {\n    api_url http://crowdsec:8080\n  }\n}\n",
        encoding="utf-8",
    )

    assert _caddyfile_required_modules(caddyfile) == {
        "dns.providers.cloudflare",
        "http.handlers.crowdsec",
    }


def test_caddyfile_keeps_trusted_proxy_options_on_separate_lines(full_config, tmp_path, seed_oidc_secrets) -> None:
    from toolkit.core.generate.generate import generate_configs

    seed_oidc_secrets(
        full_config,
        tmp_path,
        {
            "CLOUDFLARE_API_TOKEN": "test-cloudflare-token",
            "CROWDSEC_CADDY_BOUNCER_KEY": "test-crowdsec-bouncer-key",
        },
    )
    with patch("toolkit.services.caddy.plugin.validate_generated_caddyfile"):
        generate_configs(full_config, tmp_path)

    content = (tmp_path / "generated" / "Caddyfile").read_text(encoding="utf-8")
    trusted_line = next(line for line in content.splitlines() if "trusted_proxies static" in line)
    assert "trusted_proxies_strict" not in trusted_line
    assert "\n\t\ttrusted_proxies_strict\n" in content


def test_caddyfile_prevents_edge_content_transformation(full_config, tmp_path, seed_oidc_secrets) -> None:
    from toolkit.core.generate.generate import generate_configs

    seed_oidc_secrets(full_config, tmp_path)
    with patch("toolkit.services.caddy.plugin.validate_generated_caddyfile"):
        generate_configs(full_config, tmp_path)

    content = (tmp_path / "generated" / "Caddyfile").read_text(encoding="utf-8")
    security_headers = content[content.index("(security_headers)") : content.index("(compression)")]
    assert '>Cache-Control "no-transform"' in security_headers


def test_caddy_waits_for_crowdsec_when_security_edge_is_enabled() -> None:
    import yaml

    compose = yaml.safe_load((ROOT / "toolkit/services/caddy/compose.yaml").read_text())
    manifest = yaml.safe_load((ROOT / "toolkit/services/caddy/service.yaml").read_text())
    assert compose["services"]["caddy"]["depends_on"]["crowdsec"] == {"condition": "service_healthy"}
    assert "crowdsec" in manifest["depends_on"]


def test_caddyfile_vaultwarden_blocks_admin(full_config, tmp_path, seed_oidc_secrets):
    from toolkit.core.generate.generate import generate_configs

    seed_oidc_secrets(full_config, tmp_path)
    with patch("toolkit.services.caddy.plugin.validate_generated_caddyfile"):
        generate_configs(full_config, tmp_path)
    content = (tmp_path / "generated" / "Caddyfile").read_text()
    assert "path /admin" in content
    assert "path /admin/*" in content
    assert "respond 403" in content
    assert "header >Content-Security-Policy" in content


def test_caddyfile_fmd_keeps_phone_api_native_and_browser_ui_authenticated(full_config, tmp_path, seed_oidc_secrets):
    from toolkit.core.generate.generate import generate_configs

    seed_oidc_secrets(full_config, tmp_path)
    with patch("toolkit.services.caddy.plugin.validate_generated_caddyfile"):
        generate_configs(full_config, tmp_path)

    content = (tmp_path / "generated" / "Caddyfile").read_text(encoding="utf-8")
    start = content.index(f"fmd.{full_config.domain} {{")
    end = content.index("\n}\n", start)
    site = content[start:end]

    assert "path /version /version/" in site
    assert "/api/v1/device" in site
    assert "request_body" in site and "max_size 15MB" in site
    assert site.count("import authelia") == 1
    assert "reverse_proxy 10.10.10.12:8084" in site
    assert "header_up X-Real-IP {remote_host}" in site
