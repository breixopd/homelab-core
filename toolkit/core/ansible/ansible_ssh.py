"""Resolve Ansible SSH private key path for LXC ProxyCommand access."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import os
import shlex
import shutil
import socket
import subprocess
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from toolkit.core.config.config import Config, config_path, load_config


def resolve_tool(name: str, root: Path | None = None) -> str | None:
    """Return path to a CLI tool, checking PATH then repo .venv/bin."""
    path = shutil.which(name)
    if path:
        return path
    if root is not None:
        venv_bin = root.resolve() / ".venv" / "bin" / name
        if venv_bin.is_file() and os.access(venv_bin, os.X_OK):
            return str(venv_bin)
    return None


def resolve_ansible_ssh_key(cfg: Config | None = None, root: Path | None = None) -> Path | None:
    """Return an existing private key path for SSH to LXCs via Proxmox jump host."""
    if cfg is None:
        if root is None:
            return None
        cfg = load_config(config_path(root))

    candidates: list[str] = []
    if cfg.ssh.key_file:
        candidates.append(cfg.ssh.key_file)
    if root is not None:
        candidates.append(str(root.resolve() / "ssh" / "homelab_admin_ed25519"))
    candidates.extend(
        [
            str(Path.home() / ".ssh" / "id_ed25519"),
            str(Path.home() / ".ssh" / "id_rsa"),
            # Key deployed to every guest LXC by the Ansible bootstrap. When a
            # verify/hook runs *on* an LXC (e.g. `deploy verify --hooks --node infra`
            # running on infra-01), the controller's ssh.key_file path does not
            # exist there — this fallback lets infra reach media/apps for the
            # cross-VM SSSD/LDAP checks without requiring a separate key.
            str(Path.home() / ".ssh" / "homelab-deploy"),
        ]
    )

    for raw in candidates:
        path = Path(os.path.expanduser(raw))
        if path.is_file():
            return path.resolve()
    return None


def ssh_proxy_command(cfg: Config, root: Path | None = None) -> str:
    """Build ProxyCommand for reaching LXCs via Proxmox jump host."""
    from toolkit.core.infra.host_capacity import resolve_proxmox_host
    from toolkit.core.infra.proxmox_ssh import resolve_proxmox_proxy_key

    key = resolve_proxmox_proxy_key(cfg, root)
    if key is None:
        return ""
    prox = resolve_proxmox_host(cfg, root)
    if not prox:
        return ""
    kh = ""
    if root is not None:
        kh_file = root / "automation" / "ansible" / "inventory" / "known_hosts"
        if kh_file.is_file():
            kh = f"-o UserKnownHostsFile={kh_file}"
    return (
        f"ssh -i {key} -o BatchMode=yes -o IdentitiesOnly=yes -o IdentityAgent=none "
        f"-o StrictHostKeyChecking=accept-new -o ConnectTimeout={cfg.proxmox.ssh.connect_timeout} "
        f"-o ServerAliveInterval=30 "
        f"-o ServerAliveCountMax=120 {kh} "
        f"-p {cfg.proxmox.ssh.port} -W %h:%p {cfg.proxmox.ssh.user}@{prox}"
    )


def _inventory_known_hosts(root: Path | None) -> str:
    if root is None:
        return ""
    kh = root / "automation" / "ansible" / "inventory" / "known_hosts"
    return str(kh) if kh.is_file() else ""


def _ssh_control_path(root: Path | None, identity: str) -> Path | None:
    if root is None:
        return None
    # Keep this path short: OpenSSH adds a random suffix while creating the
    # socket, and Unix-domain paths are commonly limited to 108 bytes.
    directory = root.resolve() / ".homelab-state" / "cm"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
    return directory / f"c-{digest}"


def _local_network_ips() -> list[str]:
    """Return all IPv4 addresses bound to local interfaces (non-loopback)."""
    try:
        out = subprocess.run(["hostname", "-I"], capture_output=True, text=True, timeout=5, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    return [tok for tok in out.split() if not tok.startswith("127.") and ":" not in tok]


def _is_local_ip(ip: str) -> bool:
    """True when ``ip`` is bound to a local interface (loopback or own address)."""
    if ip in ("127.0.0.1", "::1", "localhost"):
        return True
    return ip in _local_network_ips()


def _is_directly_reachable(ip: str, prefix_length: int) -> bool:
    """True when ``ip`` is reachable without a jump host.

    Returns True when the target is a local IP or on the same declared subnet as
    a local interface. Guest machines share a private network, so a verify
    running on infra can SSH directly to media/apps without bouncing through the
    Proxmox jump host (which would reject the guest SSH key).
    """
    if _is_local_ip(ip):
        return True
    try:
        target = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for local in _local_network_ips():
        try:
            local_addr = ipaddress.ip_address(local)
        except ValueError:
            continue
        if local_addr.version != target.version:
            continue
        network = ipaddress.ip_network(f"{local}/{prefix_length}", strict=False)
        if target in network:
            return True
    return False


def _machine_ssh_target(cfg: Config, vm_ip: str) -> tuple[str, int, int]:
    machine = next((candidate for candidate in cfg.machines.values() if candidate.address == vm_ip), None)
    if machine is None or not machine.enabled:
        raise ValueError(f"SSH target {vm_ip!r} is not an enabled machine")
    return machine.effective_ssh_user, machine.ssh_port, machine.cidr


def ssh_run_on_vm(
    cfg: Config,
    vm_ip: str,
    remote_command: str,
    *,
    root: Path | None = None,
    timeout: int = 30,
    retries: int = 1,
    stdin: str | None = None,
) -> tuple[int, str, str]:
    """Run a shell command on an LXC via Proxmox jump host.

    When ``vm_ip`` is a local address (e.g. the hook is already running on the
    target LXC), the command is executed locally without SSH — the LXCs have no
    SSH key to reach themselves, so the SSH path would always fail.

    When ``vm_ip`` is on the same /24 as a local interface (e.g. verify running
    on infra reaching media/apps), SSH directly without the Proxmox jump host —
    the guest SSH key is not authorized on the Proxmox host.
    """
    import time

    if _is_local_ip(vm_ip):
        try:
            result = subprocess.run(
                ["bash", "-c", remote_command],
                capture_output=True,
                input=stdin,
                text=True,
                timeout=timeout,
                check=False,
            )
            return result.returncode, result.stdout, result.stderr
        except (OSError, subprocess.TimeoutExpired) as exc:
            return 1, "", str(exc)

    try:
        ssh_user, ssh_port, prefix_length = _machine_ssh_target(cfg, vm_ip)
    except ValueError as exc:
        return 255, "", str(exc)
    direct = _is_directly_reachable(vm_ip, prefix_length)
    last_err = ""
    kh = _inventory_known_hosts(root)
    for attempt in range(max(1, retries)):
        key = resolve_ansible_ssh_key(cfg, root)
        if key is None:
            return 255, "", "no SSH key"
        proxy = "" if direct else ssh_proxy_command(cfg, root)
        ssh_cmd = [
            "ssh",
            "-i",
            str(key),
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "IdentitiesOnly=yes",
            "-o",
            "IdentityAgent=none",
            "-o",
            "UpdateHostKeys=yes",
            "-o",
            "ServerAliveInterval=30",
            "-o",
            "ServerAliveCountMax=120",
            "-o",
            f"ConnectTimeout={min(timeout, cfg.ssh.connect_timeout)}",
            "-p",
            str(ssh_port),
        ]
        if kh:
            ssh_cmd.extend(["-o", f"UserKnownHostsFile={kh}"])
        if proxy:
            ssh_cmd.extend(["-o", f"ProxyCommand={proxy}"])
        control_path = _ssh_control_path(root, f"{ssh_user}@{vm_ip}:{ssh_port}|{proxy}")
        if control_path is not None:
            ssh_cmd.extend(
                [
                    "-o",
                    "ControlMaster=auto",
                    "-o",
                    "ControlPersist=120",
                    "-o",
                    f"ControlPath={control_path}",
                ]
            )
        ssh_cmd.append(f"{ssh_user}@{vm_ip}")
        ssh_cmd.append(remote_command)
        try:
            result = subprocess.run(
                ssh_cmd,
                capture_output=True,
                input=stdin,
                text=True,
                timeout=timeout,
                check=False,
            )
            if result.returncode == 0 or attempt + 1 >= retries:
                return result.returncode, result.stdout, result.stderr
            last_err = result.stderr.strip() or f"exit {result.returncode}"
        except (OSError, subprocess.TimeoutExpired) as exc:
            last_err = str(exc)
            if attempt + 1 >= retries:
                return 255, "", last_err
        time.sleep(min(5 * (attempt + 1), 20))
    return 255, "", last_err


def sanitize_probe_output(text: str, *, max_len: int = 120) -> str:
    """Collapse tracebacks and long probe failures into a short verify detail."""
    body = (text or "").strip()
    if not body:
        return ""
    if "Traceback (most recent call last)" in body:
        for line in reversed(body.splitlines()):
            line = line.strip()
            if line and not line.startswith("File "):
                return line[:max_len]
        return "HTTP probe failed"
    if len(body) > max_len:
        return body[:max_len]
    return body


def _url_with_host(url: str, host: str) -> str:
    """Replace a validated HTTP(S) URL host while preserving its port and path."""
    parsed = urlsplit(url)
    bracketed_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    netloc = bracketed_host if parsed.port is None else f"{bracketed_host}:{parsed.port}"
    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))


_PYTHON_HTTP_FALLBACK = """
import json
import sys
import urllib.request

