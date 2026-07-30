from toolkit.core.config.config import Config
from toolkit.core.deploy.deploy_log import (
    DeployProgressSnapshot,
    format_progress_line,
    parse_deploy_stream_line,
    should_echo_deploy_line,
)
from toolkit.core.deploy.deploy_workflow import _init_recover_step_status, workflow_progress_percent


def test_parse_play_and_task_updates_vm_and_task():
    snap = DeployProgressSnapshot()
    assert parse_deploy_stream_line(
        "PLAY [compute-a] *************************************************",
        snap,
        {"compute-a": "apps"},
    )
    assert snap.node == "apps"
    assert parse_deploy_stream_line("TASK [Wait for staggered compose to finish] *****************", snap)
    assert snap.ansible_task == "Wait for staggered compose to finish"
    line = format_progress_line(snap)
    assert "apps" in line
    assert "Wait for staggered compose" in line


def test_parse_compose_wave():
    snap = DeployProgressSnapshot(node="apps")
    assert parse_deploy_stream_line("Waiting for wave 'cloud' (4 services, timeout 600s)...", snap)
    assert snap.compose_wave == "cloud"
    assert "wave=cloud" in format_progress_line(snap)


def test_parse_fatal_sets_detail():
    snap = DeployProgressSnapshot(node="apps")
    assert parse_deploy_stream_line("fatal: [apps-01]: FAILED! => boom", snap)
    assert "FAILED" in snap.detail


def test_operator_stream_hides_raw_ansible_results_but_keeps_progress() -> None:
    assert not should_echo_deploy_line("ok: [media-01] => (item={'path': 'large metadata'})")
    assert not should_echo_deploy_line("skipping: [media-01] => (item=(censored due to no_log))")
    assert not should_echo_deploy_line("TASK [Sync artifacts] ****************")
    assert should_echo_deploy_line("▶ Deploy media LXC · task=Sync artifacts")
    assert should_echo_deploy_line("  [media] → bazarr: applying service setup")
    assert should_echo_deploy_line("  ✗ bazarr.providers: none configured")
    assert should_echo_deploy_line("Summary: 278/278 checks passed")


def test_workflow_progress_advances_when_running_step_is_skipped() -> None:
    cfg = Config()
    running = workflow_progress_percent({"infra": "running", "deploy_apps": "pending"}, cfg)
    skipped = workflow_progress_percent({"infra": "skip", "deploy_apps": "pending"}, cfg)

    assert skipped >= running


def test_workflow_progress_uses_only_active_target_steps() -> None:
    cfg = Config()
    active_steps = {
        "preflight": "ok",
        "infra": "skip",
        "deploy_apps": "ok",
        "verify": "ok",
    }

    assert workflow_progress_percent(active_steps, cfg) == 100


def test_recovery_progress_contains_only_executed_steps() -> None:
    cfg = Config()
    steps = _init_recover_step_status(cfg, vm="infra")

    assert set(steps) == {
        "preflight",
        "deploy_infra",
        "hooks",
        "hook_verify",
        "verify",
    }
    completed = dict.fromkeys(steps, "ok")
    assert workflow_progress_percent(completed, cfg) == 100
