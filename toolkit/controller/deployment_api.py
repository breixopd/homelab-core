"""Controller-owned deployment readiness and active-operation views."""

from __future__ import annotations

import hashlib
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Literal, cast

from toolkit.controller.contracts import JobKind, JobRecord, MachineId
from toolkit.controller.dashboard_api import read_last_verify_summary
from toolkit.controller.read_models import (
    DeploymentJobSummary,
    DeploymentPreflightCheck,
    DeploymentView,
    VerifyNodeSummary,
)
from toolkit.core.compose.registry import enabled_categories, load_all
from toolkit.core.config.config import Config, ToolkitState, get_state, load_config
from toolkit.core.config.storage import config_path, env_path
from toolkit.core.deploy.deploy_workflow import workflow_step_labels
from toolkit.core.ops.preflight import PreflightItem, preflight_passed, run_preflight

DEPLOYMENT_JOB_KINDS = frozenset({JobKind.DEPLOY, JobKind.RECOVER, JobKind.GENERATE, JobKind.VERIFY})
_PREFLIGHT_TTL_SECONDS = 10.0
_PREFLIGHT_CACHE_LIMIT = 32
_PREFLIGHT_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="preflight")
_PREFLIGHT_LOCK = threading.Lock()
_PreflightKey = tuple[Path, str]
_PREFLIGHT_CACHE: dict[_PreflightKey, tuple[float, list[PreflightItem]]] = {}
_PREFLIGHT_INFLIGHT: dict[_PreflightKey, Future[list[PreflightItem]]] = {}


def _preflight_key(root: Path, cfg: Config) -> _PreflightKey:
    revision = hashlib.sha256(cfg.model_dump_json().encode()).hexdigest()
    return root.resolve(), revision


def _complete_preflight(key: _PreflightKey, future: Future[list[PreflightItem]]) -> None:
    failed = False
    try:
        result = future.result()
    except Exception:
        failed = True
        result = [PreflightItem("preflight_error", "Preflight", False, "Could not complete; refresh shortly")]
    with _PREFLIGHT_LOCK:
        if _PREFLIGHT_INFLIGHT.get(key) is not future:
            return
        _PREFLIGHT_INFLIGHT.pop(key, None)
        _PREFLIGHT_CACHE[key] = (0.0 if failed else time.monotonic(), result)
        while len(_PREFLIGHT_CACHE) > _PREFLIGHT_CACHE_LIMIT:
            oldest = min(_PREFLIGHT_CACHE, key=lambda item: _PREFLIGHT_CACHE[item][0])
            del _PREFLIGHT_CACHE[oldest]


def _bounded_preflight(root: Path, cfg: Config) -> list[PreflightItem]:
    """Return cached readiness while one bounded background refresh runs."""
    root = root.resolve()
    key = _preflight_key(root, cfg)
    now = time.monotonic()
    new_future: Future[list[PreflightItem]] | None = None
    with _PREFLIGHT_LOCK:
        cached = _PREFLIGHT_CACHE.get(key)
        future = _PREFLIGHT_INFLIGHT.get(key)
        if cached and now - cached[0] <= _PREFLIGHT_TTL_SECONDS and future is None:
            return list(cached[1])
        if future is None and len(_PREFLIGHT_INFLIGHT) < _PREFLIGHT_CACHE_LIMIT:
            new_future = _PREFLIGHT_EXECUTOR.submit(run_preflight, root, cfg, bootstrap=True, profile="controller")
            _PREFLIGHT_INFLIGHT[key] = new_future
    if new_future is not None:
        new_future.add_done_callback(lambda completed: _complete_preflight(key, completed))
    if cached:
        return list(cached[1])
    return [PreflightItem("preflight_pending", "Preflight", False, "Still running; refresh shortly")]


def _state(root: Path) -> Literal["uninitialized", "config_only", "ready"]:
    state = get_state(root)
    if state is ToolkitState.UNINITIALIZED:
        return "uninitialized"
    if state is ToolkitState.CONFIG_ONLY:
        return "config_only"
    return "ready"


def _active_jobs(jobs: list[JobRecord], principal: str) -> list[DeploymentJobSummary]:
    return [
        DeploymentJobSummary(
            job_id=job.job_id,
            kind=cast(Literal["DEPLOY", "RECOVER", "GENERATE", "VERIFY"], job.request.kind.value),
            state=cast(Literal["QUEUED", "RUNNING", "CANCEL_REQUESTED"], job.state.value),
            created_at=job.created_at,
            manageable=principal == "local:operator" or job.actor == principal,
        )
        for job in jobs
        if job.request.kind in DEPLOYMENT_JOB_KINDS
    ]


def read_deployment_view(root: Path, jobs: list[JobRecord], principal: str) -> DeploymentView:
    root = root.resolve()
    state = _state(root)
    if state == "uninitialized":
        return DeploymentView(
            state=state,
            enabled_targets=[],
            node_count=0,
            total_services=0,
            category_count=0,
            generated_config_count=0,
            step_labels={},
            preflight=[],
            preflight_ok=False,
            active_jobs=_active_jobs(jobs, principal),
        )

    load_all()
    cfg = load_config(config_path(root))
    categories = enabled_categories(cfg)
    targets = cast(list[MachineId], list(cfg.enabled_nodes))
    preflight_items = _bounded_preflight(root, cfg)
    last_verify = read_last_verify_summary(root)
    target_verify: dict[str, VerifyNodeSummary] = {
        target: last_verify[target] for target in targets if last_verify and target in last_verify
    }
    checks = [
        DeploymentPreflightCheck(
            check_id=item.id,
            label=item.label,
            ok=item.ok,
            detail=item.detail[:500],
        )
        for item in preflight_items
    ]
    return DeploymentView(
        state=state,
        enabled_targets=targets,
        node_count=len(targets),
        total_services=sum(len(category.services(cfg)) for category in categories),
        category_count=len(categories),
        generated_config_count=sum(1 for target in targets if env_path(target, root).is_file()),
        step_labels=workflow_step_labels(cfg),
        preflight=checks,
        preflight_ok=preflight_passed(preflight_items),
        last_verify=target_verify or None,
        active_jobs=_active_jobs(jobs, principal),
    )
