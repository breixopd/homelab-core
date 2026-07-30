from __future__ import annotations

import gzip
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import yaml
from toolkit.core.config.config import Config
from toolkit.core.manifest.catalog import clear_catalog_cache
from toolkit.core.ops.logical_backups import logical_dump_names, prepare_logical_dumps


def test_logical_dump_names_follow_role_and_enabled_services() -> None:
    cfg = Config(services={"email": False, "cloud": True})

    assert logical_dump_names(cfg, "infra") == (
        "komodo-mongo.archive.gz",
        "postgres.sql.gz",
        "headscale.sqlite.gz",
    )
    assert logical_dump_names(cfg, "media") == ()
    assert logical_dump_names(cfg, "apps") == (
        "dev-postgres.sql.gz",
        "fmd-server.sqlite.gz",
        "immich-postgres.sql.gz",
    )


def test_custom_database_plugin_contributes_backup_without_core_changes(tmp_path: Path) -> None:
    service = tmp_path / "toolkit" / "services" / "custom-db"
    service.mkdir(parents=True)
    manifest = {
        "name": "custom-db",
        "label": "Custom DB",
        "description": "User-defined database plugin",
        "icon": "database",
        "category": "cloud",
        "placement": "apps",
        "priority": 50,
        "host_sources": {"CUSTOM_DB_DATA_SOURCE": {"path": "data/custom-db"}},
        "stateful": True,
        "data_specs": [
            {
                "name": "custom-data",
                "source_env": "CUSTOM_DB_DATA_SOURCE",
                "target": "/var/lib/custom",
                "size_estimate_gb": 1,
                "snapshot": False,
            }
        ],
        "backup_exports": [
            {
                "artifact": "custom-db.sql.gz",
                "strategy": "container",
                "command": ["custom-dump", "--all"],
            }
        ],
    }
    compose = {
        "services": {
            "custom-db": {
                "image": "example/custom-db:1@sha256:" + "a" * 64,
                "volumes": ["${CUSTOM_DB_DATA_SOURCE:-./data/custom-db}:/var/lib/custom"],
                "logging": {
                    "driver": "json-file",
                    "options": {"max-size": "10m", "max-file": "3"},
                },
            }
        }
    }
    (service / "service.yaml").write_text(yaml.safe_dump(manifest, sort_keys=False), encoding="utf-8")
    (service / "compose.yaml").write_text(yaml.safe_dump(compose, sort_keys=False), encoding="utf-8")
    clear_catalog_cache()

    assert logical_dump_names(Config(services={"cloud": True}), "apps", tmp_path) == ("custom-db.sql.gz",)


