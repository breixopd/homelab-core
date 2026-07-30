"""Authenticated controller transport over the existing managed-node SSH path."""

from __future__ import annotations

import shlex
from pathlib import Path

import httpx

from toolkit.core.ansible.ansible_ssh import ssh_run_on_vm
from toolkit.core.config.config import Config

_STATUS_MARKER = "\n__HOMELAB_CONTROLLER_STATUS__:"
_CONTROLLER_SOCKET = "/run/homelab-controller/controller.sock"
_CONTROLLER_TOKEN = "/var/lib/homelab-controller/local.token"
_CONTROLLER_CONTAINER = "homelab-controller"


class SSHControllerTransport(httpx.BaseTransport):
    """Proxy controller HTTP requests through SSH without exporting its token."""

    def __init__(self, cfg: Config, root: Path, *, timeout: int = 30) -> None:
        self._cfg = cfg
        self._root = root.resolve()
        self._timeout = timeout
        self._node = cfg.control_node
        self._address = cfg.node_ip(self._node)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.url.host != "controller":
            raise httpx.ConnectError("refusing non-controller SSH request", request=request)
        if request.method not in {"DELETE", "GET", "POST", "PUT"}:
            raise httpx.ConnectError("unsupported controller request method", request=request)

        target = f"http://controller{request.url.raw_path.decode('ascii')}"
        curl = [
            "curl",
            "--silent",
            "--show-error",
            "--unix-socket",
            _CONTROLLER_SOCKET,
            "--request",
            request.method,
            "--header",
            "X-Controller-Transport: local",
            "--header",
            'X-Controller-Token: "$token"',
            "--header",
            "Content-Type: application/json",
            "--data-binary",
            "@-",
            "--write-out",
            f"{_STATUS_MARKER}%{{http_code}}",
            target,
        ]
        quoted = " ".join(
            '"X-Controller-Token: $token"' if part == 'X-Controller-Token: "$token"' else shlex.quote(part)
            for part in curl
        )
        remote = f"docker exec -i {_CONTROLLER_CONTAINER} sh -c " + shlex.quote(
            f"token=$(cat {_CONTROLLER_TOKEN}); exec {quoted}"
        )
        code, stdout, stderr = ssh_run_on_vm(
            self._cfg,
            self._address,
            remote,
            root=self._root,
            timeout=self._timeout,
            retries=2,
            stdin=request.read().decode("utf-8"),
        )
        if code:
            raise httpx.ConnectError(
                f"controller SSH bridge failed: {(stderr or 'remote command failed').strip()}",
                request=request,
            )
        body, marker, raw_status = stdout.rpartition(_STATUS_MARKER)
        if marker != _STATUS_MARKER or not raw_status.strip().isdigit():
            raise httpx.RemoteProtocolError("controller SSH bridge returned an invalid response", request=request)
        return httpx.Response(
            int(raw_status.strip()),
            content=body.encode("utf-8"),
            headers={"content-type": "application/json"},
            request=request,
        )