payload = json.loads(sys.stdin.buffer.read(65537))
body = payload["body"]
request = urllib.request.Request(
    payload["url"],
    data=body.encode() if body is not None else None,
    headers=payload["headers"],
    method=payload["method"],
)
with urllib.request.urlopen(request, timeout=payload["timeout"]) as response:
    sys.stdout.buffer.write(response.read(65536))
"""


def docker_exec_curl(
    cfg: Config,
    vm_ip: str,
    container: str,
    url: str,
    *,
    root: Path | None = None,
    headers: dict[str, str] | None = None,
    method: str = "GET",
    body: str | None = None,
    ca_file: str | None = None,
    cookie_file: str | None = None,
    cookie_jar: str | None = None,
    timeout: int = 15,
) -> tuple[int, str]:
    """Probe a service without exposing request headers in process arguments."""
    from toolkit.core.net.curl_config import render_curl_config

    container_target = _url_with_host(url, "127.0.0.1") if urlsplit(url).hostname in {"localhost", "127.0.0.1"} else url
    container_config = render_curl_config(
        container_target,
        method=method,
        headers=headers,
        body=body,
        timeout=timeout,
        ca_file=ca_file,
        cookie_file=cookie_file,
        cookie_jar=cookie_jar,
    )
    command = f"docker exec -i {shlex.quote(container)} curl --disable --config -"
    rc, out, err = ssh_run_on_vm(
        cfg,
        vm_ip,
        command,
        root=root,
        timeout=timeout + 10,
        stdin=container_config,
    )
    if rc == 0:
        return 0, out

    if ca_file or cookie_file or cookie_jar:
        return rc, sanitize_probe_output(out or err)

    # A few minimal service images omit curl. Resolve the bridge address without
    # request metadata, then retry using the managed VM's curl binary.
    inspect_command = (
        "docker inspect -f '{{range.NetworkSettings.Networks}}{{.IPAddress}} {{end}}' "
        f"{shlex.quote(container)}"
    )
    inspect_rc, container_ip, inspect_err = ssh_run_on_vm(
        cfg,
        vm_ip,
        inspect_command,
        root=root,
        timeout=15,
    )
    container_ip = (container_ip or "").split()[0] if container_ip else ""
    if inspect_rc != 0 or not container_ip:
        return rc, sanitize_probe_output(out or err or inspect_err)

    fallback_url = _url_with_host(url, container_ip)
    fallback_config = render_curl_config(
        fallback_url,
        method=method,
        headers=headers,
        body=body,
        timeout=timeout,
    )
    rc, out, err = ssh_run_on_vm(
        cfg,
        vm_ip,
        "curl --disable --config -",
        root=root,
        timeout=timeout + 10,
        stdin=fallback_config,
    )
    if rc == 0:
        return 0, out

    payload = json.dumps(
        {
            "body": body,
            "headers": dict(headers or {}),
            "method": method,
            "timeout": timeout,
            "url": fallback_url,
        },
        separators=(",", ":"),
    )
    python_command = f"python3 -c {shlex.quote(_PYTHON_HTTP_FALLBACK)}"
    rc, out, python_err = ssh_run_on_vm(
        cfg,
        vm_ip,
        python_command,
        root=root,
        timeout=timeout + 10,
        stdin=payload,
    )
    if rc == 0:
        return 0, out
    return rc, sanitize_probe_output(out or python_err or err)


def should_verify_remote(
    cfg: Config,
    root: Path,
    *,
    on_guest: bool = False,
) -> bool:
    """True when deploy verify/hooks should fan out to managed machines."""
    controller_local = os.environ.get("HOMELAB_CONTROLLER_ROLE", "").strip().lower() == "local"
    if (on_guest or os.environ.get("HOMELAB_NODE")) and not controller_local:
        return False
    inventory = root / "automation" / "ansible" / "inventory" / "hosts.yml"
    return bool(
        cfg.is_multi_node
        and cfg.proxmox.provision_machines
        and inventory.is_file()
        and resolve_tool("ansible", root) is not None
    )


def ssh_argv(cfg: Config, root: Path | None, vm_ip: str, *, program: str = "ssh") -> list[str]:
    """Base argv for ssh/scp to LXCs (key, proxy, known_hosts)."""
    key = resolve_ansible_ssh_key(cfg, root)
    if key is None:
        raise RuntimeError("No SSH key configured")
    _ssh_user, ssh_port, _prefix = _machine_ssh_target(cfg, vm_ip)
    argv = [
        program,
        "-i",
        str(key),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "IdentitiesOnly=yes",
        "-o",
        "IdentityAgent=none",
        "-o",
        "ServerAliveInterval=30",
        "-o",
        f"ConnectTimeout={cfg.ssh.connect_timeout}",
    ]
    argv.extend(["-P" if program == "scp" else "-p", str(ssh_port)])
    kh = _inventory_known_hosts(root)
    if kh:
        argv.extend(["-o", f"UserKnownHostsFile={kh}"])
    proxy = ssh_proxy_command(cfg, root)
    if proxy:
        argv.extend(["-o", f"ProxyCommand={proxy}"])
    return argv


def _reserve_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _wait_for_local_forward(process: subprocess.Popen[str], port: int, timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("SSH local forward exited before becoming ready")
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.1)
    raise TimeoutError(f"SSH local forward did not become ready within {timeout:g}s")


def _stop_process(process: subprocess.Popen[str]) -> None:
    try:
        if process.poll() is None:
            process.terminate()
        process.wait(timeout=5)
        return
    except (OSError, subprocess.TimeoutExpired):
        pass
    try:
        process.kill()
        process.wait(timeout=5)
    except (OSError, subprocess.TimeoutExpired):
        pass


@contextmanager
def ssh_local_forward(
    cfg: Config,
    root: Path | None,
    vm_ip: str,
    remote_port: int,
    *,
    remote_host: str,
) -> Iterator[int]:
    """Temporarily forward a target-local TCP port to controller loopback."""
    if not 1 <= remote_port <= 65535:
        raise ValueError("remote port must be between 1 and 65535")
    try:
        remote_address = ipaddress.ip_address(remote_host)
    except ValueError as exc:
        raise ValueError("remote host must be an IP address") from exc
    if remote_address.version != 4:
        raise ValueError("remote host must be an IPv4 address")
    if str(remote_address) not in {vm_ip, "127.0.0.1"}:
        raise ValueError("remote host must be the SSH target or its loopback")
    ssh_user, _ssh_port, _prefix = _machine_ssh_target(cfg, vm_ip)
    process: subprocess.Popen[str] | None = None
    last_error: Exception | None = None
    local_port = 0
    for _attempt in range(3):
        local_port = _reserve_local_port()
        command = [
            *ssh_argv(cfg, root, vm_ip),
            "-o",
            "ExitOnForwardFailure=yes",
            "-o",
            "ControlMaster=no",
            "-N",
            "-L",
            f"127.0.0.1:{local_port}:{remote_address}:{remote_port}",
            f"{ssh_user}@{vm_ip}",
        ]
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                text=True,
            )
            _wait_for_local_forward(process, local_port, cfg.ssh.connect_timeout)
            if process.poll() is None:
                break
            raise RuntimeError("SSH local forward stopped during startup")
        except (OSError, RuntimeError, TimeoutError) as exc:
            last_error = exc
            if process is not None:
                _stop_process(process)
            process = None
        except BaseException:
            if process is not None:
                _stop_process(process)
            raise
    if process is None:
        raise RuntimeError(f"Could not establish SSH local forward: {last_error}") from last_error
    try:
        yield local_port
    finally:
        _stop_process(process)


def scp_to_vm(
    cfg: Config,
    root: Path | None,
    local_path: Path,
    vm_ip: str,
    remote_path: str,
    *,
    timeout: int = 300,
) -> None:
    """Copy a file to a guest LXC."""
    ssh_user, _ssh_port, _prefix = _machine_ssh_target(cfg, vm_ip)
    remote = f"{ssh_user}@{vm_ip}:{remote_path}"
    cmd = [*ssh_argv(cfg, root, vm_ip, program="scp")]
    if local_path.is_dir():
        cmd.append("-r")
    cmd.extend((str(local_path), remote))
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"scp to {vm_ip} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "scp failed")[:300]
        raise RuntimeError(err)


def scp_from_vm(
    cfg: Config,
    root: Path | None,
    vm_ip: str,
    remote_path: str,
    local_path: Path,
    *,
    timeout: int = 300,
) -> None:
    """Copy a file or directory from a declared managed guest."""
    ssh_user, _ssh_port, _prefix = _machine_ssh_target(cfg, vm_ip)
    remote = f"{ssh_user}@{vm_ip}:{remote_path}"
    cmd = [*ssh_argv(cfg, root, vm_ip, program="scp"), "-r", remote, str(local_path)]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"scp from {vm_ip} timed out after {timeout}s") from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "scp failed")[:300]
        raise RuntimeError(err)


def refresh_known_hosts_file(root: Path, cfg: Config | None = None) -> list[str]:
    """Re-scan LXC host keys via Proxmox jump and update Ansible known_hosts."""
    import os
    import subprocess

    if cfg is None:
        from toolkit.core.config.config import config_path, load_config

        cfg = load_config(config_path(root))

    kh = root / "automation" / "ansible" / "inventory" / "known_hosts"
    kh.parent.mkdir(parents=True, exist_ok=True)
    key = resolve_ansible_ssh_key(cfg, root)
    proxy = ssh_proxy_command(cfg, root)
    if not key or not proxy:
        return ["known_hosts: missing SSH key or Proxmox proxy"]

    from toolkit.core.ops.controller_guard import allow_env, is_dedicated_deploy_controller

    user_kh = Path.home() / ".ssh" / "known_hosts"
    touch_user_kh = (
        allow_env("ssh_user_known_hosts")
        or is_dedicated_deploy_controller()
        or os.environ.get("HOMELAB_REFRESH_USER_KNOWN_HOSTS", "").strip().lower() in ("1", "true", "yes")
    )
    lines: list[str] = []
    targets = [(node, cfg.machines[node]) for node in cfg.enabled_nodes]
    kh.write_text("")
    for node, machine in targets:
        ip = machine.address
        for kh_file in (kh, user_kh) if touch_user_kh else (kh,):
            if kh_file.is_file():
                subprocess.run(
                    ["ssh-keygen", "-R", ip, "-f", str(kh_file)],
                    capture_output=True,
                    check=False,
                )
        probe = subprocess.run(
            [
                "ssh",
                "-i",
                str(key),
                "-o",
                "BatchMode=yes",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "IdentityAgent=none",
                "-o",
                f"UserKnownHostsFile={kh}",
                "-o",
                f"ProxyCommand={proxy}",
                "-o",
                f"ConnectTimeout={cfg.ssh.connect_timeout}",
                "-p",
                str(machine.ssh_port),
                f"{machine.effective_ssh_user}@{ip}",
                "hostname",
            ],
            capture_output=True,
            text=True,
            check=False,
            timeout=cfg.ssh.command_timeout,
        )
        if probe.returncode == 0:
            lines.append(f"known_hosts: trusted {node} ({ip})")
        else:
            lines.append(f"known_hosts: SSH probe failed for {node} ({ip}): {probe.stderr[:80]}")
    return lines
