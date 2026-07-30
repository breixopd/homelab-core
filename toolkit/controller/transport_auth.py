"""Owner-only credentials for role-scoped controller Unix-socket clients."""

from __future__ import annotations

import os
import secrets
import stat
from pathlib import Path


def read_transport_token(path: Path, *, allow_group_read: bool = False) -> str:
    if not path.is_absolute():
        raise RuntimeError("Controller transport token path must be absolute")
    descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    try:
        metadata = os.fstat(descriptor)
        forbidden_mode = 0o037 if allow_group_read else 0o077
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_mode & forbidden_mode:
            raise RuntimeError("Controller transport token must be an owner-only regular file")
        if metadata.st_uid not in {0, os.geteuid()}:
            raise RuntimeError("Controller transport token has an unexpected owner")
        if allow_group_read and metadata.st_mode & 0o040:
            process_groups = {os.getgid(), *os.getgroups()}
            if os.geteuid() != 0 and metadata.st_gid not in process_groups:
                raise RuntimeError("Controller transport token is not readable by this client group")
        value = os.read(descriptor, 513).decode("ascii").strip()
    finally:
        os.close(descriptor)
    if len(value) < 32 or len(value) > 512:
        raise RuntimeError("Controller transport token has an invalid length")
    return value


def load_or_create_transport_token(path: Path, *, group_gid: int | None = None) -> str:
    if not path.is_absolute():
        raise RuntimeError("Controller transport token path must be absolute")
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    parent_mode = path.parent.lstat().st_mode
    if stat.S_ISLNK(parent_mode) or not stat.S_ISDIR(parent_mode):
        raise RuntimeError("Controller transport token parent must be a directory")
    if group_gid is not None:
        try:
            descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
        except FileNotFoundError:
            pass
        else:
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid not in {0, os.geteuid()}:
                    raise RuntimeError("Controller transport token must be an owner-managed regular file")
                if metadata.st_mode & 0o027:
                    raise RuntimeError("Controller transport token has unsafe permissions")
                os.fchown(descriptor, -1, group_gid)
                os.fchmod(descriptor, 0o640)
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            return read_transport_token(path, allow_group_read=True)
    else:
        try:
            return read_transport_token(path)
        except FileNotFoundError:
            pass

    value = secrets.token_urlsafe(48)
    temporary_path = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    try:
        descriptor = os.open(
            temporary_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | os.O_NOFOLLOW,
            0o640 if group_gid is not None else 0o600,
        )
    except FileExistsError as exc:
        raise RuntimeError("Controller transport token temporary path collision") from exc
    try:
        if group_gid is not None:
            os.fchown(descriptor, -1, group_gid)
            os.fchmod(descriptor, 0o640)
        remaining = memoryview(value.encode("ascii"))
        while remaining:
            remaining = remaining[os.write(descriptor, remaining) :]
        os.fsync(descriptor)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise
    finally:
        os.close(descriptor)
    try:
        try:
            os.link(temporary_path, path, follow_symlinks=False)
        except FileExistsError:
            return read_transport_token(path, allow_group_read=group_gid is not None)
        directory = os.open(path.parent, os.O_RDONLY | os.O_CLOEXEC | os.O_DIRECTORY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
        return read_transport_token(path, allow_group_read=group_gid is not None)
    finally:
        temporary_path.unlink(missing_ok=True)
