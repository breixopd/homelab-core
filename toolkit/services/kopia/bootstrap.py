"""Kopia post-deploy bootstrap: repository connect/create and retention policies."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from toolkit.core.ops.automation import docker_exec

if TYPE_CHECKING:
    from toolkit.core.config.config import Config


def _repository_arguments(cfg: Config) -> tuple[str, list[str]]:
    if cfg.backups.target == "local":
        return "filesystem", ["--path=/repository"]
    host = next((item for item in cfg.external_hosts if item.name == cfg.backups.storage_host), None)
    if host is None:
        raise ValueError("configured remote backup storage host is missing")
    repository_path = host.integration_value("backup-storage", "path")
    if not isinstance(repository_path, str) or not repository_path:
        raise ValueError("configured remote backup storage path is missing")
    return "sftp", [
        f"--host={host.ip}",
        f"--port={host.ssh_port}",
        f"--username={host.ssh_user}",
        f"--path={repository_path}",
        "--keyfile=/app/config/remote_ed25519",
        "--known-hosts=/app/config/known_hosts",
    ]


def bootstrap_kopia_repository(cfg: Config, secrets: dict[str, str]) -> list[str]:
    logs: list[str] = []
    repo_pass = secrets.get("KOPIA_REPOSITORY_PASSWORD", "")
    if not repo_pass:
        logs.append("Kopia: KOPIA_REPOSITORY_PASSWORD not set — skip repo init")
        return logs
    kopia_environment = {"KOPIA_CONFIG_PATH": "/app/config/repository.config"}
    kopia_secret_environment = {"KOPIA_PASSWORD": repo_pass}
    from toolkit.core.manifest.placement import service_node

    server_node = service_node(cfg, "kopia")
    try:
        provider, provider_args = _repository_arguments(cfg)
        provider_label = "SFTP" if provider == "sftp" else "filesystem"
        status_rc, status_out = docker_exec(
            "kopia",
            ["kopia", "repository", "status"],
            environment=kopia_environment,
            secret_environment=kopia_secret_environment,
        )
        status_text = (status_out or "").lower()
        connected = status_rc == 0 and "connected" in status_text
        provider_matches = provider in status_text
        if connected and provider_matches:
            logs.append("Kopia: repository already connected")
            _ensure_agent_users(
                cfg,
                kopia_environment,
                kopia_secret_environment,
                secrets.get("KOPIA_AGENT_PASSWORD", ""),
                logs,
            )
            _ensure_kopia_policies(kopia_environment, kopia_secret_environment, logs)
            return logs
        if connected:
            disconnect_rc, disconnect_out = docker_exec(
                "kopia",
                ["kopia", "repository", "disconnect"],
                environment=kopia_environment,
                secret_environment=kopia_secret_environment,
            )
            if disconnect_rc != 0:
                logs.append(f"Kopia: repository backend switch failed ({(disconnect_out or '')[:120]})")
                return logs
            logs.append(f"Kopia: disconnected previous backend before switching to {provider_label}")
        connect_rc, connect_out = docker_exec(
            "kopia",
            [
                "kopia",
                "repository",
                "connect",
                provider,
                *provider_args,
                f"--override-hostname=homelab-{server_node}",
                "--override-username=kopia",
            ],
            environment=kopia_environment,
            secret_environment=kopia_secret_environment,
        )
        if connect_rc == 0:
            logs.append(f"Kopia: {provider_label} repository connected")
            _ensure_agent_users(
                cfg,
                kopia_environment,
                kopia_secret_environment,
                secrets.get("KOPIA_AGENT_PASSWORD", ""),
                logs,
            )
            _ensure_kopia_policies(kopia_environment, kopia_secret_environment, logs)
            return logs
        connect_err = (connect_out or "").lower()
        uninitialized = (
            "does not seem to contain a valid repository" in connect_err
            or "repository not initialized" in connect_err
            or "blob not found" in connect_err
        )
        if not uninitialized:
            logs.append(f"Kopia: repository connect failed ({(connect_out or '')[:120]})")
            return logs
        create_rc, create_out = docker_exec(
            "kopia",
            [
                "kopia",
                "repository",
                "create",
                provider,
                *provider_args,
                f"--override-hostname=homelab-{server_node}",
                "--override-username=kopia",
            ],
            environment=kopia_environment,
            secret_environment=kopia_secret_environment,
        )
        if create_rc == 0:
            logs.append(f"Kopia: {provider_label} repository created")
        else:
            logs.append(f"Kopia: repository create failed ({(create_out or '')[:120]})")
            return logs
        _ensure_agent_users(
            cfg,
            kopia_environment,
            kopia_secret_environment,
            secrets.get("KOPIA_AGENT_PASSWORD", ""),
            logs,
        )
        _ensure_kopia_policies(kopia_environment, kopia_secret_environment, logs)
        snap_rc, snap_out = docker_exec(
            "kopia",
            [
                "kopia",
                "snapshot",
                "create",
                "/source",
                "--description=Initial bootstrap snapshot",
                "--tags=bootstrap:initial",
            ],
            environment=kopia_environment,
            secret_environment=kopia_secret_environment,
        )
        if snap_rc == 0:
            logs.append("Kopia: initial snapshot created successfully")
        else:
            logs.append(f"Kopia: initial snapshot deferred ({(snap_out or '')[:80]})")
    except Exception as exc:
        logs.append(f"Kopia: bootstrap error ({exc})")
    return logs


def _ensure_agent_users(
    cfg: Config,
    kopia_environment: dict[str, str],
    kopia_secret_environment: dict[str, str],
    password: str,
    logs: list[str],
) -> None:
    if not password:
        logs.append("Kopia: agent credential missing; node enrollment deferred")
        return
    changed = False
    from toolkit.core.manifest.catalog import load_service_catalog
    from toolkit.core.manifest.placement import manifest_runtime_nodes, service_node

    manifest = load_service_catalog().require("kopia")
    agent_nodes = [
        node for node in manifest_runtime_nodes(cfg, manifest, "kopia-agent") if node != service_node(cfg, "kopia")
    ]
    for node in agent_nodes:
        username = f"homelab@homelab-{node}"
        command = [
            "sh",
            "-ec",
            (
                'printf "%s\\n%s\\n" "$KOPIA_AGENT_PASSWORD" "$KOPIA_AGENT_PASSWORD" '
                '| exec script --echo=never -q -e -c "$1" /dev/null'
            ),
            "homelab-kopia-user-password",
            f"kopia server users set {shlex.quote(username)} --ask-password",
        ]
        secret_environment = {**kopia_secret_environment, "KOPIA_AGENT_PASSWORD": password}
        rc, _output = docker_exec(
            "kopia",
            command,
            environment=kopia_environment,
            secret_environment=secret_environment,
        )
        if rc != 0:
            command[-1] = f"kopia server users add {shlex.quote(username)} --ask-password"
            rc, output = docker_exec(
                "kopia",
                command,
                environment=kopia_environment,
                secret_environment=secret_environment,
            )
            if rc != 0:
                logs.append(f"Kopia: failed to provision {username} ({(output or '')[:80]})")
                continue
        changed = True
        logs.append(f"Kopia: repository agent ready for {node}")
    if changed:
        docker_exec("kopia", ["/bin/sh", "-c", "kill -HUP 1"], environment=kopia_environment)


def _ensure_kopia_policies(
    kopia_environment: dict[str, str],
    kopia_secret_environment: dict[str, str],
    logs: list[str],
) -> None:
    """Apply Kopia retention and scheduling policies (idempotent)."""
    policy_rc, policy_out = docker_exec(
        "kopia",
        [
            "kopia",
            "policy",
            "set",
            "--global",
            "--keep-annual=1",
            "--keep-monthly=12",
            "--keep-weekly=8",
            "--keep-daily=14",
            "--keep-hourly=0",
            "--keep-latest=3",
            "--compression=zstd",
        ],
        environment=kopia_environment,
        secret_environment=kopia_secret_environment,
    )
    if policy_rc == 0:
        logs.append("Kopia: global retention policy applied (12M/8W/14D/3latest, zstd)")
    else:
        logs.append(f"Kopia: policy set skipped ({(policy_out or '')[:80]})")
    path_policy_rc, path_policy_out = docker_exec(
        "kopia",
        ["kopia", "policy", "set", "/source", "--snapshot-interval=6h", "--compression=zstd"],
        environment=kopia_environment,
        secret_environment=kopia_secret_environment,
    )
    if path_policy_rc == 0:
        logs.append("Kopia: /source path policy set (6h interval, zstd)")
    else:
        logs.append(f"Kopia: /source policy skipped ({(path_policy_out or '')[:80]})")
