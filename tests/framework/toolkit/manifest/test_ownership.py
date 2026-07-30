from __future__ import annotations

from dataclasses import asdict

import pytest
from toolkit.core.manifest.ownership import (
    OwnedPath,
    OwnedSecret,
    OwnershipLedger,
    commit_ownership_ledger,
    load_ownership_ledger,
    ownership_ledger_path,
    ownership_tombstones,
    prune_local_ownership_tombstones,
)
from toolkit.core.state.files import atomic_write_json


def _write_ledger(root, ledger: OwnershipLedger) -> None:
    atomic_write_json(
        ownership_ledger_path(root),
        {
            "version": 1,
            "generated": [asdict(item) for item in ledger.generated],
            "config": [asdict(item) for item in ledger.config],
            "machines": list(ledger.machines),
            "secrets": [asdict(item) for item in ledger.secrets],
        },
    )


def test_ownership_tombstones_only_include_previously_verified_removed_state(tmp_path) -> None:
    previous = OwnershipLedger(
        generated=(OwnedPath("generated/removed.env", "removed", "apps"),),
        config=(OwnedPath("config/removed", "removed", "apps"),),
        machines=("apps", "retired"),
        secrets=(OwnedSecret("REMOVED_PASSWORD", "service:removed"), OwnedSecret("USER_TOKEN", "active")),
    )
    _write_ledger(tmp_path, previous)
    current = OwnershipLedger(machines=("apps",))

    tombstones = ownership_tombstones(tmp_path, current)

    assert tombstones.generated == previous.generated
    assert tombstones.config == previous.config
    assert tombstones.machines == ("retired",)
    assert tombstones.secrets == (OwnedSecret("REMOVED_PASSWORD", "service:removed"),)


def test_local_prune_is_bounded_to_generated_owned_paths(tmp_path) -> None:
    removed = tmp_path / "generated" / "removed.env"
    removed.parent.mkdir(parents=True)
    removed.write_text("secret\n")
    retired = tmp_path / "generated" / "retired" / "compose.yaml"
    retired.parent.mkdir(parents=True)
    retired.write_text("services: {}\n")
    retained = tmp_path / "config" / "removed"
    retained.parent.mkdir()
    retained.write_text("durable\n")
    _write_ledger(
        tmp_path,
        OwnershipLedger(
            generated=(OwnedPath("generated/removed.env", "removed", "apps"),),
            config=(OwnedPath("config/removed", "removed", "apps"),),
            machines=("retired",),
        ),
    )

    prune_local_ownership_tombstones(tmp_path, OwnershipLedger())

    assert not removed.exists()
    assert not retired.exists()
    assert retained.read_text() == "durable\n"


def test_local_prune_refuses_nested_generated_symlink(tmp_path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    protected = outside / "secret"
    protected.write_text("keep\n")
    generated = tmp_path / "generated"
    generated.mkdir()
    (generated / "nested").symlink_to(outside, target_is_directory=True)
    _write_ledger(
        tmp_path,
        OwnershipLedger(generated=(OwnedPath("generated/nested/secret", "removed", "apps"),)),
    )

    with pytest.raises(ValueError, match="symbolic link"):
        prune_local_ownership_tombstones(tmp_path, OwnershipLedger())

    assert protected.read_text() == "keep\n"


def test_commit_prunes_only_tracked_removed_service_secrets(tmp_path, monkeypatch) -> None:
    _write_ledger(
        tmp_path,
        OwnershipLedger(
            secrets=(
                OwnedSecret("REMOVED_PASSWORD", "service:removed"),
                OwnedSecret("OPERATOR_TOKEN", "active"),
            )
        ),
    )
    saved: list[dict[str, str]] = []
    monkeypatch.setattr(
        "toolkit.core.secrets.secrets.load_secrets_plaintext",
        lambda _path: {"REMOVED_PASSWORD": "old", "OPERATOR_TOKEN": "keep", "UNTRACKED": "keep"},
    )
    monkeypatch.setattr(
        "toolkit.core.secrets.secrets.save_secrets_plaintext",
        lambda values, _path: saved.append(dict(values)),
    )

    removed = commit_ownership_ledger(
        tmp_path,
        OwnershipLedger(secrets=(OwnedSecret("OPERATOR_TOKEN", "active"),)),
    )

    assert removed == ("REMOVED_PASSWORD",)
    assert saved == [{"OPERATOR_TOKEN": "keep", "UNTRACKED": "keep"}]
    assert load_ownership_ledger(tmp_path).secrets == (OwnedSecret("OPERATOR_TOKEN", "active"),)
