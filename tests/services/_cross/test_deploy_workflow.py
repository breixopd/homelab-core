from __future__ import annotations

import asyncio
import importlib
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, MagicMock, patch

import pytest
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.deploy.deploy import DeployResult
from toolkit.core.deploy.deploy_qa import QAResult
from toolkit.core.generate.generate import generate_all
from toolkit.core.generate.validate import ValidationReport
from toolkit.core.infra.host_capacity import HostCapacity

KOMODO_PLUGIN = importlib.import_module("toolkit.services.komodo-core.plugin").KomodoPlugin
GITEA_PLUGIN = importlib.import_module("toolkit.services.gitea.plugin").GiteaPlugin

# Lightweight local capacity so tests never SSH-probe Proxmox.
_FAKE_CAP = HostCapacity(
    cpu_cores=8,
    mem_total_mb=16384,
    load_1m=1.0,
    wave_timeout_s=180,
    inter_wave_sleep_s=5,
    max_pull_parallel=2,
    load_threshold=16.0,
    source="local",
)


def _generate_result(report: ValidationReport) -> AsyncMock:
    return AsyncMock(return_value=(report, None))


async def _inline_to_thread(func, /, *args, **kwargs):
    return func(*args, **kwargs)


@pytest.fixture(autouse=True)
def _hermetic_workflow():
    """Patch all external-I/O boundaries so workflow-logic tests never touch the
    network, SSH, tofu, or ansible (they would otherwise hang on real subprocesses)."""
    with (
        patch.dict(
            "os.environ",
            {
                "HOMELAB_DEPLOY_SOAK_SECONDS": "0",
                "HOMELAB_SKIP_DEPLOY_CLEANUP": "1",
                "HOMELAB_TEST_PLAINTEXT_SECRETS": "1",
            },
        ),
        patch("toolkit.core.deploy.deploy_workflow.run_blocking", new=_inline_to_thread),
        patch("toolkit.core.deploy.deploy_workflow.detect_host_capacity", return_value=_FAKE_CAP),
        # The workflow now owns the typed safety-gate dispatch. Keep generic
        # pipeline tests hermetic and explicitly mark the gate not applicable.
        patch("toolkit.core.deploy.deploy_workflow._run_pre_deploy_dump", return_value=(False, None)),
        patch("toolkit.core.infra.ssh_probe.ssh_ok", return_value=False),
        patch("toolkit.core.deploy.deploy_workflow._ensure_guest_custom_images", new=AsyncMock(return_value=None)),
        patch("toolkit.services.jellyfin.capabilities.resolve_hw_transcode", return_value="none"),
        patch("toolkit.services.tdarr.capabilities.resolve_cpu_workers", return_value=2),
        patch("toolkit.services.tdarr.capabilities.resolve_gpu_workers", return_value=0),
        patch("toolkit.core.infra.iac_sync.sync_from_repo_root", return_value=None),
        patch("toolkit.core.ansible.ansible_inventory.ensure_group_vars_all", return_value=None),
        patch("toolkit.core.ansible.ansible_inventory.parse_tofu_machine_ips", return_value={}),
        patch("toolkit.core.ansible.ansible_inventory.write_inventory", return_value=None),
        patch(
            "toolkit.core.deploy.deploy_qa.run_infrastructure_qa",
            return_value=QAResult(ok=True),
        ),
        patch("toolkit.core.ops.dns.verify_dns_propagation", return_value=True),
        patch(
            "toolkit.services.vaultwarden.bootstrap.sync_catalog_to_vaultwarden",
            return_value=[],
        ),
        patch.object(KOMODO_PLUGIN, "reconcile_runtime_credentials", return_value=[]),
        patch.object(GITEA_PLUGIN, "reconcile_runtime_credentials", return_value=[]),
        patch(
            "toolkit.core.infra.ssh_probe.probe_ssh_connectivity",
            return_value=["SSH: OK infra (10.0.0.1) → infra-01"],
        ),
        patch(
            "toolkit.core.images.publish.verify_guest_images",
            return_value=(True, []),
        ),
    ):
        yield


def _setup(root: Path) -> Config:
    cfg = Config(
        domain="example.com",
        email="admin@example.com",
        service_settings={"jellyfin": {"hardware-transcode": "none"}},
    )
    cfg.proxmox.provision_machines = False  # avoid requiring tofu/ansible/jq
    save_config(cfg, config_path(root))
    inv_dir = root / "automation" / "ansible" / "inventory"
    inv_dir.mkdir(parents=True, exist_ok=True)
    (inv_dir / "hosts.yml").write_text("all:\n  children:\n    guest_hosts:\n      hosts: {}\n")
    generate_all(root)
    return cfg


def test_deploy_workflow_docker_preflight_fails(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_deploy_workflow

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    (root / "docker-compose.yml").write_text("name: homelab\n")

    logs: list[str] = []
    steps: list[tuple[str, str]] = []

    def on_log(msg: str):
        logs.append(msg)

    def on_step(step: str, status: str):
        steps.append((step, status))

    validation_report = ValidationReport(errors=[], warnings=[], skipped=[], checks=["ok"])

    with (
        patch(
            "toolkit.core.deploy.deploy_workflow.generate_and_validate_artifacts",
            new=_generate_result(validation_report),
        ),
        patch("toolkit.core.deploy.deploy_workflow.detect_host_capacity", return_value=_FAKE_CAP),
        patch("toolkit.core.deploy.deploy_workflow.run_preflight", return_value=[]),
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=True),
        patch("toolkit.core.compose.docker.compose_for_root") as mock_compose_root,
    ):
        mock_dc = MagicMock()
        mock_dc.preflight.return_value = False
        mock_compose_root.return_value = mock_dc

        result = asyncio.run(run_deploy_workflow(root, cfg, on_log=on_log, on_step=on_step))

    assert result.success is False
    assert "Docker daemon unavailable" in result.message
    assert result.step_status["deploy_infra"] == "fail"
    assert result.step_status["hooks"] == "skip"
    assert result.step_status["verify"] == "skip"
    assert result.step_status["dns"] == "skip"


