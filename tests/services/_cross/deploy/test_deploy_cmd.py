from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from click.testing import CliRunner
from toolkit.cli import main
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path


def _setup_config(root: Path) -> None:
    cfg = Config(domain="example.com", email="admin@example.com")
    save_config(cfg, config_path(root))


def test_deploy_all_help_includes_yes():
    runner = CliRunner()
    result = runner.invoke(main, ["deploy", "all", "--help"])
    assert result.exit_code == 0
    assert "--yes" in result.output
    assert "-y" in result.output


def test_destroy_host_reports_checkpoint_refusal_without_traceback(tmp_path: Path):
    runner = CliRunner()
    with patch(
        "toolkit.core.infra.infra_destroy.destroy_host_guarded",
        side_effect=RuntimeError("No verified recovery checkpoint is available"),
    ):
        result = runner.invoke(main, ["--root", str(tmp_path), "deploy", "destroy-host", "--yes"])

    assert result.exit_code != 0
    assert "No verified recovery checkpoint is available" in result.output
    assert "Traceback" not in result.output


def test_destroy_infra_uses_controller_plan_approval_and_job_progress(tmp_path: Path):
    client = MagicMock()
    plan = SimpleNamespace(
        plan_id="plan-identifier-1234",
        plan_hash="a" * 64,
        spec=SimpleNamespace(
            action="destroy_all",
            scopes=["infra", "apps", "media"],
            config_revision="c" * 64,
            checkpoint_id="b" * 32,
            checkpoint_verified_at=datetime(2026, 7, 10, tzinfo=UTC),
        ),
    )
    client.create_destruction_plan.return_value = plan
    client.approve_plan.return_value = SimpleNamespace(token="approval-token-123456")
    client.submit.return_value = SimpleNamespace(job_id="job-123456789012")
    finished = SimpleNamespace(job_id="job-123456789012", state=SimpleNamespace(value="SUCCEEDED"), error=None)
    direct_destroy = MagicMock()

    with (
        patch("toolkit.cli.controller_client_from_environment", return_value=client),
        patch("toolkit.cli.controller_jobs.wait_for_controller_job", return_value=finished),
        patch("toolkit.core.infra.infra_destroy.destroy_infrastructure_guarded", direct_destroy),
    ):
        result = CliRunner().invoke(
            main,
            ["--root", str(tmp_path), "deploy", "destroy-infra", "--yes"],
        )

    assert result.exit_code == 0
    assert "Checkpoint" in result.output
    assert "Infrastructure destroyed and independently verified" in result.output
    client.create_destruction_plan.assert_called_once()
    client.approve_plan.assert_called_once_with(
        plan.plan_id,
        plan_hash=plan.plan_hash,
        confirmation="DESTROY ALL MANAGED INFRASTRUCTURE",
    )
    client.submit.assert_called_once()
    direct_destroy.assert_not_called()


def test_deploy_all_accepts_y_flag(tmp_path: Path):
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--root", str(tmp_path), "deploy", "all", "-y", "--skip-infra", "--skip-dns"],
    )
    assert "No such option: -y" not in result.output
    assert result.exit_code != 2


def test_deploy_all_help(tmp_path: Path):
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "deploy", "all", "--help"])
    assert result.exit_code == 0
    assert "deploy" in result.output.lower()


def test_deploy_all_dry_run_requires_config_without_bootstrapping(tmp_path: Path):
    """A plan must never create config, secrets, or generated artifacts."""
    runner = CliRunner()

    result = runner.invoke(main, ["--root", str(tmp_path), "deploy", "all", "--dry-run"])

    assert result.exit_code != 0
    assert "requires an existing config.yaml" in result.output
    assert not (tmp_path / "config.yaml").exists()
    assert not (tmp_path / "secrets.env").exists()
    assert not (tmp_path / "generated").exists()


def test_deploy_all_dry_run_bypasses_bootstrap_and_zfs_detection(tmp_path: Path):
    """Configured dry-runs only read the manifest/configuration state."""
    _setup_config(tmp_path)
    runner = CliRunner()

    async def dry_run(*_args, **_kwargs):
        return SimpleNamespace(success=True)

    with (
        patch("toolkit.core.deploy.deploy_workflow.run_dry_run_workflow", new=dry_run),
        patch("toolkit.core.infra.zfs_detect.detect_and_merge_zfs") as zfs_detect,
        patch("toolkit.core.secrets.secrets.generate_all_secrets") as generate_secrets,
        patch("toolkit.core.generate.generate.run_full_generate") as generate,
    ):
        result = runner.invoke(main, ["--root", str(tmp_path), "deploy", "all", "--dry-run"])

    assert result.exit_code == 0, result.output
    zfs_detect.assert_not_called()
    generate_secrets.assert_not_called()
    generate.assert_not_called()
    assert not (tmp_path / "secrets.env").exists()
    assert not (tmp_path / "generated").exists()


