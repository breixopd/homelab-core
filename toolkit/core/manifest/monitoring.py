"""Compile plugin-owned Prometheus scrape contracts into concrete targets."""

from __future__ import annotations

from dataclasses import dataclass

from toolkit.core.config.config import Config
from toolkit.core.manifest.catalog import ServiceCatalog, load_service_catalog


class PrometheusCompilationError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class CompiledPrometheusTarget:
    service: str
    scrape_id: str
    job: str
    path: str
    target: str
    instance: str
    node: str | None
    host_port: int | None


def compile_prometheus_targets(
    cfg: Config,
    catalog: ServiceCatalog | None = None,
) -> tuple[CompiledPrometheusTarget, ...]:
    from toolkit.core.manifest.placement import manifest_node, manifest_runtime_nodes
    from toolkit.core.manifest.routes import service_is_enabled

    selected = catalog or load_service_catalog()
    prometheus = selected.provider("metrics")
    if prometheus is None:
        return ()
    if not service_is_enabled(cfg, prometheus, selected):
        return ()
    compiled: list[CompiledPrometheusTarget] = []
    job_paths: dict[str, str] = {}

    for manifest in selected.manifests:
        if not service_is_enabled(cfg, manifest, selected):
            continue
        for scrape in manifest.prometheus:
            job = scrape.job or manifest.name
            previous_path = job_paths.setdefault(job, scrape.path)
            if previous_path != scrape.path:
                raise PrometheusCompilationError(
                    f"Prometheus job {job!r} declares conflicting paths {previous_path!r} and {scrape.path!r}"
                )
            if scrape.host_integration:
                if scrape.host_port is None:
                    raise PrometheusCompilationError(
                        f"host integration scrape {manifest.name}.{scrape.id} must declare host_port"
                    )
                compiled.extend(
                    CompiledPrometheusTarget(
                        service=manifest.name,
                        scrape_id=scrape.id,
                        job=job,
                        path=scrape.path,
                        target=f"{host.ip}:{scrape.host_port}",
                        instance=host.name,
                        node=None,
                        host_port=scrape.host_port,
                    )
                    for host in cfg.external_hosts
                    if scrape.host_integration in host.services
                )
                continue

            nodes = (
                manifest_runtime_nodes(cfg, manifest, scrape.runtime_service)
                if scrape.runtime_service
                else (manifest_node(cfg, manifest),)
            )
            if scrape.container_port is None:
                raise PrometheusCompilationError(
                    f"container scrape {manifest.name}.{scrape.id} must declare container_port"
                )
            for node in nodes:
                if scrape.host_port is None:
                    raise PrometheusCompilationError(
                        f"Prometheus scrape {manifest.name}.{scrape.id} requires a host port"
                    )
                target = f"{cfg.node_ip(node)}:{scrape.host_port}"
                compiled.append(
                    CompiledPrometheusTarget(
                        service=manifest.name,
                        scrape_id=scrape.id,
                        job=job,
                        path=scrape.path,
                        target=target,
                        instance=node,
                        node=node,
                        host_port=scrape.host_port,
                    )
                )
    return tuple(compiled)
