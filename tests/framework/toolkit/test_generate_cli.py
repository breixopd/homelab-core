from __future__ import annotations

from click.testing import CliRunner
from toolkit.cli.generate_cmd import generate
from toolkit.core.config.config import Config


def test_generate_reports_compose_models_after_artifact_generation(tmp_path, monkeypatch) -> None:
    stacks = tmp_path / "stacks"
    stacks.mkdir()
    (stacks / "platform.yaml").write_text("networks: {}\n")
    order: list[str] = []
    compose = tmp_path / "docker-compose.yml"
    compose.write_text("services: {}\n")
    reconcile = tmp_path / ".homelab-state" / "last-reconcile.json"
    reconcile.parent.mkdir()
    reconcile.write_text("{}\n")

    def fake_generate(_root):
        order.append("generate")
        for role in ("infra", "media", "apps"):
            model = tmp_path / "generated" / role / "compose.yaml"
            model.parent.mkdir(parents=True, exist_ok=True)
            model.write_text("services: {}\n")
        return {}

    monkeypatch.setattr("toolkit.cli.generate_cmd.generate_all", fake_generate)

    def fake_generate_configs(_cfg, _root, *, on_progress=None):
        if on_progress is not None:
            on_progress(1, 1, "authelia")
        return []

    monkeypatch.setattr("toolkit.cli.generate_cmd.generate_configs", fake_generate_configs)
    monkeypatch.setattr("toolkit.cli.generate_cmd.load_config", lambda _path: Config())
    monkeypatch.setattr(
        "toolkit.core.registry.reconcile.write_last_reconcile",
        lambda *_args, **_kwargs: reconcile,
    )

    result = CliRunner().invoke(generate, ["--skip-validate"], obj={"root": tmp_path})

    assert result.exit_code == 0, result.output
    assert order == ["generate"]
    assert "Generating runtime environment and Compose models..." in result.output
    assert "Generating service-owned artifacts..." in result.output
    assert "artifact [1/1]: authelia" in result.output
    assert "compose: docker-compose.yml" in result.output
    assert "compose[media]: generated/media/compose.yaml" in result.output
