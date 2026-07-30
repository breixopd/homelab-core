"""Container rightsizing from node-scoped cAdvisor telemetry."""

from __future__ import annotations

import json
import math
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from pydantic import BaseModel, ConfigDict, Field

from toolkit.core.state.files import atomic_write_json

if TYPE_CHECKING:
    from toolkit.core.config.config import Config

RightsizeReconciler = Callable[["Config", tuple[str, ...]], bool]
_MAX_RIGHTSIZE_STATE_BYTES = 64 * 1024
_RIGHTSIZE_STATE_KEY = re.compile(r"^[a-z0-9][a-z0-9-]{0,62}/[a-z0-9][a-z0-9-]{0,62}$")


class RightsizeApplyError(RuntimeError):
    pass


# Stateful services are now declared per-service via `stateful: true` in
# service.yaml (validated by the strict manifest catalog). The rightsize engine reads
# that field directly — no hardcoded fallback list.


@dataclass(frozen=True, slots=True)
class RightsizeProposal:
    vm: str
    service: str
    current_mem_mb: int
    proposed_mem_mb: int
    current_cpus: float
    proposed_cpus: float
    p95_mem_mb: float
    p95_cpu_pct: float
    reason: str
    safe_to_apply: bool
    stateful: bool = False
    blocked_reason: str = ""

    @property
    def change_pct(self) -> float:
        if self.current_mem_mb == 0:
            return 0.0
        return (self.proposed_mem_mb - self.current_mem_mb) / self.current_mem_mb * 100.0


class RightsizeApprovalPayload(BaseModel):
    """Validated action data stored with a rightsizing approval."""

    model_config = ConfigDict(extra="forbid")

    node: str = Field(pattern=r"^[a-z0-9][a-z0-9-]{0,62}$")
    current_mem_mb: int = Field(ge=64, le=4_194_304)
    proposed_mem_mb: int = Field(ge=64, le=4_194_304)
    current_cpus: float = Field(ge=0.05, le=256)
    proposed_cpus: float = Field(ge=0.05, le=256)
    p95_mem_mb: float = Field(ge=0, le=4_194_304)
    p95_cpu_pct: float = Field(ge=0, le=25_600)
    stateful: bool
    reason: str = Field(max_length=500)

    @classmethod
    def from_proposal(cls, proposal: RightsizeProposal) -> RightsizeApprovalPayload:
        return cls(
            node=proposal.vm,
            current_mem_mb=proposal.current_mem_mb,
            proposed_mem_mb=proposal.proposed_mem_mb,
            current_cpus=proposal.current_cpus,
            proposed_cpus=proposal.proposed_cpus,
            p95_mem_mb=proposal.p95_mem_mb,
            p95_cpu_pct=proposal.p95_cpu_pct,
            stateful=proposal.stateful,
            reason=proposal.reason,
        )


@dataclass(frozen=True, slots=True)
class RightsizeConfig:
    """Tunable knobs for the rightsize policy.

    Defaults encode the locked guardrails from the spec:
    headroom 1.3×, max 25%/step, 24h cooldown.
    """

    enabled: bool = True
    headroom_factor: float = 1.3
    max_step_pct: float = 25.0
    cooldown_hours: int = 24
    min_telemetry_window_days: int = 1
    default_telemetry_window_days: int = 7
    minimum_samples_per_minute: int = 2


def rightsize_config_from_desired_state(config: Config) -> RightsizeConfig:
    """Build policy from settings declared by the control-plane service."""
    from toolkit.core.manifest.settings import service_setting_bool, service_setting_int

    return RightsizeConfig(
        enabled=service_setting_bool(config, "homelab-ui", "rightsize-enabled"),
        headroom_factor=service_setting_int(config, "homelab-ui", "rightsize-headroom-percent") / 100,
        max_step_pct=service_setting_int(config, "homelab-ui", "rightsize-max-step-percent"),
        cooldown_hours=service_setting_int(config, "homelab-ui", "rightsize-cooldown-hours"),
        default_telemetry_window_days=service_setting_int(config, "homelab-ui", "rightsize-telemetry-days"),
        minimum_samples_per_minute=service_setting_int(
            config,
            "homelab-ui",
            "rightsize-minimum-samples-per-minute",
        ),
    )


# --- pure guardrail helpers (unit-testable in isolation) -------------------


def is_within_max_step(current: int | float, proposed: int | float, max_step_pct: float) -> bool:
    """True iff |proposed - current| / current ≤ max_step_pct."""
    if current == 0:
        return False
    change_pct = abs(proposed - current) / current * 100.0
    return change_pct <= max_step_pct


