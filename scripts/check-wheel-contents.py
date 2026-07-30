#!/usr/bin/env python3
"""Verify that a wheel contains every runtime asset from the toolkit tree."""

from __future__ import annotations

import argparse
import zipfile
from pathlib import Path


def _source_assets(root: Path) -> set[str]:
    toolkit = root / "toolkit"
    return {
        path.relative_to(root).as_posix()
        for path in toolkit.rglob("*")
        if path.is_file() and path.suffix != ".py" and "__pycache__" not in path.parts
    }


def _wheel_entries(wheel: Path) -> set[str]:
    with zipfile.ZipFile(wheel) as archive:
        return set(archive.namelist())


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", type=Path, help="Wheel file to inspect")
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    expected = _source_assets(root)
    missing = sorted(expected - _wheel_entries(args.wheel))
    if missing:
        print(f"Wheel is missing {len(missing)} runtime assets:")
        for path in missing:
            print(f"  {path}")
        return 1
    print(f"Wheel contains all {len(expected)} runtime assets")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
