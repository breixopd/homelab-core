from __future__ import annotations

import os
import socket
import threading
from pathlib import Path

import pytest
from toolkit.controller.__main__ import ControllerStartupError, bind_controller_socket, main


def test_bind_controller_socket_sets_owner_group_permissions(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "controller.sock"
    bound = bind_controller_socket(path)
    try:
        assert path.parent.stat().st_mode & 0o777 == 0o750
        assert path.stat().st_mode & 0o777 == 0o660
    finally:
        bound.close()


def test_bind_controller_socket_grants_only_declared_client_group(tmp_path: Path) -> None:
    path = tmp_path / "runtime" / "controller.sock"

    bound = bind_controller_socket(path, client_gid=os.getgid())
    try:
        assert path.parent.stat().st_gid == os.getgid()
        assert path.stat().st_gid == os.getgid()
        assert path.parent.stat().st_mode & 0o777 == 0o750
        assert path.stat().st_mode & 0o777 == 0o660
    finally:
        bound.close()


def test_bind_controller_socket_refuses_live_controller(tmp_path: Path) -> None:
    path = tmp_path / "controller.sock"
    first = bind_controller_socket(path)
    try:
        with pytest.raises(ControllerStartupError, match="already accepting"):
            bind_controller_socket(path)
    finally:
        first.close()


def test_bind_controller_socket_replaces_only_stale_socket(tmp_path: Path) -> None:
    path = tmp_path / "controller.sock"
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(path))
    stale.close()

    replacement = bind_controller_socket(path)
    try:
        assert path.is_socket()
    finally:
        replacement.close()


def test_bind_controller_socket_refuses_non_socket_path(tmp_path: Path) -> None:
    path = tmp_path / "controller.sock"
    path.write_text("do not delete")

    with pytest.raises(ControllerStartupError, match="not a Unix socket"):
        bind_controller_socket(path)

    assert path.read_text() == "do not delete"


def test_main_handles_interrupt_and_cleans_socket(tmp_path: Path, monkeypatch) -> None:
    socket_path = tmp_path / "runtime" / "controller.sock"
    monkeypatch.setenv("HOMELAB_ROOT", str(tmp_path))
    monkeypatch.setenv("HOMELAB_CONTROLLER_DB", str(tmp_path / "state" / "controller.db"))
    monkeypatch.setenv("HOMELAB_CONTROLLER_SOCKET", str(socket_path))

    def interrupt(_server, *, sockets):
        assert sockets[0].getsockname() == str(socket_path)
        raise KeyboardInterrupt

    monkeypatch.setattr("uvicorn.Server.run", interrupt)

    main()

    assert not socket_path.exists()


def test_main_does_not_unlink_replacement_socket(tmp_path: Path, monkeypatch) -> None:
    socket_path = tmp_path / "runtime" / "controller.sock"
    replacement: socket.socket | None = None
    monkeypatch.setenv("HOMELAB_ROOT", str(tmp_path))
    monkeypatch.setenv("HOMELAB_CONTROLLER_DB", str(tmp_path / "state" / "controller.db"))
    monkeypatch.setenv("HOMELAB_CONTROLLER_SOCKET", str(socket_path))

    def replace(_server, *, sockets):
        nonlocal replacement
        socket_path.unlink()
        replacement = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        replacement.bind(str(socket_path))

    monkeypatch.setattr("uvicorn.Server.run", replace)

    try:
        main()
        assert socket_path.is_socket()
    finally:
        if replacement is not None:
            replacement.close()
        socket_path.unlink(missing_ok=True)


def test_main_starts_and_stops_worker_with_server(tmp_path: Path, monkeypatch) -> None:
    started = threading.Event()
    stopped = threading.Event()

    class FakeListener:
        closed = False

        def close(self) -> None:
            self.closed = True

    listener = FakeListener()
    monkeypatch.setenv("HOMELAB_ROOT", str(tmp_path))
    monkeypatch.setenv("HOMELAB_CONTROLLER_DB", str(tmp_path / "state" / "controller.db"))
    monkeypatch.setenv("HOMELAB_CONTROLLER_SOCKET", str(tmp_path / "runtime" / "controller.sock"))
    monkeypatch.setattr(
        "toolkit.controller.__main__.bind_controller_socket",
        lambda _path, *, client_gid=None: listener,
    )

    def run_worker(_worker, stop, *, poll_interval=1.0):
        started.set()
        stop.wait(1)
        stopped.set()

    def run_server(_server, *, sockets):
        assert sockets == [listener]
        assert started.wait(1)

    monkeypatch.setattr("toolkit.controller.worker.ControllerWorker.run_forever", run_worker)
    monkeypatch.setattr("uvicorn.Server.run", run_server)

    main()

    assert stopped.is_set()
    assert listener.closed is True