def respects_floor(proposed: int, floor_mb: int) -> bool:
    """True iff proposed ≥ floor."""
    return proposed >= floor_mb


def can_auto_apply(
    p: RightsizeProposal,
    cfg: RightsizeConfig,
    last_applied_at: float | None,
    now: float,
) -> bool:
    """A proposal is auto-appliable iff: kills-switch on, shrinks only, ≤max_step,
    ≥floor, NOT stateful, cooldown elapsed.

    Growth and stateful changes require explicit operator approval. Cooldown
    and policy guardrails defer work without creating an override path.
    """
    if not cfg.enabled:
        return False
    if p.stateful:
        return False
    memory_changed = p.proposed_mem_mb != p.current_mem_mb
    cpu_changed = not math.isclose(p.proposed_cpus, p.current_cpus, rel_tol=0, abs_tol=0.001)
    if not memory_changed and not cpu_changed:
        return False
    if p.proposed_mem_mb > p.current_mem_mb:
        return False
    if memory_changed and not is_within_max_step(p.current_mem_mb, p.proposed_mem_mb, cfg.max_step_pct):
        return False
    if p.current_cpus > 0:
        if p.proposed_cpus > p.current_cpus:
            return False
        if cpu_changed and not is_within_max_step(p.current_cpus, p.proposed_cpus, cfg.max_step_pct):
            return False
    if last_applied_at is not None and (now - last_applied_at) < cfg.cooldown_hours * 3600:
        return False
    return True


# --- parsing helpers --------------------------------------------------------


def _parse_mem_to_mb(value: str | int | float) -> int:
    """Parse '512m' / '1g' / '2048' / '768K' → MiB."""
    if isinstance(value, int | float):
        return int(value)
    v = str(value).strip().lower()
    try:
        if v.endswith("g"):
            return int(float(v[:-1]) * 1024)
        if v.endswith("m"):
            return int(float(v[:-1]))
        if v.endswith("k"):
            return int(float(v[:-1]) / 1024)
        return int(float(v))
    except ValueError:
        return 0


def _load_current_limits(vm: str, root: Path | str = ".") -> dict[str, dict[str, Any]]:
    """Read generated/<vm>/compose.limits.yml. Returns {service: {mem_mb, cpus}}."""
    path = Path(root) / "generated" / vm / "compose.limits.yml"
    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    services = data.get("services", {})
    result: dict[str, dict[str, Any]] = {}
    for name, spec in services.items():
        spec = spec or {}
        result[name] = {
            "mem_mb": _parse_mem_to_mb(spec.get("mem_limit", "0m")),
            "cpus": float(spec.get("cpus", 0.0) or 0.0),
        }
    return result


def _load_service_metadata(root: Path | str = ".") -> dict[str, dict[str, Any]]:
    """Read memory_floor_mb + stateful per service from service.yaml files."""
    try:
        from toolkit.core.config.service_metadata import (
            get_service_resource_policy,
            managed_runtime_service_names,
        )
    except Exception:
        return {}
    result: dict[str, dict[str, Any]] = {}
    for name in managed_runtime_service_names():
        stateful, memory_floor_mb, cpu_floor = get_service_resource_policy(name)
        result[name] = {
            "stateful": stateful,
            "memory_floor_mb": memory_floor_mb,
            "cpu_floor": cpu_floor,
        }
    return result


def _rightsize_state_path(root: Path | str) -> Path:
    return Path(root).resolve() / ".homelab-state" / "rightsize.json"


