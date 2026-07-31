from types import SimpleNamespace

import toolkit.cli.mesh_cmd as mesh_cmd
from click.testing import CliRunner
from toolkit.cli.mesh_cmd import _extract_registration_key


def test_extract_registration_key_from_current_tailscale_path_url() -> None:
    key = "hskey-authreq-example_123"

    assert _extract_registration_key(f"https://vpn.example.com/register/{key}") == key


def test_extract_registration_key_from_legacy_query_url() -> None:
    key = "hskey-authreq-example_456"

    assert _extract_registration_key(f"https://vpn.example.com/register?key={key}") == key


def test_extract_registration_key_returns_empty_for_unrelated_output() -> None:
    assert _extract_registration_key("tailscale is already connected") == ""


def test_fleet_dry_run_redacts_full_preauth_key(monkeypatch) -> None:
    cfg = SimpleNamespace(domain="example.com", email="owner@example.com", fleet=SimpleNamespace(headscale_tags=[]))
    monkeypatch.setattr(mesh_cmd, "load_root_config", lambda _ctx: ("/tmp", cfg))
    monkeypatch.setattr(
        mesh_cmd,
        "headscale_preauth_key_for_deploy",
        lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("dry-run must not create a key")),
    )

    result = CliRunner().invoke(mesh_cmd.mesh, ["join", "--fleet", "--dry-run"])

    assert result.exit_code == 0
    assert "--auth-key=<REDACTED>" in result.output


def test_personal_join_never_echoes_registration_key(monkeypatch) -> None:
    token = "hskey-personal-distinctive-token"
    cfg = SimpleNamespace(domain="example.com", email="owner@example.com", fleet=SimpleNamespace(headscale_tags=[]))
    monkeypatch.setattr(mesh_cmd, "load_root_config", lambda _ctx: ("/tmp", cfg))
    monkeypatch.setattr(mesh_cmd, "personal_mesh_up_args", lambda *_a, **_k: ["tailscale", "up"])
    monkeypatch.setattr(
        mesh_cmd.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=1, stdout=f"https://vpn.example.com/register/{token}", stderr=""),
    )
    monkeypatch.setattr(mesh_cmd, "approve_mesh_registration", lambda *_a, **_k: [f"approval pending: {token}"])

    result = CliRunner().invoke(mesh_cmd.mesh, ["join"])

    assert result.exit_code == 1
    assert token not in result.output
    assert "mesh approve --key <KEY from page>" in result.output


def test_personal_join_error_without_registration_key_remains_readable(monkeypatch) -> None:
    cfg = SimpleNamespace(domain="example.com", email="owner@example.com", fleet=SimpleNamespace(headscale_tags=[]))
    monkeypatch.setattr(mesh_cmd, "load_root_config", lambda _ctx: ("/tmp", cfg))
    monkeypatch.setattr(mesh_cmd, "personal_mesh_up_args", lambda *_a, **_k: ["tailscale", "up"])
    monkeypatch.setattr(
        mesh_cmd.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=1, stdout="", stderr="daemon unavailable"),
    )

    result = CliRunner().invoke(mesh_cmd.mesh, ["join"])

    assert result.exit_code == 1
    assert "daemon unavailable" in result.output
    assert result.output.count("<REDACTED>") == 0


def test_personal_join_timeout_returns_safe_guidance(monkeypatch) -> None:
    cfg = SimpleNamespace(domain="example.com", email="owner@example.com", fleet=SimpleNamespace(headscale_tags=[]))
    monkeypatch.setattr(mesh_cmd, "load_root_config", lambda _ctx: ("/tmp", cfg))
    monkeypatch.setattr(mesh_cmd, "personal_mesh_up_args", lambda *_a, **_k: ["tailscale", "up"])
    monkeypatch.setattr(
        mesh_cmd.subprocess,
        "run",
        lambda command, **_kwargs: (_ for _ in ()).throw(mesh_cmd.subprocess.TimeoutExpired(command, 120)),
    )

    result = CliRunner().invoke(mesh_cmd.mesh, ["join"])

    assert result.exit_code == 1
    assert result.exception is not None
    assert "timed out" in result.output
    assert "cmd=" not in result.output


def test_personal_join_requires_verified_headscale_control_state(monkeypatch) -> None:
    cfg = SimpleNamespace(domain="example.com", email="owner@example.com", fleet=SimpleNamespace(headscale_tags=[]))
    monkeypatch.setattr(mesh_cmd, "load_root_config", lambda _ctx: ("/tmp", cfg))
    monkeypatch.setattr(mesh_cmd, "personal_mesh_up_args", lambda *_a, **_k: ["tailscale", "up"])
    monkeypatch.setattr(
        mesh_cmd.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(mesh_cmd, "headscale_control_state_verified", lambda _url: False)

    result = CliRunner().invoke(mesh_cmd.mesh, ["join"])

    assert result.exit_code == 1
    assert "control state was not verified" in result.output
    assert "Mesh join complete" not in result.output


def test_personal_join_reports_success_after_verified_headscale_control_state(monkeypatch) -> None:
    cfg = SimpleNamespace(domain="example.com", email="owner@example.com", fleet=SimpleNamespace(headscale_tags=[]))
    monkeypatch.setattr(mesh_cmd, "load_root_config", lambda _ctx: ("/tmp", cfg))
    monkeypatch.setattr(mesh_cmd, "personal_mesh_up_args", lambda *_a, **_k: ["tailscale", "up"])
    monkeypatch.setattr(
        mesh_cmd.subprocess,
        "run",
        lambda *_a, **_k: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    monkeypatch.setattr(
        mesh_cmd,
        "headscale_control_state_verified",
        lambda url: url == "https://vpn.example.com",
    )

    result = CliRunner().invoke(mesh_cmd.mesh, ["join"])

    assert result.exit_code == 0
    assert "Mesh join complete" in result.output


def test_fleet_join_failure_exits_nonzero_without_echoing_key(monkeypatch) -> None:
    secret = "hskey-fleet-never-echo"
    cfg = SimpleNamespace(domain="example.com", email="owner@example.com", fleet=SimpleNamespace(headscale_tags=[]))
    monkeypatch.setattr(mesh_cmd, "load_root_config", lambda _ctx: ("/tmp", cfg))
    monkeypatch.setattr(mesh_cmd, "headscale_preauth_key_for_deploy", lambda *_a, **_k: secret)
    monkeypatch.setattr(
        mesh_cmd,
        "ensure_controller_mesh_joined",
        lambda *_a, **_k: ["Headscale: tailscale up timed out; mesh state was not verified"],
    )

    result = CliRunner().invoke(mesh_cmd.mesh, ["join", "--fleet"])

    assert result.exit_code == 1
    assert "timed out" in result.output
    assert secret not in result.output


def test_recovery_guidance_uses_existing_join_command() -> None:
    assert mesh_cmd._safe_mesh_log("run `homelab-toolkit mesh join-cmd --fleet`") == (
        "run `homelab-toolkit mesh join --fleet`"
    )