def test_deploy_all_with_existing_config(tmp_path: Path):
    _setup_config(tmp_path)
    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--root", str(tmp_path), "deploy", "all", "--skip-infra", "--skip-dns", "--json"],
    )
    assert result.exit_code != 2  # Not a click usage error


def test_deploy_all_creates_config_from_manifest_setting_environment(tmp_path: Path, monkeypatch):
    """When no config exists, deploy all creates one from env vars."""
    monkeypatch.setenv("HOMELAB_DOMAIN", "auto-test.example.com")
    monkeypatch.setenv("HOMELAB_SETTING_MEDIA_LIBRARY_SERVER", "plex")
    monkeypatch.setenv("HOMELAB_SETTING_MEDIA_CACHE_ENABLED", "true")
    runner = CliRunner()
    result = runner.invoke(
        main,
        [
            "--root",
            str(tmp_path),
            "deploy",
            "all",
            "--skip-infra",
            "--skip-dns",
            "--json",
        ],
    )
    assert result.exit_code != 2
    assert (tmp_path / "config.yaml").exists()
    config = Config.model_validate(__import__("yaml").safe_load((tmp_path / "config.yaml").read_text()))
    assert config.service_settings["media-library"]["server"] == "plex"
    assert config.service_settings["media-cache"]["enabled"] is True


def test_deploy_verify_hooks_flag(tmp_path: Path, monkeypatch):
    _setup_config(tmp_path)
    from toolkit.core.ops.hook_verify import HookVerifyResult

    loaded_roles: list[str | None] = []

    monkeypatch.setattr(
        "toolkit.core.ops.hook_verify.verify_hooks",
        lambda *_a, **_k: HookVerifyResult(),
    )
    monkeypatch.setattr(
        "toolkit.core.secrets.secrets.load_runtime_secrets",
        lambda _root, *, role=None: loaded_roles.append(role) or {},
    )

    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "deploy", "verify", "--hooks", "--node", "infra"])
    assert result.exit_code == 0
    assert "checks passed" in result.output.lower()
    assert loaded_roles == ["infra"]


def test_deploy_recover_help():
    runner = CliRunner()
    result = runner.invoke(main, ["deploy", "recover", "--help"])
    assert result.exit_code == 0
    assert "--node" in result.output
    assert "--json" in result.output


def test_deploy_recover_calls_workflow(tmp_path: Path, monkeypatch):
    _setup_config(tmp_path)
    from toolkit.core.deploy.deploy_lock import DeployLockStatus
    from toolkit.core.deploy.deploy_workflow import DeployWorkflowResult

    called: dict = {}

    async def fake_recover(*args, **kwargs):
        called["kwargs"] = kwargs
        return DeployWorkflowResult(
            success=True,
            message="recover ok",
            notification_type="positive",
            step_status={},
        )

    monkeypatch.setattr("toolkit.core.deploy.deploy_workflow.run_recover_workflow", fake_recover)
    monkeypatch.setattr(
        "toolkit.core.deploy.deploy_lock.read_deploy_lock",
        lambda _r: DeployLockStatus(locked=False, message="No deploy in progress"),
    )

    runner = CliRunner()
    result = runner.invoke(
        main,
        ["--root", str(tmp_path), "deploy", "recover", "--node", "media"],
    )
    assert result.exit_code == 0
    assert called["kwargs"]["vm"] == "media"
    assert "recover ok" in result.output


def test_deploy_ssh_test_fails_on_bad_ssh(tmp_path: Path, monkeypatch):
    _setup_config(tmp_path)
    monkeypatch.setattr(
        "toolkit.core.infra.ssh_probe.probe_ssh_connectivity",
        lambda *_a, **_k: ["SSH: FAIL infra (10.0.0.1) — timeout"],
    )
    runner = CliRunner()
    result = runner.invoke(main, ["--root", str(tmp_path), "deploy", "ssh-test"])
    assert result.exit_code == 1
    assert "FAIL" in result.output
