from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from toolkit.core.deploy.staggered_compose import StaggeredComposeRunner


def test_staggered_startup_does_not_rebuild_reconciled_images(tmp_path: Path) -> None:
    generated = tmp_path / "generated" / "media"
    generated.mkdir(parents=True)
    (generated / ".env").write_text("COMPOSE_PROFILES=media\nPRIVATE_IP=127.0.0.1\n")
    (generated / "compose.yaml").write_text(
        "services:\n"
        "  custom-service:\n"
        "    image: ghcr.io/example/custom-service:sha-123\n"
        "    build: ./custom-service\n"
        "    profiles: [media]\n"
    )
    (tmp_path / "config.yaml").write_text("domain: test.local\n")
    commands: list[list[str]] = []

    def run(command, **_kwargs):
        commands.append(list(command))
        return MagicMock(returncode=0, stdout="", stderr="")

    runner = StaggeredComposeRunner(root=tmp_path, node="media", subprocess_run=run)
    with (
        patch.object(runner, "load_gate_strict"),
        patch.object(runner, "_ensure_compose_artifacts"),
        patch.object(runner, "wait_for_local_ip"),
        patch.object(runner, "_run_node"),
        patch("toolkit.core.ops.maintenance.maybe_prune_docker_before_deploy", return_value=[]),
    ):
        assert runner.run() == 0

    assert not any("build" in command for command in commands)