def _load_rightsize_state(root: Path | str) -> dict[str, float]:
    path = _rightsize_state_path(root)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW)
    except FileNotFoundError:
        return {}
    except OSError as exc:
        raise RuntimeError("rightsizing state is unreadable; refusing automatic changes") from exc
    try:
        content = os.read(descriptor, _MAX_RIGHTSIZE_STATE_BYTES + 1)
    finally:
        os.close(descriptor)
    if len(content) > _MAX_RIGHTSIZE_STATE_BYTES:
        raise RuntimeError("rightsizing state is unreadable; refusing automatic changes")
    try:
        payload = json.loads(content)
        values = payload["last_applied_at"]
        if not isinstance(values, dict):
            raise ValueError
        result = {str(key): float(value) for key, value in values.items()}
        if any(
            not _RIGHTSIZE_STATE_KEY.fullmatch(key) or not math.isfinite(value) or value < 0
            for key, value in result.items()
        ):
            raise ValueError
        return result
    except (KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("rightsizing state is unreadable; refusing automatic changes") from exc


def _save_rightsize_state(root: Path | str, values: dict[str, float]) -> None:
    path = _rightsize_state_path(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    if any(
        not _RIGHTSIZE_STATE_KEY.fullmatch(key) or not math.isfinite(value) or value < 0
        for key, value in values.items()
    ):
        raise ValueError("rightsizing state contains an invalid timestamp")
    atomic_write_json(path, {"last_applied_at": values}, mode=0o600)


def reconcile_rightsize_nodes(
    root: Path | str,
    config: Config,
    nodes: tuple[str, ...],
    *,
    on_log: Callable[[str], None] | None = None,
) -> bool:
    """Run the normal targeted deploy and verification path for resource changes."""
    import asyncio

    from toolkit.core.deploy.deploy_workflow import run_deploy_workflow

    emit = on_log or (lambda _message: None)
    result = asyncio.run(
        run_deploy_workflow(
            Path(root),
            config,
            on_log=emit,
            on_step=lambda step, state: emit(f"{step}: {state}"),
            targets=nodes,
            skip_infra=True,
            skip_dns=True,
        )
    )
    return result.success


# --- telemetry source -------------------------------------------------------


def _resolve_prometheus_url(root: Path | str = ".") -> str | None:
    """Find the Prometheus URL from its service placement."""
    try:
        from toolkit.core.config.config import load_config
        from toolkit.core.manifest.catalog import load_service_catalog
        from toolkit.core.manifest.placement import service_address

        cfg = load_config(Path(root) / "config.yaml")
        metrics_service = load_service_catalog().require_provider("metrics").name
        host = service_address(cfg, metrics_service) if cfg.is_multi_node else "localhost"
        return f"http://{host}:9090"
    except Exception:
        return None


def _query_prometheus_p95(vm: str, *, root: Path | str = ".", window_days: int = 7) -> dict[str, dict[str, Any]]:
    """Query Prometheus cAdvisor for p95 memory (MiB) + p95 CPU (%) per container.

    Returns {service: {p95_mem_mb, p95_cpu_pct}}. Best-effort: any HTTP/parse
    failure returns {} so callers degrade gracefully to "no proposals".
    Tests stub this function — no real network in unit tests.
    """
    prometheus_url = _resolve_prometheus_url(root)
    if prometheus_url is None:
        return {}
    node = vm.replace("\\", "\\\\").replace('"', '\\"')
    selector = f'instance="{node}",container_label_com_docker_compose_service!="",image!=""'
    label = "container_label_com_docker_compose_service"
    queries = {
        "mem": (
            f"max by ({label}) (quantile_over_time(0.95, container_memory_usage_bytes{{{selector}}}[{window_days}d]))"
        ),
        "cpu": (
            f"max by ({label}) (quantile_over_time(0.95, "
            f"rate(container_cpu_usage_seconds_total{{{selector}}}[5m])"
            f"[{window_days}d:5m]))"
        ),
        "current_mem": f"max by ({label}) (container_spec_memory_limit_bytes{{{selector}}})",
        "current_cpu": (
            f"max by ({label}) (container_spec_cpu_quota{{{selector}}} / container_spec_cpu_period{{{selector}}})"
        ),
        "samples": (f"max by ({label}) (count_over_time(container_memory_usage_bytes{{{selector}}}[{window_days}d]))"),
    }
    raw: dict[str, dict[str, float]] = {}
    for key, query in queries.items():
        url = f"{prometheus_url}/api/v1/query?query={urllib.parse.quote(query)}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError, OSError):
            return {}
        try:
            result = payload["data"]["result"]
            if payload.get("status") not in (None, "success") or not isinstance(result, list):
                return {}
            for series in result:
                name = series["metric"].get(label, "")
                value = float(series["value"][1])
                if not name or not math.isfinite(value) or value < 0:
                    return {}
                raw.setdefault(name, {})[key] = value
        except (KeyError, IndexError, TypeError, ValueError):
            return {}
    results: dict[str, dict[str, Any]] = {}
    for name, vals in raw.items():
        if set(vals) != set(queries) or vals["current_mem"] <= 0 or vals["current_cpu"] <= 0:
            continue
        results[name] = {
            "p95_mem_mb": vals.get("mem", 0.0) / (1024 * 1024),
            "p95_cpu_pct": vals.get("cpu", 0.0) * 100.0,
            "sample_count": int(vals.get("samples", 0.0)),
            "current_mem_mb": int(vals.get("current_mem", 0.0) / (1024 * 1024)),
            "current_cpus": vals.get("current_cpu", 0.0),
        }
    return results


# --- proposal computation ---------------------------------------------------


def compute_rightsize_proposals(
    vm: str,
    *,
    root: Path | str = ".",
    cfg: RightsizeConfig | None = None,
    telemetry: dict | None = None,
) -> list[RightsizeProposal]:
    """Compute proposals for ``vm`` from telemetry.

    ``telemetry`` (when provided): ``{service: {p95_mem_mb, p95_cpu_pct, stateful,
    current_mem_mb, current_cpus, memory_floor_mb}}``. When None, queried from
    Prometheus (which returns {} in tests/dev → no proposals).
    """
    cfg = cfg or RightsizeConfig()
    if not cfg.enabled:
        return []

    if telemetry is None:
        telemetry = _query_prometheus_p95(vm, root=root, window_days=cfg.default_telemetry_window_days)
    if not telemetry:
        return []

    current_limits = _load_current_limits(vm, root)
    meta = _load_service_metadata(root)
    last_applied = _load_rightsize_state(root)
    proposals: list[RightsizeProposal] = []
    now = time.time()

    for service, stats in telemetry.items():
        # Resolve current mem/cpus from explicit telemetry, else from the overlay.
        cur_mem = int(stats.get("current_mem_mb") or current_limits.get(service, {}).get("mem_mb", 0))
        cur_cpus = float(stats.get("current_cpus") or current_limits.get(service, {}).get("cpus", 0.0))
        if cur_mem == 0:
            continue  # nothing to compare against

        sample_count = int(stats.get("sample_count", 0))
        telemetry_days = max(cfg.min_telemetry_window_days, cfg.default_telemetry_window_days)
        minimum_samples = telemetry_days * 24 * 60 * cfg.minimum_samples_per_minute
        if sample_count < minimum_samples:
            continue

        svc_meta = meta.get(service, {"stateful": False, "memory_floor_mb": 128, "cpu_floor": 0.1})
        stateful = bool(stats.get("stateful", svc_meta["stateful"]))
        floor_mb = int(stats.get("memory_floor_mb", svc_meta["memory_floor_mb"]))
        cpu_floor = float(stats.get("cpu_floor", svc_meta["cpu_floor"]))

        p95_mem = float(stats.get("p95_mem_mb", 0.0))
        p95_cpu = float(stats.get("p95_cpu_pct", 0.0))

        # Move toward observed demand in bounded steps so a severely idle
        # stateless service converges without requiring manual approval.
        proposed_mem = max(floor_mb, int(math.ceil(p95_mem * cfg.headroom_factor)))
        if stateful:
            proposed_mem = max(proposed_mem, cur_mem)
        elif proposed_mem < cur_mem:
            proposed_mem = max(proposed_mem, int(math.ceil(cur_mem * (1 - cfg.max_step_pct / 100))))

        # Proposed cpus use the same headroom policy as memory.
        # p95_cpu_pct is % of one core (from cAdvisor ratio * 100).
        proposed_cpus = max(cpu_floor, round(p95_cpu * cfg.headroom_factor / 100.0, 2))
        if not stateful and proposed_cpus < cur_cpus:
            minimum_cpu_step = math.ceil(cur_cpus * (1 - cfg.max_step_pct / 100) * 100) / 100
            proposed_cpus = max(proposed_cpus, minimum_cpu_step)
        if proposed_mem == cur_mem and math.isclose(proposed_cpus, cur_cpus, rel_tol=0, abs_tol=0.001):
            continue

        # Determine auto-applicability (safe only). States fields are computed
        # honestly; the safe_to_apply flag reflects the guardrail verdict.
        safe = can_auto_apply_proto(
            vm=vm,
            service=service,
            cur_mem=cur_mem,
            proposed_mem=proposed_mem,
            cur_cpus=cur_cpus,
            proposed_cpus=proposed_cpus,
            stateful=stateful,
            cfg=cfg,
            last_applied_at=last_applied.get(f"{vm}/{service}"),
            now=now,
        )
        blocked_reason = ""
        last_applied_at = last_applied.get(f"{vm}/{service}")
        if not safe:
            if stateful:
                blocked_reason = "stateful-service"
            elif proposed_mem > cur_mem or proposed_cpus > cur_cpus:
                blocked_reason = "capacity-growth"
            elif last_applied_at is not None and (now - last_applied_at) < cfg.cooldown_hours * 3600:
                blocked_reason = "cooldown"
            else:
                blocked_reason = "policy-guardrail"

        proposals.append(
            RightsizeProposal(
                vm=vm,
                service=service,
                current_mem_mb=cur_mem,
                proposed_mem_mb=proposed_mem,
                current_cpus=cur_cpus,
                proposed_cpus=proposed_cpus,
                p95_mem_mb=p95_mem,
                p95_cpu_pct=p95_cpu,
                reason=f"p95 mem={p95_mem:.0f}MB cpu={p95_cpu:.1f}% window={cfg.default_telemetry_window_days}d",
                safe_to_apply=safe,
                stateful=stateful,
                blocked_reason=blocked_reason,
            )
        )
    return proposals


def can_auto_apply_proto(
    *,
    vm: str,
    service: str,
    cur_mem: int,
    proposed_mem: int,
    cur_cpus: float,
    proposed_cpus: float,
    stateful: bool,
    cfg: RightsizeConfig,
    last_applied_at: float | None,
    now: float,
) -> bool:
    """Build a throwaway proposal and run the policy on it.

    Kept separate from :func:`can_auto_apply` because the latter takes a fully
    constructed proposal (post-decision) — this helper runs the policy during
    construction with the same numbers.
    """
    p = RightsizeProposal(
        vm=vm,
        service=service,
        current_mem_mb=cur_mem,
        proposed_mem_mb=proposed_mem,
        current_cpus=cur_cpus,
        proposed_cpus=proposed_cpus,
        p95_mem_mb=0.0,
        p95_cpu_pct=0.0,
        reason="proto",
        safe_to_apply=False,
        stateful=stateful,
    )
    return can_auto_apply(p, cfg, last_applied_at=last_applied_at, now=now)


# --- remedy: safe-apply ------------------------------------------------------


def apply_rightsize_proposals(
    proposals: list[RightsizeProposal],
    *,
    root: Path | str = ".",
    reconcile: RightsizeReconciler | None = None,
    approval_granted: bool = False,
) -> list[RightsizeProposal]:
    """Persist authorized proposals, reconcile them, and roll back on failure."""
    from toolkit.core.config.config import Config, load_config, save_config
    from toolkit.core.config.mutations import configuration_mutation
    from toolkit.core.config.storage import config_path
    from toolkit.core.deploy.compose_limits import write_compose_limits
    from toolkit.core.machines.models import MachineResourceLimit
    from toolkit.core.state.audit_log import AuditAction, audit

    root_path = Path(root)
    applied = [proposal for proposal in proposals if proposal.safe_to_apply or approval_granted]
    if not applied:
        return []
    affected_nodes = sorted({proposal.vm for proposal in applied})
    now = time.time()

    with configuration_mutation(root_path, "rightsize-apply"):
        current = load_config(config_path(root_path))
        rollback = current.model_copy(deep=True)
        for proposal in applied:
            observed = _load_current_limits(proposal.vm, root_path).get(proposal.service)
            if not observed:
                raise RightsizeApplyError(
                    f"current enforced limit is unavailable for {proposal.vm}/{proposal.service}; regenerate first"
                )
            matches_current = observed["mem_mb"] == proposal.current_mem_mb and math.isclose(
                observed["cpus"], proposal.current_cpus, rel_tol=0, abs_tol=0.001
            )
            matches_proposed = observed["mem_mb"] == proposal.proposed_mem_mb and math.isclose(
                observed["cpus"], proposal.proposed_cpus, rel_tol=0, abs_tol=0.001
            )
            if matches_proposed:
                rollback.machines[proposal.vm].resource_limits[proposal.service] = MachineResourceLimit(
                    memory_mb=proposal.current_mem_mb,
                    cpus=proposal.current_cpus,
                )
            elif not matches_current:
                raise RightsizeApplyError(
                    f"resource proposal for {proposal.vm}/{proposal.service} is stale; recompute before applying"
                )
        rollback = Config.model_validate(rollback.model_dump(mode="python"))
        updated = current.model_copy(deep=True)
        for proposal in applied:
            if proposal.vm not in updated.enabled_nodes:
                raise ValueError(f"rightsizing target {proposal.vm!r} is not enabled")
            updated.machines[proposal.vm].resource_limits[proposal.service] = MachineResourceLimit(
                memory_mb=proposal.proposed_mem_mb,
                cpus=proposal.proposed_cpus,
            )
        validated = Config.model_validate(updated.model_dump(mode="python"))
        save_config(validated, config_path(root_path), actor="watchdog-rightsize")
        try:
            for vm in affected_nodes:
                if write_compose_limits(validated, vm, root_path) is None:
                    raise RuntimeError(f"could not regenerate resource limits for {vm!r}")
        except Exception:
            save_config(rollback, config_path(root_path), actor="watchdog-rightsize-rollback")
            for vm in affected_nodes:
                write_compose_limits(rollback, vm, root_path)
            raise

        if reconcile is not None:
            try:
                converged = reconcile(validated, tuple(affected_nodes))
            except Exception:
                converged = False
            if not converged:
                save_config(rollback, config_path(root_path), actor="watchdog-rightsize-rollback")
                for vm in affected_nodes:
                    write_compose_limits(rollback, vm, root_path)
                try:
                    rollback_converged = reconcile(rollback, tuple(affected_nodes))
                except Exception:
                    rollback_converged = False
                if not rollback_converged:
                    raise RightsizeApplyError("resource change and automatic rollback both failed")
                raise RightsizeApplyError("resource change did not converge; previous resource limits were restored")

        try:
            state = _load_rightsize_state(root_path)
            for proposal in applied:
                state[f"{proposal.vm}/{proposal.service}"] = now
            _save_rightsize_state(root_path, state)
        except Exception as exc:
            save_config(rollback, config_path(root_path), actor="watchdog-rightsize-rollback")
            for vm in affected_nodes:
                write_compose_limits(rollback, vm, root_path)
            try:
                rollback_converged = reconcile is None or reconcile(rollback, tuple(affected_nodes))
            except Exception:
                rollback_converged = False
            if not rollback_converged:
                raise RightsizeApplyError("cooldown state failed and automatic resource rollback also failed") from exc
            raise RightsizeApplyError("cooldown state could not be persisted; resource changes were restored") from exc

    for vm in affected_nodes:
        count = sum(proposal.vm == vm for proposal in applied)
        audit(
            root_path,
            AuditAction.WATCHDOG,
            actor="watchdog-rightsize",
            ok=True,
            detail=f"applied {count} verified resource change(s) on {vm}",
            vm=vm,
        )
    return applied


def execute_approved_rightsize(
    *,
    root: Path | str,
    approval: Any,
    reconcile: RightsizeReconciler | None = None,
    on_log: Callable[[str], None] | None = None,
) -> list[RightsizeProposal]:
    """Validate and execute an approved rightsizing request."""
    from toolkit.core.ops.approvals import ApprovalKind, ApprovalStatus

    if approval.kind is not ApprovalKind.RIGHTSIZE or approval.status is not ApprovalStatus.APPROVED:
        raise RightsizeApplyError("rightsizing execution requires an approved rightsizing request")
    try:
        payload = RightsizeApprovalPayload.model_validate(approval.payload)
    except Exception as exc:
        raise RightsizeApplyError("rightsizing approval payload is invalid") from exc
    proposal = RightsizeProposal(
        vm=payload.node,
        service=approval.service,
        current_mem_mb=payload.current_mem_mb,
        proposed_mem_mb=payload.proposed_mem_mb,
        current_cpus=payload.current_cpus,
        proposed_cpus=payload.proposed_cpus,
        p95_mem_mb=payload.p95_mem_mb,
        p95_cpu_pct=payload.p95_cpu_pct,
        reason=payload.reason,
        safe_to_apply=False,
        stateful=payload.stateful,
        blocked_reason="approval-required",
    )
    reconciler = reconcile or (lambda config, nodes: reconcile_rightsize_nodes(root, config, nodes, on_log=on_log))
    try:
        return apply_rightsize_proposals(
            [proposal],
            root=root,
            reconcile=reconciler,
            approval_granted=True,
        )
    except RightsizeApplyError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise RightsizeApplyError("approved rightsizing could not be applied safely") from exc


__all__ = [
    "RightsizeApplyError",
    "RightsizeApprovalPayload",
    "RightsizeConfig",
    "RightsizeProposal",
    "apply_rightsize_proposals",
    "can_auto_apply",
    "can_auto_apply_proto",
    "compute_rightsize_proposals",
    "execute_approved_rightsize",
    "is_within_max_step",
    "respects_floor",
    "reconcile_rightsize_nodes",
    "rightsize_config_from_desired_state",
]