def test_infra_logical_dumps_are_atomic_and_compressed(tmp_path: Path, monkeypatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        kwargs["stdout"].write(b"compressed-dump")
        return SimpleNamespace(returncode=0, stderr=b"")

    def fake_sqlite(_spec, _root, _role, pending):
        with gzip.open(pending, "wb") as stream:
            stream.write(b"sqlite-export")
        return ""

    monkeypatch.setattr("toolkit.core.ops.logical_backups.subprocess.run", fake_run)
    monkeypatch.setattr("toolkit.core.ops.logical_backups._sqlite_export", fake_sqlite)

    result = prepare_logical_dumps(Config(), tmp_path, "infra")

    assert result.ok
    assert {path.name for path in result.artifacts} == {
        "postgres.sql.gz",
        "komodo-mongo.archive.gz",
        "roundcube.sqlite.gz",
        "headscale.sqlite.gz",
    }
    with_contents = {path.name: gzip.decompress(path.read_bytes()) for path in result.artifacts}
    assert set(with_contents.values()) == {b"compressed-dump", b"sqlite-export"}
    assert any(command[-3:] == ["pg_dumpall", "-U", "admin"] for command in commands)
    assert any("mongodump --archive" in command[-1] for command in commands)
    assert not list((tmp_path / "data" / "kopia" / "dumps" / "infra").glob("*.tmp"))


def test_apps_logical_dumps_follow_enabled_service_groups(tmp_path: Path, monkeypatch) -> None:
    commands: list[list[str]] = []

    def fake_run(command, **kwargs):
        commands.append(command)
        kwargs["stdout"].write(b"dump")
        return SimpleNamespace(returncode=0, stderr=b"")

    def fake_sqlite(_spec, _root, _role, pending):
        with gzip.open(pending, "wb") as stream:
            stream.write(b"sqlite-export")
        return ""

    monkeypatch.setattr("toolkit.core.ops.logical_backups.subprocess.run", fake_run)
    monkeypatch.setattr("toolkit.core.ops.logical_backups._sqlite_export", fake_sqlite)
    cfg = Config(services={"cloud": True})

    result = prepare_logical_dumps(cfg, tmp_path, "apps")

    assert result.ok
    assert [path.name for path in result.artifacts] == [
        "dev-postgres.sql.gz",
        "fmd-server.sqlite.gz",
        "immich-postgres.sql.gz",
    ]
    assert any(command[-3:] == ["pg_dumpall", "-U", "postgres"] for command in commands)
    assert any(command[-3:] == ["pg_dumpall", "-U", "dev"] for command in commands)


def test_sqlite_export_uses_online_backup_and_compresses_a_consistent_database(tmp_path: Path, monkeypatch) -> None:
    source = tmp_path / "data" / "headscale" / "db.sqlite"
    source.parent.mkdir(parents=True)
    with sqlite3.connect(source) as database:
        database.execute("CREATE TABLE nodes (id INTEGER PRIMARY KEY, name TEXT NOT NULL)")
        database.execute("INSERT INTO nodes (name) VALUES ('phone')")
    environment = tmp_path / "generated" / "infra" / ".env"
    environment.parent.mkdir(parents=True)
    environment.write_text(f"HEADSCALE_DATA_SOURCE={source.parent}\n", encoding="utf-8")

    def fake_run(command, **kwargs):
        kwargs["stdout"].write(b"container-export")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("toolkit.core.ops.logical_backups.subprocess.run", fake_run)

    result = prepare_logical_dumps(Config(services={"email": False}), tmp_path, "infra")

    assert result.ok
    artifact = next(path for path in result.artifacts if path.name == "headscale.sqlite.gz")
    restored = tmp_path / "restored.sqlite"
    with gzip.open(artifact, "rb") as source_stream, restored.open("wb") as destination:
        destination.write(source_stream.read())
    with sqlite3.connect(restored) as database:
        assert database.execute("SELECT name FROM nodes").fetchall() == [("phone",)]


def test_sqlite_export_rejects_database_symlink_outside_declared_storage(tmp_path: Path, monkeypatch) -> None:
    outside = tmp_path / "outside.sqlite"
    with sqlite3.connect(outside) as database:
        database.execute("CREATE TABLE secrets (value TEXT)")
    source = tmp_path / "data" / "headscale"
    source.mkdir(parents=True)
    source.joinpath("db.sqlite").symlink_to(outside)
    environment = tmp_path / "generated" / "infra" / ".env"
    environment.parent.mkdir(parents=True)
    environment.write_text(f"HEADSCALE_DATA_SOURCE={source}\n", encoding="utf-8")

    def fake_run(_command, **kwargs):
        kwargs["stdout"].write(b"container-export")
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("toolkit.core.ops.logical_backups.subprocess.run", fake_run)

    result = prepare_logical_dumps(Config(services={"email": False}), tmp_path, "infra")

    assert not result.ok
    assert "outside its declared storage asset" in result.errors[0]


def test_failed_logical_dump_removes_partial_artifact(tmp_path: Path, monkeypatch) -> None:
    def fake_run(_command, **kwargs):
        kwargs["stdout"].write(b"partial")
        return SimpleNamespace(returncode=2, stderr=b"database unavailable")

    monkeypatch.setattr("toolkit.core.ops.logical_backups.subprocess.run", fake_run)

    result = prepare_logical_dumps(Config(), tmp_path, "infra")

    assert not result.ok
    assert "database unavailable" in result.errors[0]
    assert not list((tmp_path / "data" / "kopia" / "dumps").rglob("*.*"))


def test_successful_container_export_with_empty_payload_is_rejected(tmp_path: Path, monkeypatch) -> None:
    def fake_run(_command, **_kwargs):
        return SimpleNamespace(returncode=0, stderr=b"")

    monkeypatch.setattr("toolkit.core.ops.logical_backups.subprocess.run", fake_run)

    result = prepare_logical_dumps(Config(services={"email": False}), tmp_path, "infra")

    assert not result.ok
    assert "empty database export" in result.errors[0]
    assert not list((tmp_path / "data" / "kopia" / "dumps").rglob("*.*"))
