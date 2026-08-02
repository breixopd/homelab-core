"""Trusted discovery and lookup for database restore artifacts."""

from __future__ import annotations

import hashlib
import re
from builtins import list as list_type
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

_DUMP_NAME = re.compile(r"^pre-deploy-\d{8}-\d{6}\.sql\.gz$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class DumpNotFoundError(LookupError):
    """Raised when an opaque dump ID is absent or no longer valid."""


@dataclass(frozen=True, slots=True)
class DumpRecord:
    """A restore artifact issued by a trusted repository discovery pass."""

    dump_id: str
    name: str
    path: str
    size_bytes: int
    sha256: str
    is_remote: bool

    @property
    def size(self) -> str:
        size = float(self.size_bytes)
        for unit in ("B", "KB", "MB", "GB"):
            if size < 1024 or unit == "GB":
                return f"{size:.0f}{unit}"
            size /= 1024
        return f"{size:.0f}GB"


def _dump_id(path: str, size_bytes: int, sha256: str) -> str:
    identity = f"postgres\0{path}\0{size_bytes}\0{sha256}".encode()
    return "dmp_" + hashlib.sha256(identity).hexdigest()[:20]


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class DumpRepository:
    """Lists and resolves dumps within one fixed, operator-controlled directory."""

    def __init__(
        self,
        root: str,
        *,
        is_remote: bool,
        entries: Iterable[Mapping[str, object]] = (),
    ) -> None:
        self._root = root
        self._is_remote = is_remote
        self._entries = tuple(entries)

    @classmethod
    def local(cls, root: Path) -> DumpRepository:
        return cls(str(root.resolve()), is_remote=False)

    @classmethod
    def remote(cls, root: str, entries: Iterable[Mapping[str, object]]) -> DumpRepository:
        return cls(str(PurePosixPath(root)), is_remote=True, entries=entries)

    def list(self) -> list[DumpRecord]:
        records = self._list_remote() if self._is_remote else self._list_local()
        return sorted(records, key=lambda record: record.name, reverse=True)

    def resolve(self, dump_id: str) -> DumpRecord:
        if not dump_id.startswith("dmp_"):
            raise DumpNotFoundError("dump ID is invalid")
        for record in self.list():
            if record.dump_id == dump_id:
                return record
        raise DumpNotFoundError("dump is missing, changed, or no longer eligible")

    def _list_local(self) -> list_type[DumpRecord]:
        root = Path(self._root)
        if not root.is_dir():
            return []
        records: list_type[DumpRecord] = []
        for candidate in root.iterdir():
            if not _DUMP_NAME.fullmatch(candidate.name) or candidate.is_symlink():
                continue
            try:
                path = candidate.resolve(strict=True)
                if path.parent != root or not path.is_file():
                    continue
                size_bytes = path.stat().st_size
                sha256 = _hash_file(path)
            except OSError:
                continue
            records.append(
                DumpRecord(
                    dump_id=_dump_id(str(path), size_bytes, sha256),
                    name=path.name,
                    path=str(path),
                    size_bytes=size_bytes,
                    sha256=sha256,
                    is_remote=False,
                )
            )
        return records

    def _list_remote(self) -> list_type[DumpRecord]:
        root = PurePosixPath(self._root)
        records: list_type[DumpRecord] = []
        for entry in self._entries:
            try:
                path = PurePosixPath(str(entry["path"]))
                size_bytes = int(str(entry["size_bytes"]))
                sha256 = str(entry["sha256"])
            except (KeyError, TypeError, ValueError):
                continue
            if (
                path.parent != root
                or not path.is_absolute()
                or not _DUMP_NAME.fullmatch(path.name)
                or size_bytes <= 0
                or not _SHA256.fullmatch(sha256)
            ):
                continue
            records.append(
                DumpRecord(
                    dump_id=_dump_id(str(path), size_bytes, sha256),
                    name=path.name,
                    path=str(path),
                    size_bytes=size_bytes,
                    sha256=sha256,
                    is_remote=True,
                )
            )
        return records
