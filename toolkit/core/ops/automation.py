"""Service automation helpers — auto-configure services after first startup.

These functions run after containers are healthy to extract API keys,
create admin users, and wire services together without manual UI steps.
"""

from __future__ import annotations

import functools
import time

import httpx


def retry_hook(max_retries: int = 3, base_delay: int = 5, backoff: float = 2.0):
    """Decorator that retries a hook function with exponential backoff.

    Each attempt sleeps ``base_delay * backoff ** attempt`` seconds before
    retrying.  After exhausting all attempts the last exception is wrapped
    in a ``RuntimeError`` with a clear failure message.

    Intended for idempotent post-start hook functions where transient
    container-not-ready errors are expected.
    """

    def decorator(func):
        @functools.wraps(func)
        def wrapper(*args: object, **kwargs: object) -> object:
            last_exc: Exception | None = None
            for attempt in range(max_retries):
                try:
                    return func(*args, **kwargs)
                except Exception as e:
                    last_exc = e
                    if attempt < max_retries - 1:
                        delay = base_delay * (backoff**attempt)
                        time.sleep(delay)
            msg = f"Hook {func.__name__} failed after {max_retries} attempts: {last_exc}"
            raise RuntimeError(msg)

        return wrapper

    return decorator


def resolve_docker_service_url(service: str, port: int, fallback_host: str | None = None) -> str:
    """Return a reachable base URL for a compose service from host or in-network.

    Compose DNS names resolve only inside the Docker network.
    When hooks run on the LXC host, prefer the container bridge IP from ``docker inspect``.
    Never fall back to the service hostname on the host — that causes ``Name or service not known``.
    """
    import json
    import subprocess

    def _container_ip(name: str) -> str:
        try:
            out = subprocess.run(
                ["docker", "inspect", name, "-f", "{{json .NetworkSettings.Networks}}"],
                capture_output=True,
                text=True,
                timeout=5,
                check=False,
            )
            if out.returncode != 0:
                return ""
            networks = json.loads((out.stdout or "").strip() or "{}")
            for net in networks.values():
                ip = (net or {}).get("IPAddress", "")
                if ip:
                    return ip
        except (OSError, subprocess.TimeoutExpired, json.JSONDecodeError, ValueError, TypeError):
            pass
        return ""

    ip = _container_ip(service)
    if ip:
        return f"http://{ip}:{port}"
    # Unpublished ports / container not on network yet — use explicit fallback or loopback.
    host = fallback_host or "127.0.0.1"
    return f"http://{host}:{port}"


def http_reachable(url: str, *, timeout: int = 10) -> tuple[bool, str]:
    """Return (ok, detail) for a GET health probe."""
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True)
        if resp.status_code < 500:
            return True, f"HTTP {resp.status_code}"
        return False, f"HTTP {resp.status_code}"
    except httpx.HTTPError as exc:
        return False, str(exc)


def health_check_logs(checks: list[tuple[str, str]]) -> list[str]:
    """Probe named HTTP endpoints; return human-readable log lines."""
    logs: list[str] = []
    for name, url in checks:
        ok, detail = http_reachable(url)
        if ok:
            logs.append(f"{name}: reachable ({detail})")
        else:
            logs.append(f"{name}: not ready ({detail})")
    return logs


def _raise_if_logs_contain(logs: list[str], needle: str, *, message: str) -> None:
    if any(needle in line for line in logs):
        raise RuntimeError(message)


def docker_exec(
    service: str,
    cmd: list[str],
    *,
    timeout: int = 120,
    user: str | None = None,
    environment: dict[str, str] | None = None,
    secret_environment: dict[str, str] | None = None,
    stdin: str | None = None,
    docker_bin: str = "docker",
) -> tuple[int, str]:
    """Run a command inside a running container on this host.

    ``secret_environment`` values are written to the container's standard
    input and never appended to the host's process arguments.
    """
    import subprocess

    from toolkit.core.ops.secret_env import wrap_command_with_secret_environment

    wrapped_cmd, input_payload = wrap_command_with_secret_environment(
        cmd,
        environment=environment,
        secret_environment=secret_environment,
        stdin=stdin,
    )
    try:
        args = [docker_bin, "exec"]
        if input_payload is not None:
            args.append("-i")
        if user:
            args.extend(["-u", user])
        for key, value in (environment or {}).items():
            args.extend(["-e", f"{key}={value}"])
        args.extend([service, *wrapped_cmd])
        proc = subprocess.run(
            args,
            capture_output=True,
            input=input_payload,
            text=True,
            timeout=timeout,
            check=False,
        )
        out = ((proc.stdout or "") + (proc.stderr or "")).strip()
        return proc.returncode, out
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)


def docker_curl(
    service: str,
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    body: str | None = None,
    timeout: int = 15,
    insecure_tls: bool = False,
    ca_file: str | None = None,
    cookie_file: str | None = None,
    cookie_jar: str | None = None,
    docker_bin: str = "docker",
) -> tuple[int, str]:
    """Make a container HTTP request without putting request data in argv."""
    from toolkit.core.net.curl_config import render_curl_config

    request_config = render_curl_config(
        url,
        method=method,
        headers=headers,
        body=body,
        timeout=timeout,
        insecure_tls=insecure_tls,
        ca_file=ca_file,
        cookie_file=cookie_file,
        cookie_jar=cookie_jar,
    )
    return docker_exec(
        service,
        ["curl", "--disable", "--config", "-"],
        stdin=request_config,
        timeout=timeout + 10,
        docker_bin=docker_bin,
    )
