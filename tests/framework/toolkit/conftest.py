from __future__ import annotations

import importlib
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from toolkit.core.capabilities import GpuCapabilities, ServerCapabilities
from toolkit.core.compose.registry import load_all
from toolkit.core.config.config import Config
from toolkit.core.infra.host_capacity import HostCapacity

load_all()

# Safe, deterministic capability snapshot so unit tests never SSH-probe Proxmox/LXCs.
_TEST_CAP = HostCapacity(
    cpu_cores=4,
    mem_total_mb=8192,
    load_1m=1.0,
    wave_timeout_s=180,
    inter_wave_sleep_s=5,
    max_pull_parallel=2,
    load_threshold=8.0,
    source="local",
)
_TEST_GPU = GpuCapabilities(backend="none", source="test")


_PROBE_TEST_MODULES = (
    "test_host_capacity",
    "test_server_capabilities",
    "test_capabilities_compat",
    "test_capabilities_detect",
    "test_capabilities_store",
)
# Modules that legitimately exercise tdarr readiness waiting themselves.
_TDARR_TEST_MODULES = ("test_tdarr_automation", "test_tdarr_plugins")
# Modules that test SOPS encrypt/decrypt round-trips against real tooling.
_SECRETS_CRYPTO_TEST_MODULES = ("test_secrets", "test_bitwarden_crypto", "test_vaultwarden_bootstrap")


@pytest.fixture(autouse=True)
def _no_network_probes(request, tmp_path: Path):
    """Stub host/GPU detection and service-readiness waits for every unit test.
    Capability probing does live SSH to Proxmox + LXCs, and post-start hooks block on
    HTTP readiness loops; without this, `generate`/hook tests hang on network timeouts.

    Skipped for the modules that test those functions directly."""
    mod = request.node.module.__name__.rsplit(".", 1)[-1]
    from contextlib import ExitStack

    controller_token = tmp_path / "controller-local.token"
    controller_token.write_text("unit-test-controller-token-000000000000000000")
    controller_token.chmod(0o600)

    with ExitStack() as stack:
        stack.enter_context(
            patch.dict(
                "os.environ",
                {
                    "HOMELAB_CONTROLLER_ROLE": "local",
                    "HOMELAB_CONTROLLER_TOKEN_FILE": str(controller_token),
                },
            )
        )
        # No unit test should genuinely block; collapse retry/backoff sleeps to no-ops.
        stack.enter_context(patch("time.sleep", lambda *_a, **_k: None))

        def blocked_ssh(*_args, **_kwargs):
            return 255, "", "unit-test transport unavailable"

        for module_name, module in tuple(sys.modules.items()):
            if module_name.startswith("toolkit.") and callable(getattr(module, "ssh_run_on_vm", None)):
                stack.enter_context(patch.object(module, "ssh_run_on_vm", blocked_ssh))
        if mod not in _PROBE_TEST_MODULES:
            stack.enter_context(
                patch(
                    "toolkit.core.infra.host_capacity.detect_host_capacity",
                    return_value=_TEST_CAP,
                )
            )
            stack.enter_context(patch("toolkit.core.infra.host_capacity.detect_lxc_capacity", return_value=None))
            stack.enter_context(
                patch(
                    "toolkit.core.capabilities.detect.detect_host_capacity",
                    return_value=_TEST_CAP,
                )
            )
            stack.enter_context(
                patch(
                    "toolkit.core.ops.preflight.detect_host_capacity",
                    return_value=_TEST_CAP,
                )
            )
            stack.enter_context(
                patch(
                    "toolkit.core.capabilities.detect.detect_lxc_capacity",
                    return_value=None,
                )
            )
            stack.enter_context(
                patch(
                    "toolkit.core.capabilities.detect_gpu_for_vm",
                    return_value=_TEST_GPU,
                )
            )
            stack.enter_context(
                patch(
                    "toolkit.core.capabilities.detect_server_capabilities",
                    return_value=ServerCapabilities(host=_TEST_CAP, gpu=_TEST_GPU, vm="media"),
                )
            )
        if mod not in _TDARR_TEST_MODULES:
            stack.enter_context(patch("toolkit.services.tdarr.bootstrap.wait_for_tdarr", return_value=False))
        if mod not in ("test_caddy_validate",):
            stack.enter_context(
                patch("toolkit.services.caddy.plugin.validate_generated_caddyfile", lambda *_a, **_k: None),
            )
        if mod not in _SECRETS_CRYPTO_TEST_MODULES:
            stack.enter_context(
                patch.dict("os.environ", {"HOMELAB_TEST_PLAINTEXT_SECRETS": "1"}),
            )
            stack.enter_context(
                patch("toolkit.core.secrets.secrets.secrets_encryption_available", return_value=False),
            )
        if mod != "test_automation":
            arr_module = importlib.import_module("toolkit.services._arr")
            stack.enter_context(patch.object(arr_module, "wait_for_arr_api", return_value=False))
        yield


@pytest.fixture
def tmp_root(tmp_path: Path) -> Path:
    return tmp_path / "homelab"


@pytest.fixture
def sample_config() -> Config:
    return Config(domain="example.com", email="admin@example.com")


@pytest.fixture
def seed_oidc_secrets():
    """Persist all enabled manifest OIDC secrets required by generation tests."""
    from toolkit.core.config.storage import secrets_path
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.routes import service_is_enabled
    from toolkit.core.secrets.secrets import load_secrets_plaintext, save_secrets_plaintext

    def seed(cfg: Config, root: Path, extra: dict[str, str] | None = None) -> dict[str, str]:
        path = secrets_path(root)
        values = load_secrets_plaintext(path)
        for manifest in load_service_catalog().manifests:
            if manifest.oidc is not None and service_is_enabled(cfg, manifest):
                values.setdefault(manifest.oidc.secret_env_var, f"test-{manifest.oidc.client_id}-secret")
        values.setdefault("AUTHELIA_OIDC_HMAC_SECRET", "test-authelia-hmac-secret-" + "x" * 64)
        values.setdefault("WAZUH_INDEXER_PASSWORD", "test-wazuh-indexer-password")
        values.setdefault("WAZUH_DASHBOARD_PASSWORD", "test-wazuh-dashboard-password")
        values.update(extra or {})
        save_secrets_plaintext(values, path)
        return values

    return seed


@pytest.fixture
def config_path(tmp_root: Path, sample_config: Config) -> Path:
    path = tmp_root / "config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = sample_config.model_dump(mode="json", exclude_defaults=False)
    path.write_text(yaml.dump(data, default_flow_style=False, sort_keys=False))
    return path


@pytest.fixture
def tmp_config(tmp_path: Path) -> Path:
    """Provide a temp dir with a saved config file."""
    from toolkit.core.config.config import save_config

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = Config(domain="test.example.com", email="admin@test.example.com")
    cp = root / "config.yaml"
    save_config(cfg, cp)
    return root


@pytest.fixture
def mock_docker(monkeypatch):
    """Mock docker compose subprocess calls."""
    import subprocess

    calls = []

    def mock_run(*args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args=args, returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", mock_run)
    return calls


@pytest.fixture
def full_config() -> Config:
    """Config with ALL service categories enabled."""
    from toolkit.core.config.config import ServicesConfig

    return Config(
        domain="test.example.com",
        email="admin@test.example.com",
        services=ServicesConfig(
            management=True,
            media=True,
            cloud=True,
            notifications=True,
            security=True,
            email=True,
        ),
    )