def test_deploy_workflow_all_vms_deploy_to_local(tmp_path: Path, monkeypatch):
    import toolkit.core.deploy.deploy as deploy_mod
    from toolkit.core.deploy.deploy_workflow import run_deploy_workflow

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    (root / "docker-compose.yml").write_text("name: homelab\n")

    logs: list[str] = []
    steps: list[tuple[str, str]] = []

    def on_log(msg: str):
        logs.append(msg)

    def on_step(step: str, status: str):
        steps.append((step, status))

    class FakeCompose:
        def __init__(self, compose_file, env_file=None, project_name=None):
            pass

        def preflight(self):
            return True

        def pull(self, services=None):
            return True

        def pull_retry(self, services=None, profiles=None, **kwargs):
            return True

        def up(self, services=None, detach=True, profiles=None):
            return True

        def ps(self):
            return []

    validation_report = ValidationReport(errors=[], warnings=[], skipped=[], checks=["ok"])

    from toolkit.core.ops.verify import VerifyResult

    ok_verify = {vm: VerifyResult(vm=vm, docker_ok=True, compose_ok=True) for vm in cfg.enabled_nodes}

    from toolkit.core.ops.hook_verify import HookVerifyResult

    empty_hooks = HookVerifyResult()

    def fake_deploy_local(root: Path, vm: str, config) -> DeployResult:
        return DeployResult(vm=vm, success=True, services_started=["svc1"])

    with (
        patch(
            "toolkit.core.deploy.deploy_workflow.generate_and_validate_artifacts",
            new=_generate_result(validation_report),
        ),
        patch("toolkit.core.deploy.deploy_workflow.deploy_local", fake_deploy_local),
        patch.object(deploy_mod, "DockerCompose", FakeCompose),
        patch.object(deploy_mod, "health_gate", lambda *a, **k: {}),
        patch("toolkit.core.deploy.deploy_workflow.detect_host_capacity", return_value=_FAKE_CAP),
        patch("toolkit.core.deploy.deploy_workflow.run_preflight", return_value=[]),
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=True),
        patch("toolkit.core.compose.docker.compose_for_root") as mock_cfr,
        patch(
            "toolkit.core.deploy.deploy_workflow.run_post_start_hooks_remote",
            return_value=({}, True),
        ),
        patch("toolkit.core.deploy.deploy_workflow.verify_all", return_value=ok_verify),
        patch("toolkit.core.ops.hook_verify.verify_hooks", return_value=empty_hooks),
        patch("toolkit.core.secrets.secrets.load_secrets_plaintext", return_value={}),
        patch("toolkit.core.ops.dns.resolve_public_dns_ip", return_value=("", "missing")),
    ):
        mock_cfr.return_value = FakeCompose(None)

        result = asyncio.run(run_deploy_workflow(root, cfg, on_log=on_log, on_step=on_step))

    assert result.success is True
    assert result.step_status["deploy_infra"] == "ok"


def test_deploy_workflow_full_success(tmp_path: Path):
    """Test that all 5 steps pass when everything is mocked correctly."""
    from toolkit.core.deploy.deploy_workflow import run_deploy_workflow

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    (root / "docker-compose.yml").write_text("name: homelab\n")

    logs: list[str] = []
    steps: list[tuple[str, str]] = []

    def on_log(msg: str):
        logs.append(msg)

    def on_step(step: str, status: str):
        steps.append((step, status))

    class FakeCompose:
        def __init__(self, compose_file, env_file=None, project_name=None):
            pass

        def preflight(self):
            return True

        def pull(self, services=None):
            return True

        def pull_retry(self, services=None, profiles=None, **kwargs):
            return True

        def up(self, services=None, detach=True, profiles=None):
            return True

        def ps(self):
            return []

    def fake_deploy_local(root: Path, vm: str, config) -> DeployResult:
        return DeployResult(vm=vm, success=True, services_started=["svc1", "svc2"])

    from toolkit.core.ops.verify import VerifyResult

    validation_report = ValidationReport(errors=[], warnings=[], skipped=[], checks=["ok"])
    ok_verify = {vm: VerifyResult(vm=vm, docker_ok=True, compose_ok=True) for vm in cfg.enabled_nodes}

    from toolkit.core.ops.hook_verify import HookVerifyResult

    empty_hooks = HookVerifyResult()

    with (
        patch(
            "toolkit.core.deploy.deploy_workflow.generate_and_validate_artifacts",
            new=_generate_result(validation_report),
        ),
        patch("toolkit.core.compose.docker.DockerCompose", FakeCompose),
        patch("toolkit.core.deploy.deploy_workflow.deploy_local", fake_deploy_local),
        patch("toolkit.core.deploy.deploy.health_gate", lambda *a, **k: {}),
        patch("toolkit.core.deploy.deploy_workflow.detect_host_capacity", return_value=_FAKE_CAP),
        patch("toolkit.core.deploy.deploy_workflow.run_preflight", return_value=[]),
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=True),
        patch(
            "toolkit.core.deploy.deploy_workflow.run_post_start_hooks_remote",
            return_value=({}, True),
        ),
        patch("toolkit.core.deploy.deploy_workflow.verify_all", return_value=ok_verify),
        patch("toolkit.core.ops.hook_verify.verify_hooks", return_value=empty_hooks),
        patch("toolkit.core.ops.dns.resolve_public_dns_ip") as mock_dns_ip,
        patch("toolkit.core.ops.dns.CloudflareDNS") as mock_cf,
        patch("toolkit.core.compose.docker.compose_for_root") as mock_cfr,
        patch(
            "toolkit.core.secrets.secrets.load_secrets_plaintext",
            return_value={"CLOUDFLARE_API_TOKEN": "tok"},
        ),
        patch("toolkit.core.deploy.deploy_notify.send_deploy_notification", return_value=True),
    ):
        mock_dns_ip.return_value = ("1.2.3.4", "test")
        mock_cf_instance = MagicMock()
        mock_cf_instance.sync_records.return_value = {"created": 1, "updated": 0}
        mock_cf.return_value = mock_cf_instance
        mock_cfr.return_value = FakeCompose(None)

        result = asyncio.run(run_deploy_workflow(root, cfg, on_log=on_log, on_step=on_step))

    assert result.success is True
    assert result.message == "Deployment complete!"
    assert result.notification_type == "positive"
    assert result.step_status["generate"] == "ok"
    assert result.step_status["infra"] == "ok"
    assert result.step_status["deploy_infra"] == "ok"
    assert result.step_status["hooks"] == "ok"


