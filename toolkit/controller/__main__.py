"""Run the privileged controller on an owner-managed Unix socket."""

from __future__ import annotations

import errno
import os
import socket
import stat
import threading
from functools import partial
from pathlib import Path

import uvicorn

from toolkit.controller.app import create_controller_app
from toolkit.controller.operations import build_operation_registry
from toolkit.controller.store import ControllerStore
from toolkit.controller.transport_auth import load_or_create_transport_token
from toolkit.controller.worker import ControllerWorker


class ControllerStartupError(RuntimeError):
    pass


def _socket_identity(path: Path) -> tuple[int, int] | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    if not stat.S_ISSOCK(metadata.st_mode):
        return None
    return metadata.st_dev, metadata.st_ino


def _unlink_socket_if_owned(path: Path, identity: tuple[int, int] | None) -> None:
    if identity is not None and _socket_identity(path) == identity:
        path.unlink()


def _socket_is_live(path: Path) -> bool:
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    probe.settimeout(0.25)
    try:
        probe.connect(str(path))
        return True
    except OSError as exc:
        if exc.errno in {errno.ECONNREFUSED, errno.ENOENT}:
            return False
        raise ControllerStartupError(f"cannot verify existing controller socket: {exc.strerror}") from exc
    finally:
        probe.close()


def bind_controller_socket(path: Path, *, client_gid: int | None = None) -> socket.socket:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o750)
    if client_gid is not None:
        os.chown(path.parent, -1, client_gid)
    path.parent.chmod(0o750)
    if path.exists() or path.is_symlink():
        mode = path.lstat().st_mode
        if not stat.S_ISSOCK(mode):
            raise ControllerStartupError(f"refusing to replace {path}: path is not a Unix socket")
        if _socket_is_live(path):
            raise ControllerStartupError(f"controller socket {path} is already accepting connections")
        path.unlink()

    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    identity = None
    try:
        listener.bind(str(path))
        identity = _socket_identity(path)
        if client_gid is not None:
            os.chown(path, -1, client_gid, follow_symlinks=False)
        os.chmod(path, 0o660, follow_symlinks=False)
        listener.listen(128)
        return listener
    except Exception:
        _unlink_socket_if_owned(path, identity)
        listener.close()
        raise


def main() -> None:
    root = Path(os.environ.get("HOMELAB_ROOT", "/opt/homelab")).resolve()
    state_path = Path(os.environ.get("HOMELAB_CONTROLLER_DB", "/var/lib/homelab-controller/controller.db")).resolve()
    socket_path = Path(os.environ.get("HOMELAB_CONTROLLER_SOCKET", "/run/homelab-controller/controller.sock")).resolve()
    local_token_path = Path(
        os.environ.get("HOMELAB_CONTROLLER_LOCAL_TOKEN_FILE", str(state_path.parent / "local.token"))
    )
    ui_token_path = Path(os.environ.get("HOMELAB_CONTROLLER_UI_TOKEN_FILE", str(socket_path.parent / "ui.token")))
    ui_gid_value = os.environ.get("HOMELAB_CONTROLLER_UI_GID", "").strip()
    ui_gid = int(ui_gid_value) if ui_gid_value else None
    if ui_gid is not None and ui_gid <= 0:
        raise ControllerStartupError("HOMELAB_CONTROLLER_UI_GID must be a positive integer")
    local_token = load_or_create_transport_token(local_token_path)
    ui_token = load_or_create_transport_token(ui_token_path, group_gid=ui_gid)
    store = ControllerStore(state_path)
    worker = ControllerWorker(
        store,
        build_operation_registry(root),
        worker_id=f"controller-{os.getpid()}",
        lease_seconds=30,
        registry_factory=partial(build_operation_registry, root),
    )
    app = create_controller_app(
        root=root,
        store=store,
        worker=worker,
        local_transport_token=local_token,
        ui_transport_token=ui_token,
    )
    listener = bind_controller_socket(socket_path, client_gid=ui_gid)
    socket_identity = _socket_identity(socket_path)
    worker_stop = threading.Event()
    worker_thread = threading.Thread(
        target=worker.run_forever,
        args=(worker_stop,),
        name="homelab-controller-worker",
        daemon=True,
    )
    worker_thread.start()
    try:
        config = uvicorn.Config(
            app,
            log_level=os.environ.get("HOMELAB_CONTROLLER_LOG_LEVEL", "info"),
            access_log=True,
            proxy_headers=False,
        )
        try:
            uvicorn.Server(config).run(sockets=[listener])
        except KeyboardInterrupt:
            pass
    finally:
        worker_stop.set()
        worker_thread.join(timeout=5)
        try:
            _unlink_socket_if_owned(socket_path, socket_identity)
        except FileNotFoundError:
            pass
        listener.close()


if __name__ == "__main__":
    main()
