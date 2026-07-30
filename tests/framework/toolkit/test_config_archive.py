from __future__ import annotations

import io
import tarfile
from pathlib import Path

from click.testing import CliRunner
from toolkit.cli import main
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path


def _archive(path: Path, files: dict[str, bytes]) -> None:
    with tarfile.open(path, "w:gz") as stream:
        for name, content in files.items():
            member = tarfile.TarInfo(name)
            member.size = len(content)
            stream.addfile(member, io.BytesIO(content))


def test_config_export_uses_canonical_restore_paths(tmp_path: Path) -> None:
    save_config(Config(domain="example.com", email="owner@example.com"), config_path(tmp_path))
    (tmp_path / "secrets.enc.yaml").write_text("sops:\n  version: 3.13.2\n", encoding="utf-8")
    (tmp_path / ".sops.yaml").write_text("creation_rules: []\n", encoding="utf-8")
    (tmp_path / "stacks").mkdir()
    (tmp_path / "stacks" / "platform.yaml").write_text("services: {}\n", encoding="utf-8")
    output = tmp_path / "backup.tar.gz"

    result = CliRunner().invoke(
        main,
        ["--root", str(tmp_path), "config", "export", "--output", str(output)],
    )

    assert result.exit_code == 0, (result.output, result.exception)
    with tarfile.open(output, "r:gz") as stream:
        names = set(stream.getnames())
    assert {"config.yaml", "secrets.enc.yaml", ".sops.yaml", "stacks/platform.yaml"}.issubset(names)
    assert not any(name.startswith("config/") for name in names)


def test_config_import_rejects_path_traversal_without_partial_writes(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    archive = tmp_path / "malicious.tar.gz"
    _archive(
        archive,
        {
            "config.yaml": b"domain: valid.example\n",
            "../outside.txt": b"escaped",
        },
    )

    result = CliRunner().invoke(
        main,
        ["--root", str(root), "config", "import", "--input", str(archive), "--yes"],
    )

    assert result.exit_code != 0
    assert "unsafe archive member" in result.output.lower()
    assert not outside.exists()
    assert not (root / "config.yaml").exists()


def test_config_import_refuses_existing_symlink_target(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text("unchanged\n", encoding="utf-8")
    (root / "config.yaml").symlink_to(outside)
    archive = tmp_path / "backup.tar.gz"
    _archive(archive, {"config.yaml": b"domain: replacement.example\n"})

    result = CliRunner().invoke(
        main,
        ["--root", str(root), "config", "import", "--input", str(archive), "--yes"],
    )

    assert result.exit_code != 0
    assert "symbolic link" in result.output.lower()
    assert outside.read_text(encoding="utf-8") == "unchanged\n"
    assert (root / "config.yaml").is_symlink()


def test_config_import_validates_every_file_before_writing(tmp_path: Path) -> None:
    root = tmp_path / "root"
    root.mkdir()
    archive = tmp_path / "invalid.tar.gz"
    _archive(
        archive,
        {
            "config.yaml": b"domain: valid.example\n",
            "stacks/platform.yaml": b"services: [\n",
        },
    )

    result = CliRunner().invoke(
        main,
        ["--root", str(root), "config", "import", "--input", str(archive), "--yes"],
    )

    assert result.exit_code != 0
    assert "invalid yaml" in result.output.lower()
    assert not (root / "config.yaml").exists()
    assert not (root / "stacks" / "platform.yaml").exists()


def test_ci_installs_checksum_pinned_secret_tools_before_generation() -> None:
    root = Path(__file__).resolve().parents[3]
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    installer = root / "scripts" / "install-ci-secret-tools.sh"

    assert installer.is_file()
    source = installer.read_text(encoding="utf-8")
    assert 'SOPS_VERSION="3.13.2"' in source
    assert 'AGE_VERSION="1.3.1"' in source
    assert "sha256sum --check" in source
    assert workflow.count("./scripts/install-ci-secret-tools.sh") == 3
    assert "homelab-toolkit --root . sync" not in workflow
    assert "homelab-toolkit --root . generate" in workflow