def test_deploy_workflow_skip_infra_uses_deploy_playbook(tmp_path: Path):
    """skip-infra redeploy must not re-run full guest-setup."""
    from toolkit.core.deploy.deploy_workflow import select_guest_deploy_playbook

    root = tmp_path / "homelab"
    root.mkdir()
    ansible_dir = root / "automation" / "ansible"
    ansible_dir.mkdir(parents=True)
    (ansible_dir / "guest-setup.yml").write_text("---\n- hosts: all\n  tasks: []\n")
    deploy_pb = ansible_dir / "playbooks" / "deploy-server-toolkit.yml"
    deploy_pb.parent.mkdir(parents=True, exist_ok=True)
    deploy_pb.write_text("---\n- hosts: all\n  tasks: []\n")

    playbook, label = select_guest_deploy_playbook(root, skip_infra=True)

    assert playbook == deploy_pb
    assert label == "deploy-server-toolkit"


def test_deploy_step_id():
    from toolkit.core.deploy.deploy_workflow import deploy_step_id

    assert deploy_step_id("infra") == "deploy_infra"
    assert deploy_step_id("media") == "deploy_media"


def test_targeted_remote_deploy_uses_exact_inventory_group() -> None:
    from toolkit.core.deploy.deploy_workflow import ansible_target_limit

    assert ansible_target_limit(("infra", "media")) == "infra:media"
    assert ansible_target_limit(("edge-a",)) == "edge-a"
    assert ansible_target_limit(None) is None


def test_workflow_progress_percent():
    from toolkit.core.deploy.deploy_workflow import workflow_progress_percent, workflow_step_labels

    cfg = Config(domain="example.com")
    labels = workflow_step_labels(cfg)
    step_status = {step: "pending" for step in labels}
    assert workflow_progress_percent(step_status, cfg) == 0

    step_status["preflight"] = "ok"
    step_status["generate"] = "running"
    pct = workflow_progress_percent(step_status, cfg)
    assert 0 < pct < 100

    for step in labels:
        step_status[step] = "ok"
    assert workflow_progress_percent(step_status, cfg) == 100


def test_workflow_step_labels_includes_vm_steps():
    from toolkit.core.deploy.deploy_workflow import workflow_step_labels

    cfg = Config(domain="example.com")
    labels = workflow_step_labels(cfg)
    assert "deploy_infra" in labels
    assert labels["preflight"] == "Pre-flight checks"
    assert list(labels).index("dns") < list(labels).index("hook_verify")


def test_guest_deploy_syncs_dns_before_hook_verification():
    playbook = (
        Path(__file__).parents[3] / "automation" / "ansible" / "playbooks" / "deploy-server-toolkit.yml"
    ).read_text()

    assert playbook.index("name: Sync DNS records to Cloudflare") < playbook.index(
        "name: Verify post-start hooks for this VM role"
    )


