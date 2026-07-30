"""Parse docker-compose services into a canonical ServiceGraph index."""

from __future__ import annotations

import json
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass(frozen=True)
class PortBinding:
    published: str
    target: str = ""
    host_ip: str = ""


@dataclass
class ServiceNode:
    name: str
    image: str = ""
    profiles: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    ports: tuple[PortBinding, ...] = ()
    labels: dict[str, Any] = field(default_factory=dict)


@dataclass
class ServiceGraph:
    nodes: dict[str, ServiceNode]

    @classmethod
    def from_compose(cls, compose_path: Path) -> ServiceGraph:
        raw = yaml.safe_load(compose_path.read_text()) or {}
        return cls._from_compose_dict(raw, source=str(compose_path))

    @classmethod
    def from_repo(
        cls,
        root: Path,
        *,
        env_file: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> ServiceGraph:
        """Load graph from merged compose (supports stacks/ include layout)."""
        compose_path = root / "docker-compose.yml"
        if not compose_path.is_file():
            raise FileNotFoundError(compose_path)
        head = yaml.safe_load(compose_path.read_text()) or {}
        if head.get("include"):
            return cls._from_docker_compose_config(root, env_file=env_file, env=env)
        return cls.from_compose(compose_path)

    @classmethod
    def _from_docker_compose_config(
        cls,
        root: Path,
        *,
        env_file: Path | None = None,
        env: dict[str, str] | None = None,
    ) -> ServiceGraph:
        cmd = ["docker", "compose", "-f", str(root / "docker-compose.yml")]
        if env_file and env_file.is_file():
            cmd.extend(["--env-file", str(env_file)])
        cmd.extend(["config", "--format", "json"])
        from toolkit.core.compose.docker import compose_process_environment

        run_env = compose_process_environment(env_file, overrides=env)
        run_env.setdefault("INSTALL_ROOT", str(root))
        proc = subprocess.run(
            cmd,
            cwd=root,
            env=run_env,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "docker compose config failed").strip()
            raise RuntimeError(detail)
        data = json.loads(proc.stdout or "{}")
        return cls._from_compose_dict(data, source="docker compose config")

    @classmethod
    def _from_compose_dict(cls, raw: dict[str, Any], *, source: str) -> ServiceGraph:
        services = raw.get("services") or {}
        if not isinstance(services, dict):
            raise ValueError(f"{source}: services must be a mapping")

        nodes: dict[str, ServiceNode] = {}
        for name, spec in services.items():
            if not isinstance(spec, dict):
                continue
            nodes[name] = ServiceNode(
                name=name,
                image=str(spec.get("image") or ""),
                profiles=_parse_profiles(spec.get("profiles")),
                depends_on=_parse_depends_on(spec.get("depends_on")),
                ports=_parse_ports(spec.get("ports")),
                labels=_parse_homelab_labels(spec),
            )
        return cls(nodes=nodes)

    def service_names(self) -> list[str]:
        return list(self.nodes.keys())

    def topo_layers(self) -> list[list[str]]:
        """Topological layers for stagger waves (depends_on order)."""
        names = set(self.nodes)
        in_degree = {name: 0 for name in names}
        dependents: dict[str, list[str]] = {name: [] for name in names}

        for name, node in self.nodes.items():
            for dep in node.depends_on:
                if dep not in names:
                    continue
                in_degree[name] += 1
                dependents[dep].append(name)

        layers: list[list[str]] = []
        ready = deque(sorted(name for name, degree in in_degree.items() if degree == 0))

        while ready:
            layer = sorted(ready)
            ready.clear()
            layers.append(layer)
            for name in layer:
                for child in dependents[name]:
                    in_degree[child] -= 1
                    if in_degree[child] == 0:
                        ready.append(child)

        remaining = [name for name, degree in in_degree.items() if degree > 0]
        if remaining:
            raise ValueError(f"cycle in depends_on involving: {', '.join(sorted(remaining))}")
        return layers

    def filter_by_profiles(self, active: frozenset[str]) -> ServiceGraph:
        """Keep services with no profiles or at least one matching active profile."""
        filtered: dict[str, ServiceNode] = {}
        for name, node in self.nodes.items():
            if not node.profiles or active.intersection(node.profiles):
                filtered[name] = node
        return ServiceGraph(nodes=filtered)

    def dependency_map(self) -> dict[str, list[str]]:
        """Compose depends_on edges limited to services in this graph."""
        names = set(self.nodes)
        return {name: [dep for dep in node.depends_on if dep in names] for name, node in self.nodes.items()}


def _parse_profiles(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    return ()


def _parse_depends_on(value: Any) -> tuple[str, ...]:
    if not value:
        return ()
    if isinstance(value, list):
        return tuple(str(item) for item in value)
    if isinstance(value, dict):
        return tuple(str(name) for name in value)
    return ()


def _parse_ports(value: Any) -> tuple[PortBinding, ...]:
    if not value or not isinstance(value, list):
        return ()

    bindings: list[PortBinding] = []
    for entry in value:
        if isinstance(entry, str):
            bindings.append(_parse_port_string(entry))
        elif isinstance(entry, dict):
            bindings.append(
                PortBinding(
                    published=str(entry.get("published", "")),
                    target=str(entry.get("target", "")),
                    host_ip=str(entry.get("host_ip", "")),
                )
            )
    return tuple(bindings)


def _parse_port_string(value: str) -> PortBinding:
    parts = value.split(":")
    if len(parts) == 2:
        return PortBinding(published=parts[0], target=parts[1])
    if len(parts) >= 3:
        return PortBinding(host_ip=parts[0], published=parts[1], target=parts[2])
    return PortBinding(published=value)


def _parse_homelab_labels(spec: dict[str, Any]) -> dict[str, Any]:
    homelab = spec.get("x-homelab")
    if isinstance(homelab, dict):
        return dict(homelab)

    labels = spec.get("labels") or {}
    if not isinstance(labels, dict):
        return {}

    return {key: value for key, value in labels.items() if str(key).startswith("x-homelab.")}
