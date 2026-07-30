from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from toolkit.core.infra.infra_env import load_tofu_env, tofu_env_from_secrets


def test_tofu_env_from_secrets_sets_proxmox_tokens():
    env = tofu_env_from_secrets(
        {
            "PROXMOX_API_TOKEN_ID": "root@pam!ci",
            "PROXMOX_API_TOKEN_SECRET": "secret",
        },
        allow_destroy=True,
    )
    assert env["TF_VAR_proxmox_api_token"] == "root@pam!ci=secret"
    assert env["TF_VAR_allow_destroy"] == "true"


def test_tofu_env_from_secrets_enriches_root_paths(tmp_path: Path):
    root = tmp_path / "homelab"
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / "automation" / "ansible").mkdir(parents=True)
    (root / "automation" / "ansible" / "ansible.cfg").write_text("[defaults]\n")

    env = tofu_env_from_secrets({}, root=root)

    assert env["HOMELAB_ROOT"] == str(root.resolve())
    assert str(root / ".venv" / "bin") in env["PATH"]
    assert env["ANSIBLE_CONFIG"].endswith("ansible.cfg")


def test_load_tofu_env_reads_secrets(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    (root / "secrets.enc.yaml").write_text("x: y\n")

    with patch(
        "toolkit.core.secrets.secrets.load_secrets_plaintext",
        return_value={
            "PROXMOX_API_TOKEN_ID": "a",
            "PROXMOX_API_TOKEN_SECRET": "b",
        },
    ):
        env = load_tofu_env(root)

    assert env["TF_VAR_proxmox_api_token_id"] == "a"
    assert env["TF_VAR_proxmox_api_token_secret"] == "b"


def test_load_tofu_env_injects_proxmox_ca_bundle(tmp_path: Path):
    root = tmp_path / "homelab"
    root.mkdir()
    (root / "config.yaml").write_text(
        "proxmox:\n  api_url: https://pve.example.test:8006\n",
        encoding="utf-8",
    )
    bundle = root / ".homelab-state" / "trust" / "proxmox-ca-bundle.pem"

    with (
        patch("toolkit.core.secrets.secrets.load_secrets_plaintext", return_value={}),
        patch("toolkit.core.infra.proxmox_tls.ensure_proxmox_ca_bundle", return_value=bundle),
    ):
        env = load_tofu_env(root)

    assert env["SSL_CERT_FILE"] == str(bundle)


def test_every_deploy_workflow_tofu_process_receives_managed_environment():
    deploy_dir = Path(__file__).resolve().parents[3] / "toolkit/core/deploy"
    source = "\n".join(
        (deploy_dir / filename).read_text(encoding="utf-8") for filename in ("deploy_workflow.py", "deploy_wipe.py")
    )

    assert source.count("env=tofu_env,") == 4