def test_run_dry_run_workflow(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_dry_run_workflow

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    logs: list[str] = []

    result = asyncio.run(run_dry_run_workflow(root, cfg, on_log=logs.append))

    assert result.success is True
    assert "Dry-run complete" in result.message
    assert any("HOMELAB TOOLKIT — DRY RUN" in line for line in logs)
    assert any("Configuration Summary" in line for line in logs)


def test_run_dry_run_workflow_does_not_probe_unconfigured_host_capacity(tmp_path: Path):
    """A dry-run remains offline unless the operator supplied capacity values."""
    from toolkit.core.deploy.deploy_workflow import run_dry_run_workflow

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    logs: list[str] = []

    with patch("toolkit.core.deploy.deploy_workflow.detect_host_capacity") as detect_capacity:
        result = asyncio.run(run_dry_run_workflow(root, cfg, on_log=logs.append))

    assert result.success is True
    detect_capacity.assert_not_called()
    assert any("offline estimate unavailable" in line for line in logs)


def test_run_post_start_hooks_remote_local_path(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_post_start_hooks_remote

    cfg = Config(domain="example.com")
    cfg.proxmox.provision_machines = False

    with (
        patch(
            "toolkit.core.deploy.deploy_workflow.run_post_start_hooks",
            return_value={"management": ["hook ok"]},
        ),
        patch(
            "toolkit.core.deploy.hook_audit.audit_hook_results",
            return_value=MagicMock(passed=True),
        ),
    ):
        results, ok = run_post_start_hooks_remote(cfg, tmp_path, "infra")

    assert ok is True
    assert results["management"] == ["hook ok"]


def test_run_post_start_hooks_remote_executes_on_managed_guest(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_post_start_hooks_remote

    cfg = Config(domain="example.com")
    cfg.proxmox.provision_machines = True

    with (
        patch(
            "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
            return_value=(0, "hook ok\n", ""),
        ) as ssh,
        patch(
            "toolkit.core.deploy.hook_audit.audit_hook_results",
            return_value=MagicMock(passed=True),
        ),
    ):
        results, ok = run_post_start_hooks_remote(cfg, tmp_path, "infra")

    assert ok is True
    assert results == {"infra": ["hook ok"]}
    assert ssh.call_args.args[1] == cfg.node_ip("infra")
    assert "deploy hooks --node infra" in ssh.call_args.args[2]
    assert ssh.call_args.kwargs["timeout"] == 900


def test_run_post_start_hooks_remote_fails_closed_on_guest_error(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_post_start_hooks_remote

    cfg = Config(domain="example.com")
    cfg.proxmox.provision_machines = True

    with (
        patch(
            "toolkit.core.ansible.ansible_ssh.ssh_run_on_vm",
            return_value=(1, "", "hook command failed"),
        ),
        patch(
            "toolkit.core.deploy.hook_audit.audit_hook_results",
            return_value=MagicMock(passed=False),
        ),
    ):
        results, ok = run_post_start_hooks_remote(cfg, tmp_path, "infra")

    assert ok is False
    assert results == {"infra": ["hook command failed"]}


def test_run_post_start_hooks_skips_when_no_containers(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_post_start_hooks

    cfg = Config(domain="example.com")
    mock_dc = MagicMock()
    mock_dc.ps.return_value = []

    with patch("toolkit.core.deploy.deploy_workflow.compose_for_root", return_value=mock_dc):
        results = run_post_start_hooks(cfg, tmp_path, vm="infra")

    assert "infra" in results
    assert "skipped service setup" in results["infra"][0].lower()


def test_reconcile_infrastructure_secrets_persists_lxc_passwords(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import reconcile_infrastructure_secrets

    with (
        patch(
            "toolkit.core.secrets.secrets.extract_lxc_root_passwords",
            return_value={"infra": "one", "apps": "two"},
        ),
        patch("toolkit.core.secrets.secrets.merge_secret_values") as merge,
    ):
        logs = reconcile_infrastructure_secrets(tmp_path)

    assert logs == ["LXC credentials: saved 2 machine passwords from OpenTofu state"]
    merge.assert_called_once()
    assert merge.call_args.args[0] == tmp_path
    assert '"infra": "one"' in merge.call_args.args[1]["LXC_ROOT_PASSWORDS"]


def test_check_storage_active_no_inventory(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import _check_storage_active

    assert asyncio.run(_check_storage_active(tmp_path)) is False


def test_check_storage_active_no_ansible_binary(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import _check_storage_active

    inv = tmp_path / "automation" / "ansible" / "inventory"
    inv.mkdir(parents=True)
    (inv / "hosts.yml").write_text("all:\n  hosts: {}\n")

    with patch("shutil.which", return_value=None):
        assert asyncio.run(_check_storage_active(tmp_path)) is False


def test_check_storage_active_reads_subprocess_output(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import _check_storage_active

    inv = tmp_path / "automation" / "ansible" / "inventory"
    inv.mkdir(parents=True)
    (inv / "hosts.yml").write_text("all:\n  hosts: {}\n")

    proc = MagicMock()
    proc.communicate = AsyncMock(return_value=(b"active\n", b""))

    with (
        patch("shutil.which", return_value="/usr/bin/ansible"),
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch(
            "toolkit.core.ansible.ansible_inventory.generated_extra_vars",
            return_value=[],
        ),
    ):
        assert asyncio.run(_check_storage_active(tmp_path)) is True


def test_check_storage_active_subprocess_error_fails_closed(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import _check_storage_active

    inv = tmp_path / "automation" / "ansible" / "inventory"
    inv.mkdir(parents=True)
    (inv / "hosts.yml").write_text("all:\n  hosts: {}\n")

    with (
        patch("shutil.which", return_value="/usr/bin/ansible"),
        patch("asyncio.create_subprocess_exec", side_effect=OSError("unavailable")),
        patch(
            "toolkit.core.ansible.ansible_inventory.generated_extra_vars",
            return_value=[],
        ),
    ):
        assert asyncio.run(_check_storage_active(tmp_path)) is False


def test_check_storage_active_timeout_fails_closed(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import _check_storage_active

    inv = tmp_path / "automation" / "ansible" / "inventory"
    inv.mkdir(parents=True)
    (inv / "hosts.yml").write_text("all:\n  hosts: {}\n")

    proc = MagicMock()
    proc.communicate = AsyncMock()

    async def timeout(awaitable, *, timeout):
        awaitable.close()
        raise TimeoutError

    with (
        patch("shutil.which", return_value="/usr/bin/ansible"),
        patch("asyncio.create_subprocess_exec", return_value=proc),
        patch("asyncio.wait_for", new=timeout),
        patch(
            "toolkit.core.ansible.ansible_inventory.generated_extra_vars",
            return_value=[],
        ),
    ):
        assert asyncio.run(_check_storage_active(tmp_path)) is False


def test_run_recover_workflow_missing_playbook(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_recover_workflow

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)

    result = asyncio.run(
        run_recover_workflow(
            root,
            cfg,
            on_log=lambda _m: None,
            on_step=lambda _s, _st: None,
        )
    )

    assert result.success is False
    assert "missing" in result.message


def test_run_recover_workflow_preflight_failure(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_recover_workflow
    from toolkit.core.ops.preflight import PreflightItem

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    playbook = root / "automation" / "ansible" / "playbooks" / "deploy-recover.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("---\n- hosts: all\n  tasks: []\n")

    with (
        patch(
            "toolkit.core.deploy.deploy_workflow.run_preflight",
            return_value=[PreflightItem(id="disk", label="Disk space", ok=False)],
        ),
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=False),
    ):
        result = asyncio.run(
            run_recover_workflow(
                root,
                cfg,
                on_log=lambda _m: None,
                on_step=lambda _s, _st: None,
            )
        )

    assert result.success is False
    assert "preflight" in result.message.lower()


def test_run_recover_workflow_success(tmp_path: Path, monkeypatch):
    from toolkit.core.deploy.deploy_workflow import run_recover_workflow
    from toolkit.core.ops.hook_verify import HookVerifyResult
    from toolkit.core.ops.preflight import PreflightItem
    from toolkit.core.ops.verify import VerifyResult

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    monkeypatch.setenv("HOMELAB_NODE", cfg.control_node)
    playbook = root / "automation" / "ansible" / "playbooks" / "deploy-recover.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("---\n- hosts: all\n  tasks: []\n")

    async def fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(side_effect=[b"PLAY [infra-01]\n", b""])
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        return proc

    ok_verify = {vm: VerifyResult(vm=vm, docker_ok=True, compose_ok=True) for vm in cfg.enabled_nodes}

    with (
        patch(
            "toolkit.core.deploy.deploy_workflow.run_preflight",
            return_value=[PreflightItem(id="ssh", label="SSH", ok=True)],
        ) as preflight,
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=True),
        patch("toolkit.core.deploy.deploy_workflow.resolve_tool", return_value="/usr/bin/ansible-playbook"),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch(
            "toolkit.core.deploy.deploy_workflow._ensure_guest_custom_images",
            new=AsyncMock(return_value=None),
        ) as ensure_images,
        patch(
            "toolkit.core.deploy.deploy_workflow.run_post_start_hooks_remote",
            return_value=({}, True),
        ),
        patch("toolkit.core.ops.hook_verify.verify_hooks", return_value=HookVerifyResult()) as verify_hooks,
        patch("toolkit.core.deploy.deploy_workflow.verify_remote", return_value=ok_verify),
        patch("toolkit.core.deploy.deploy_workflow.verify_all", return_value=ok_verify),
        patch("toolkit.core.secrets.secrets.load_secrets_plaintext", return_value={}),
        patch.object(
            KOMODO_PLUGIN,
            "reconcile_runtime_credentials",
            return_value=["Komodo: API key verified"],
        ) as komodo,
    ):
        result = asyncio.run(
            run_recover_workflow(
                root,
                cfg,
                on_log=lambda _m: None,
                on_step=lambda _s, _st: None,
                vm="apps",
            )
        )

    assert result.success is True
    assert preflight.call_args.kwargs == {
        "bootstrap": False,
        "require_provisioning_tools": False,
        "profile": "controller",
    }
    assert result.step_status["deploy_apps"] == "ok"
    ensure_images.assert_awaited_once_with(cfg, root, ANY, vms=("apps",))
    assert verify_hooks.call_args.kwargs["vm"] == "apps"
    komodo.assert_not_called()


def test_recover_progress_tracks_the_active_ansible_node(tmp_path: Path, monkeypatch):
    from toolkit.core.deploy.deploy_workflow import run_recover_workflow
    from toolkit.core.ops.preflight import PreflightItem

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    monkeypatch.setenv("HOMELAB_NODE", cfg.control_node)
    playbook = root / "automation" / "ansible" / "playbooks" / "deploy-recover.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("---\n- hosts: all\n  tasks: []\n")
    snapshots: list[dict[str, str]] = []
    active_steps: list[str] = []

    async def fake_runner(*_args, **kwargs):
        callback = kwargs["on_output"]
        for node in cfg.enabled_nodes:
            callback(f"ok: [{node}-01]")
            active_steps.append(snapshots[-1]["step"])
        return 2

    with (
        patch(
            "toolkit.core.deploy.deploy_workflow.run_preflight",
            return_value=[PreflightItem(id="ssh", label="SSH", ok=True)],
        ),
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=True),
        patch("toolkit.core.ansible.ansible_runner.run_playbook_streaming", side_effect=fake_runner),
    ):
        result = asyncio.run(
            run_recover_workflow(
                root,
                cfg,
                on_log=lambda _message: None,
                on_step=lambda _step, _status: None,
                on_progress=snapshots.append,
            )
        )

    assert result.success is False
    assert active_steps == ["Deploy infra LXC", "Deploy media LXC", "Deploy apps LXC"]


def test_recovery_images_follow_the_generated_release_tag(tmp_path: Path, monkeypatch) -> None:
    from toolkit.core.config.storage import env_path
    from toolkit.core.deploy.deploy_workflow import _generated_custom_image_tag

    cfg = _setup(tmp_path)
    image = SimpleNamespace(env_var="HOMELAB_UI_IMAGE", repository="homelab-toolkit")
    monkeypatch.setattr("toolkit.core.images.publish.expected_images_for_node", lambda *_args: [image])
    expected = "sha-" + "a" * 40
    for node in cfg.enabled_nodes:
        path = env_path(node, tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"HOMELAB_UI_IMAGE={cfg.images.registry}/homelab-toolkit:{expected}\n",
            encoding="utf-8",
        )

    assert _generated_custom_image_tag(cfg, tmp_path, vms=None) == expected


def test_recover_retries_transient_container_health(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_recover_workflow
    from toolkit.core.ops.hook_verify import HookVerifyResult
    from toolkit.core.ops.preflight import PreflightItem
    from toolkit.core.ops.verify import VerifyResult

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    playbook = root / "automation" / "ansible" / "playbooks" / "deploy-recover.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("---\n- hosts: all\n  tasks: []\n")

    async def fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(side_effect=[b""])
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        return proc

    unhealthy = {"infra": VerifyResult(vm="infra", docker_ok=True, compose_ok=False)}
    healthy = {"infra": VerifyResult(vm="infra", docker_ok=True, compose_ok=True)}
    logs: list[str] = []

    with (
        patch(
            "toolkit.core.deploy.deploy_workflow.run_preflight",
            return_value=[PreflightItem(id="ssh", label="SSH", ok=True)],
        ),
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=True),
        patch("toolkit.core.deploy.deploy_workflow.resolve_tool", return_value="/usr/bin/ansible-playbook"),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("toolkit.core.deploy.deploy_workflow.run_post_start_hooks_remote", return_value=({}, True)),
        patch("toolkit.core.ops.hook_verify.verify_hooks", return_value=HookVerifyResult()),
        patch("toolkit.core.deploy.deploy_workflow.verify_remote", side_effect=[unhealthy, healthy]) as verify,
        patch("toolkit.core.secrets.secrets.load_secrets_plaintext", return_value={}),
        patch.object(
            KOMODO_PLUGIN,
            "reconcile_runtime_credentials",
            return_value=["Komodo: API key verified"],
        ) as komodo,
        patch("asyncio.sleep", new=AsyncMock()),
    ):
        result = asyncio.run(
            run_recover_workflow(root, cfg, on_log=logs.append, on_step=lambda _s, _st: None, vm="infra")
        )

    assert result.success
    assert verify.call_count == 2
    komodo.assert_called_once_with(cfg, root)
    assert any("container verification failed on attempt 1/3" in line for line in logs)


def test_recover_stops_after_nonretryable_hook_failure(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_recover_workflow
    from toolkit.core.ops.hook_verify import HookVerifyResult, VerifyCheck
    from toolkit.core.ops.preflight import PreflightItem
    from toolkit.core.ops.verify import VerifyResult

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    playbook = root / "automation" / "ansible" / "playbooks" / "deploy-recover.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("---\n- hosts: all\n  tasks: []\n")
    nonretryable = HookVerifyResult(
        checks=[VerifyCheck("music-sync", "api_status", False, "manual authorization", retryable=False)]
    )
    ok_verify = {vm: VerifyResult(vm=vm, docker_ok=True, compose_ok=True) for vm in cfg.enabled_nodes}

    async def fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(side_effect=[b""])
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        return proc

    with (
        patch(
            "toolkit.core.deploy.deploy_workflow.run_preflight",
            return_value=[PreflightItem(id="ssh", label="SSH", ok=True)],
        ),
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=True),
        patch("toolkit.core.deploy.deploy_workflow.resolve_tool", return_value="/usr/bin/ansible-playbook"),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("toolkit.core.deploy.deploy_workflow.run_post_start_hooks_remote", return_value=({}, True)),
        patch("toolkit.core.ops.hook_verify.verify_hooks", return_value=nonretryable) as verify_hooks,
        patch("toolkit.core.deploy.deploy_workflow.verify_remote", return_value=ok_verify),
        patch("toolkit.core.secrets.secrets.load_secrets_plaintext", return_value={}),
        patch("asyncio.sleep", new=AsyncMock()) as sleep,
        patch.object(
            KOMODO_PLUGIN,
            "reconcile_runtime_credentials",
            return_value=["Komodo: API key verified"],
        ),
    ):
        result = asyncio.run(
            run_recover_workflow(root, cfg, on_log=lambda _m: None, on_step=lambda _s, _st: None, vm="infra")
        )

    assert result.success is False
    assert verify_hooks.call_count == 1
    sleep.assert_not_awaited()


def test_recover_preserves_nonretryable_failure_while_retrying_affected_service(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_recover_workflow
    from toolkit.core.ops.hook_verify import HookVerifyResult, VerifyCheck
    from toolkit.core.ops.preflight import PreflightItem
    from toolkit.core.ops.verify import VerifyResult

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    playbook = root / "automation" / "ansible" / "playbooks" / "deploy-recover.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("---\n- hosts: all\n  tasks: []\n")
    mixed = HookVerifyResult(
        checks=[
            VerifyCheck("music-sync", "api_status", False, "manual authorization", retryable=False),
            VerifyCheck("caddy", "health", False, "temporarily unavailable"),
        ]
    )
    ok_verify = {vm: VerifyResult(vm=vm, docker_ok=True, compose_ok=True) for vm in cfg.enabled_nodes}

    async def fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(side_effect=[b""])
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        return proc

    with (
        patch(
            "toolkit.core.deploy.deploy_workflow.run_preflight",
            return_value=[PreflightItem(id="ssh", label="SSH", ok=True)],
        ),
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=True),
        patch("toolkit.core.deploy.deploy_workflow.resolve_tool", return_value="/usr/bin/ansible-playbook"),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("toolkit.core.deploy.deploy_workflow.run_post_start_hooks_remote", return_value=({}, True)),
        patch(
            "toolkit.core.ops.hook_verify.verify_hooks",
            side_effect=[
                mixed,
                HookVerifyResult(checks=[VerifyCheck("caddy", "health", True, "ready")]),
            ],
        ) as verify_hooks,
        patch("toolkit.core.deploy.deploy_workflow.verify_remote", return_value=ok_verify),
        patch("toolkit.core.secrets.secrets.load_secrets_plaintext", return_value={}),
        patch("asyncio.sleep", new=AsyncMock()) as sleep,
        patch.object(
            KOMODO_PLUGIN,
            "reconcile_runtime_credentials",
            return_value=["Komodo: API key verified"],
        ),
    ):
        result = asyncio.run(
            run_recover_workflow(root, cfg, on_log=lambda _m: None, on_step=lambda _s, _st: None, vm="infra")
        )

    assert result.success is False
    assert verify_hooks.call_count == 2
    assert verify_hooks.call_args_list[1].kwargs["only_services"] == frozenset({"caddy"})
    sleep.assert_awaited_once_with(30)


def test_deploy_workflow_validation_failure(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_deploy_workflow

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    (root / "docker-compose.yml").write_text("name: homelab\n")

    bad_report = ValidationReport(
        errors=["missing generated file"],
        warnings=[],
        skipped=[],
        checks=[],
    )

    with (
        patch(
            "toolkit.core.deploy.deploy_workflow.generate_and_validate_artifacts",
            new=_generate_result(bad_report),
        ),
        patch("toolkit.core.deploy.deploy_workflow.run_preflight", return_value=[]),
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=True),
        patch("shutil.which", return_value="/usr/bin/docker"),
    ):
        result = asyncio.run(
            run_deploy_workflow(
                root,
                cfg,
                on_log=lambda _m: None,
                on_step=lambda _s, _st: None,
            )
        )

    assert result.success is False
    assert "validation failed" in result.message.lower()
    assert result.step_status["generate"] == "fail"


def test_deploy_workflow_missing_tools(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_deploy_workflow

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    cfg.proxmox.provision_machines = True
    (root / "docker-compose.yml").write_text("name: homelab\n")

    def fake_which(name):
        return None

    with (
        patch("toolkit.core.deploy.deploy_workflow.run_preflight", return_value=[]),
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=True),
        patch("shutil.which", side_effect=fake_which),
        patch("toolkit.core.deploy.deploy_workflow.resolve_tool", return_value=None),
    ):
        result = asyncio.run(
            run_deploy_workflow(
                root,
                cfg,
                on_log=lambda _m: None,
                on_step=lambda _s, _st: None,
            )
        )

    assert result.success is False
    assert "Missing" in result.message
    assert result.step_status["generate"] == "fail"


def test_deploy_workflow_skip_infra_does_not_require_tofu_or_jq(tmp_path: Path, monkeypatch):
    from toolkit.core.deploy.deploy_workflow import run_deploy_workflow
    from toolkit.core.ops.preflight import PreflightItem

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    monkeypatch.setenv("HOMELAB_NODE", cfg.control_node)
    cfg.proxmox.provision_machines = True
    (root / "docker-compose.yml").write_text("name: homelab\n")
    clean_report = ValidationReport(errors=[], warnings=[], skipped=[], checks=[])

    def fake_which(name: str) -> str | None:
        return "/usr/bin/docker" if name == "docker" else None

    def fake_resolve(name: str, _root: Path) -> str | None:
        return "/usr/bin/ansible-playbook" if name == "ansible-playbook" else None

    with (
        patch(
            "toolkit.core.deploy.deploy_workflow.generate_and_validate_artifacts",
            new=_generate_result(clean_report),
        ),
        patch(
            "toolkit.core.deploy.deploy_workflow.run_preflight",
            return_value=[PreflightItem(id="ssh", label="SSH", ok=True)],
        ) as preflight,
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", side_effect=[True, False]),
        patch("shutil.which", side_effect=fake_which),
        patch("toolkit.core.deploy.deploy_workflow.resolve_tool", side_effect=fake_resolve),
    ):
        result = asyncio.run(
            run_deploy_workflow(
                root,
                cfg,
                on_log=lambda _m: None,
                on_step=lambda _s, _st: None,
                skip_infra=True,
            )
        )

    assert result.success is False
    assert "pre-flight failed" in result.message.lower()
    assert preflight.call_count == 2
    assert all(call.kwargs["require_provisioning_tools"] is False for call in preflight.call_args_list)
    assert [call.kwargs["bootstrap"] for call in preflight.call_args_list] == [True, False]
    assert all(call.kwargs["profile"] == "controller" for call in preflight.call_args_list)


def test_deploy_workflow_deploy_lock_held(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_deploy_workflow
    from toolkit.core.deploy.operation_lease import OperationLease

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    held = OperationLease.acquire(root, "recover")

    try:
        result = asyncio.run(
            run_deploy_workflow(
                root,
                cfg,
                on_log=lambda _m: None,
                on_step=lambda _s, _st: None,
            )
        )
    finally:
        held.release()

    assert result.success is False
    assert ".deploy.lock" in result.message


def test_deploy_workflow_reuses_and_does_not_release_supplied_lease(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_deploy_workflow
    from toolkit.core.deploy.operation_lease import LeaseBusyError, OperationLease

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    held = OperationLease.acquire(root, "secret-rotation")

    try:
        with (
            patch("toolkit.core.deploy.deploy_workflow.run_preflight", return_value=[]),
            patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=False),
        ):
            result = asyncio.run(
                run_deploy_workflow(
                    root,
                    cfg,
                    on_log=lambda _message: None,
                    on_step=lambda _step, _status: None,
                    operation_lease=held,
                )
            )

        assert result.success is False
        with pytest.raises(LeaseBusyError):
            OperationLease.acquire(root, "concurrent")
    finally:
        held.release()

    replacement = OperationLease.acquire(root, "deploy")
    replacement.release()


def test_deploy_workflow_consumes_cancel_request_after_preflight(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_deploy_workflow
    from toolkit.core.deploy.operation_lease import OperationLease

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)

    def cancel_during_preflight(*_args, **_kwargs):
        OperationLease.request_active_cancel(root)
        return []

    with (
        patch("toolkit.core.deploy.deploy_workflow.run_preflight", side_effect=cancel_during_preflight),
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=True),
        patch("toolkit.core.deploy.deploy_workflow._run_pre_deploy_dump") as dump,
    ):
        result = asyncio.run(
            run_deploy_workflow(
                root,
                cfg,
                on_log=lambda _m: None,
                on_step=lambda _s, _st: None,
            )
        )

    assert result.success is False
    assert "cancel" in result.message.lower()
    dump.assert_not_called()
    replacement = OperationLease.acquire(root, "deploy")
    replacement.release()


def test_recover_workflow_consumes_cancel_request_after_preflight(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_recover_workflow
    from toolkit.core.deploy.operation_lease import OperationLease

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    playbook = root / "automation" / "ansible" / "playbooks" / "deploy-recover.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("---\n- hosts: all\n  tasks: []\n")

    def cancel_during_preflight(*_args, **_kwargs):
        OperationLease.request_active_cancel(root)
        return []

    with (
        patch("toolkit.core.deploy.deploy_workflow.run_preflight", side_effect=cancel_during_preflight),
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=True),
        patch("asyncio.create_subprocess_exec") as subprocess_exec,
    ):
        result = asyncio.run(
            run_recover_workflow(
                root,
                cfg,
                on_log=lambda _m: None,
                on_step=lambda _s, _st: None,
            )
        )

    assert result.success is False
    assert "cancel" in result.message.lower()
    subprocess_exec.assert_not_called()
    replacement = OperationLease.acquire(root, "recover")
    replacement.release()


def test_recover_workflow_consumes_cancel_request_during_final_verify(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_recover_workflow
    from toolkit.core.deploy.operation_lease import OperationLease
    from toolkit.core.ops.hook_verify import HookVerifyResult
    from toolkit.core.ops.preflight import PreflightItem
    from toolkit.core.ops.verify import VerifyResult

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    playbook = root / "automation" / "ansible" / "playbooks" / "deploy-recover.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("---\n- hosts: all\n  tasks: []\n")

    async def fake_exec(*_args, **_kwargs):
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(side_effect=[b""])
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        return proc

    verify_results = {vm: VerifyResult(vm=vm, docker_ok=True, compose_ok=True) for vm in cfg.enabled_nodes}

    def cancel_during_verify(*_args, **_kwargs):
        OperationLease.request_active_cancel(root)
        return verify_results

    with (
        patch(
            "toolkit.core.deploy.deploy_workflow.run_preflight",
            return_value=[PreflightItem(id="ssh", label="SSH", ok=True)],
        ),
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=True),
        patch("toolkit.core.deploy.deploy_workflow.resolve_tool", return_value="/usr/bin/ansible-playbook"),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("toolkit.core.deploy.deploy_workflow.run_post_start_hooks_remote", return_value=({}, True)),
        patch("toolkit.core.ops.hook_verify.verify_hooks", return_value=HookVerifyResult()),
        patch("toolkit.core.deploy.deploy_workflow.verify_remote", side_effect=cancel_during_verify),
        patch("toolkit.core.deploy.deploy_workflow.verify_all", side_effect=cancel_during_verify),
        patch("toolkit.core.secrets.secrets.load_secrets_plaintext", return_value={}),
    ):
        result = asyncio.run(
            run_recover_workflow(
                root,
                cfg,
                on_log=lambda _m: None,
                on_step=lambda _s, _st: None,
            )
        )

    assert result.success is False
    assert "cancel" in result.message.lower()
    replacement = OperationLease.acquire(root, "recover")
    replacement.release()


def test_clean_wipe_consumes_cancel_request_after_preflight(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_clean_wipe_workflow
    from toolkit.core.deploy.operation_lease import OperationLease

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)

    def cancel_during_preflight(*_args, **_kwargs):
        OperationLease.request_active_cancel(root)
        return []

    with (
        patch("toolkit.core.deploy.deploy_workflow.run_preflight", side_effect=cancel_during_preflight),
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=True),
        patch("toolkit.core.deploy.deploy_workflow.sync_from_repo_root") as sync,
    ):
        result = asyncio.run(
            run_clean_wipe_workflow(
                root,
                cfg,
                on_log=lambda _m: None,
                on_step=lambda _s, _st: None,
            )
        )

    assert result.success is False
    assert "cancel" in result.message.lower()
    assert result.step_status["preflight"] == "ok"
    sync.assert_not_called()
    replacement = OperationLease.acquire(root, "clean-wipe")
    replacement.release()


def test_clean_wipe_releases_lease_when_preflight_raises(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_clean_wipe_workflow
    from toolkit.core.deploy.operation_lease import OperationLease

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)

    with patch(
        "toolkit.core.deploy.deploy_workflow.run_preflight",
        side_effect=RuntimeError("preflight crashed"),
    ):
        with pytest.raises(RuntimeError, match="preflight crashed"):
            asyncio.run(
                run_clean_wipe_workflow(
                    root,
                    cfg,
                    on_log=lambda _m: None,
                    on_step=lambda _s, _st: None,
                )
            )

    replacement = OperationLease.acquire(root, "clean-wipe")
    replacement.release()


def test_deploy_workflow_has_no_implicit_destroy_mode():
    import inspect

    from toolkit.core.deploy.deploy_workflow import run_deploy_workflow

    assert "destroy_first" not in inspect.signature(run_deploy_workflow).parameters


def test_clean_wipe_requires_checkpoint_before_sync_or_destruction(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_clean_wipe_workflow
    from toolkit.core.deploy.destructive_guard import RecoveryCheckpointRequiredError

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)

    with (
        patch("toolkit.core.deploy.deploy_workflow.run_preflight", return_value=[]),
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=True),
        patch(
            "toolkit.core.deploy.destructive_guard.require_verified_checkpoint",
            side_effect=RecoveryCheckpointRequiredError("checkpoint required"),
        ),
        patch("toolkit.core.deploy.deploy_workflow.sync_from_repo_root") as sync,
        patch("toolkit.core.infra.infra_destroy.destroy_infrastructure") as destroy,
    ):
        result = asyncio.run(
            run_clean_wipe_workflow(
                root,
                cfg,
                on_log=lambda _m: None,
                on_step=lambda _s, _st: None,
                wipe_zfs=True,
            )
        )

    assert result.success is False
    assert "checkpoint required" in result.message
    sync.assert_not_called()
    destroy.assert_not_called()


def test_clean_wipe_never_wipes_zfs_when_lxc_destroy_fails(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_clean_wipe_workflow

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)

    with (
        patch("toolkit.core.deploy.deploy_workflow.run_preflight", return_value=[]),
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=True),
        patch("toolkit.core.deploy.destructive_guard.require_verified_checkpoint"),
        patch("toolkit.core.deploy.deploy_workflow.sync_from_repo_root"),
        patch("toolkit.core.infra.infra_destroy.destroy_infrastructure", return_value=1),
        patch("asyncio.create_subprocess_exec") as subprocess_exec,
    ):
        result = asyncio.run(
            run_clean_wipe_workflow(
                root,
                cfg,
                on_log=lambda _m: None,
                on_step=lambda _s, _st: None,
                wipe_zfs=True,
            )
        )

    assert result.success is False
    assert "destroy failed" in result.message.lower()
    subprocess_exec.assert_not_called()


def test_clean_wipe_workflow_cannot_replace_active_deploy_lease(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_clean_wipe_workflow
    from toolkit.core.deploy.operation_lease import OperationLease

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    held = OperationLease.acquire(root, "deploy")
    try:
        result = asyncio.run(
            run_clean_wipe_workflow(
                root,
                cfg,
                on_log=lambda _m: None,
                on_step=lambda _s, _st: None,
            )
        )
        assert result.success is False
        assert ".deploy.lock" in result.message
        assert held.snapshot.operation == "deploy"
    finally:
        held.release()


def test_run_recover_workflow_releases_lock_on_verify_exception(tmp_path: Path):
    """Recover must release the deploy lock when verify_remote raises.

    Regression: the recover workflow acquired `.deploy.lock`, then released
    it only on the happy path. An exception from `verify_remote` (e.g. an
    Ansible rc=127 wrapper error, or a transport crash) propagated with the
    lock still held — leaving a stale `.deploy.lock` that blocked every
    subsequent `deploy all` / `deploy recover`. The live breakage on Jun 23
    surfaced as exactly such a stale lock (`pid=218719`, process long dead).
    """
    from toolkit.core.deploy.deploy_workflow import run_recover_workflow
    from toolkit.core.deploy.operation_lease import OperationLease
    from toolkit.core.ops.hook_verify import HookVerifyResult
    from toolkit.core.ops.preflight import PreflightItem

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    playbook = root / "automation" / "ansible" / "playbooks" / "deploy-recover.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("---\n- hosts: all\n  tasks: []\n")

    async def fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(side_effect=[b"PLAY [infra-01]\n", b""])
        proc.returncode = 0
        proc.wait = AsyncMock(return_value=0)
        return proc

    with (
        patch(
            "toolkit.core.deploy.deploy_workflow.run_preflight",
            return_value=[PreflightItem(id="ssh", label="SSH", ok=True)],
        ),
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=True),
        patch("toolkit.core.deploy.deploy_workflow.resolve_tool", return_value="/usr/bin/ansible-playbook"),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch(
            "toolkit.core.deploy.deploy_workflow.run_post_start_hooks_remote",
            return_value=({}, True),
        ),
        patch("toolkit.core.ops.hook_verify.verify_hooks", return_value=HookVerifyResult()),
        # The failure surface: verify_remote raises mid-deploy (transport crash).
        patch(
            "toolkit.core.deploy.deploy_workflow.verify_remote",
            side_effect=RuntimeError("simulated transport crash"),
        ),
        patch("toolkit.core.secrets.secrets.load_secrets_plaintext", return_value={}),
    ):
        with pytest.raises(RuntimeError, match="simulated transport crash"):
            asyncio.run(
                run_recover_workflow(
                    root,
                    cfg,
                    on_log=lambda _m: None,
                    on_step=lambda _s, _st: None,
                )
            )

    # The lease MUST be released. Probe by acquiring it fresh — if the recover
    # workflow leaked ownership, this would raise LeaseBusyError.
    probe = OperationLease.acquire(root, "deploy")
    try:
        assert probe.snapshot.operation == "deploy"
    finally:
        probe.release()


def test_run_recover_workflow_stops_after_failed_playbook(tmp_path: Path):
    from toolkit.core.deploy.deploy_workflow import run_recover_workflow
    from toolkit.core.ops.preflight import PreflightItem

    root = tmp_path / "homelab"
    root.mkdir()
    cfg = _setup(root)
    playbook = root / "automation" / "ansible" / "playbooks" / "deploy-recover.yml"
    playbook.parent.mkdir(parents=True, exist_ok=True)
    playbook.write_text("---\n- hosts: all\n  tasks: []\n")

    async def fake_exec(*args, **kwargs):
        proc = MagicMock()
        proc.stdout = MagicMock()
        proc.stdout.readline = AsyncMock(side_effect=[b"fatal: [infra-01]: FAILED!\n", b""])
        proc.returncode = 2
        proc.wait = AsyncMock(return_value=2)
        return proc

    with (
        patch(
            "toolkit.core.deploy.deploy_workflow.run_preflight",
            return_value=[PreflightItem(id="ssh", label="SSH", ok=True)],
        ),
        patch("toolkit.core.deploy.deploy_workflow.preflight_passed", return_value=True),
        patch("toolkit.core.deploy.deploy_workflow.resolve_tool", return_value="/usr/bin/ansible-playbook"),
        patch("asyncio.create_subprocess_exec", side_effect=fake_exec),
        patch("toolkit.core.deploy.deploy_workflow.run_post_start_hooks_remote") as hooks,
        patch("toolkit.core.deploy.deploy_workflow.verify_remote") as verify,
    ):
        result = asyncio.run(run_recover_workflow(root, cfg, on_log=lambda _m: None, on_step=lambda _s, _st: None))

    assert result.success is False
    assert "playbook failed" in result.message.lower()
    assert result.step_status["deploy_infra"] == "fail"
    assert result.step_status["deploy_media"] == "skip"
    assert result.step_status["deploy_apps"] == "skip"
    hooks.assert_not_called()
    verify.assert_not_called()
