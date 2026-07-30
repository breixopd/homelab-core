from __future__ import annotations

import threading
from pathlib import Path
from types import SimpleNamespace

import pytest
import toolkit.controller.deployment_api as deployment_api
from toolkit.controller.contracts import DeployOperation, JobKind, JobRequest, RecoverOperation, VerifyOperation
from toolkit.controller.deployment_api import DEPLOYMENT_JOB_KINDS, read_deployment_view
from toolkit.controller.store import ControllerStore, JobQueueLimitError
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path, env_path
from toolkit.core.ops.preflight import PreflightItem


@pytest.fixture(autouse=True)
def clear_preflight_state():
    with deployment_api._PREFLIGHT_LOCK:
        futures = list(deployment_api._PREFLIGHT_INFLIGHT.values())
        deployment_api._PREFLIGHT_CACHE.clear()
        deployment_api._PREFLIGHT_INFLIGHT.clear()
    for future in futures:
        try:
            future.result(timeout=2)
        except Exception:
            pass
    with deployment_api._PREFLIGHT_LOCK:
        deployment_api._PREFLIGHT_CACHE.clear()
    yield
    with deployment_api._PREFLIGHT_LOCK:
        futures = list(deployment_api._PREFLIGHT_INFLIGHT.values())
        deployment_api._PREFLIGHT_CACHE.clear()
        deployment_api._PREFLIGHT_INFLIGHT.clear()
    for future in futures:
        try:
            future.result(timeout=2)
        except Exception:
            pass
    with deployment_api._PREFLIGHT_LOCK:
        deployment_api._PREFLIGHT_CACHE.clear()


def test_preflight_single_flight_and_pending(tmp_path: Path, monkeypatch) -> None:
    started, release = threading.Event(), threading.Event()
    calls = 0

    def preflight(_root, _cfg, **_kwargs):
        nonlocal calls
        calls += 1
        started.set()
        release.wait(2)
        return [PreflightItem("ok", "OK", True)]

    monkeypatch.setattr(deployment_api, "run_preflight", preflight)
    cfg = Config()
    first = deployment_api._bounded_preflight(tmp_path, cfg)
    second = deployment_api._bounded_preflight(tmp_path, cfg)
    assert first[0].id == second[0].id == "preflight_pending"
    assert started.wait(1)
    assert calls == 1
    release.set()
    for _ in range(20):
        result = deployment_api._bounded_preflight(tmp_path, cfg)
        if result[0].id == "ok":
            break
        threading.Event().wait(0.01)
    assert result[0].id == "ok"
    assert deployment_api._bounded_preflight(tmp_path, cfg)[0].id == "ok"


def test_preflight_serves_stale_cache_during_refresh(tmp_path: Path, monkeypatch) -> None:
    started, release = threading.Event(), threading.Event()
    cfg = Config()
    root = tmp_path.resolve()
    stale = PreflightItem("stale", "Stale", True)
    with deployment_api._PREFLIGHT_LOCK:
        deployment_api._PREFLIGHT_CACHE[deployment_api._preflight_key(root, cfg)] = (0.0, [stale])

    def preflight(_root, _cfg, **_kwargs):
        started.set()
        release.wait(2)
        return [PreflightItem("fresh", "Fresh", True)]

    monkeypatch.setattr(deployment_api, "run_preflight", preflight)
    assert deployment_api._bounded_preflight(root, cfg)[0].id == "stale"
    assert started.wait(1)
    assert deployment_api._bounded_preflight(root, cfg)[0].id == "stale"
    release.set()
    for _ in range(20):
        result = deployment_api._bounded_preflight(root, cfg)
        if result[0].id == "fresh":
            break
        threading.Event().wait(0.01)
    assert result[0].id == "fresh"


def test_preflight_exceptions_become_safe_failure_and_retry(tmp_path: Path, monkeypatch) -> None:
    cfg = Config()
    calls = 0

    def preflight(*_args, **_kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("boom")
        return [PreflightItem("recovered", "Recovered", True)]

    monkeypatch.setattr(deployment_api, "run_preflight", preflight)
    result = deployment_api._bounded_preflight(tmp_path, cfg)
    for _ in range(20):
        if result[0].id == "preflight_error":
            break
        result = deployment_api._bounded_preflight(tmp_path, cfg)
        threading.Event().wait(0.01)
    assert result[0].id == "preflight_error"
    for _ in range(20):
        result = deployment_api._bounded_preflight(tmp_path, cfg)
        if result[0].id == "recovered":
            break
        threading.Event().wait(0.01)
    assert result[0].id == "recovered"
    assert calls == 2


def test_preflight_cache_is_scoped_to_config_revision(tmp_path: Path, monkeypatch) -> None:
    calls = 0

    def preflight(_root, cfg, **_kwargs):
        nonlocal calls
        calls += 1
        return [PreflightItem(cfg.domain, cfg.domain, True)]

    monkeypatch.setattr(deployment_api, "run_preflight", preflight)
    first_cfg = Config(domain="first.example")
    second_cfg = Config(domain="second.example")
    result = deployment_api._bounded_preflight(tmp_path, first_cfg)
    for _ in range(20):
        result = deployment_api._bounded_preflight(tmp_path, first_cfg)
        if result[0].id == first_cfg.domain:
            break
        threading.Event().wait(0.01)
    assert result[0].id == first_cfg.domain

    result = deployment_api._bounded_preflight(tmp_path, second_cfg)
    assert result[0].id == "preflight_pending"
    for _ in range(20):
        result = deployment_api._bounded_preflight(tmp_path, second_cfg)
        if result[0].id == second_cfg.domain:
            break
        threading.Event().wait(0.01)
    assert result[0].id == second_cfg.domain
    assert calls == 2


def test_deployment_view_is_typed_and_runs_preflight_once(tmp_path: Path, monkeypatch) -> None:
    cfg = Config(proxmox={"provision_machines": False})
    save_config(cfg, config_path(tmp_path))
    for target in cfg.enabled_nodes:
        path = env_path(target, tmp_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("generated=true\n")

    calls = 0
    profiles: list[dict[str, object]] = []

    def preflight(_root, _cfg, **kwargs):
        nonlocal calls
        calls += 1
        profiles.append(kwargs)
        return [PreflightItem("config", "Config", True)]

    category = SimpleNamespace(services=lambda _cfg: ["one", "two"])
    monkeypatch.setattr("toolkit.controller.deployment_api.run_preflight", preflight)
    monkeypatch.setattr("toolkit.controller.deployment_api.enabled_categories", lambda _cfg: [category])
    monkeypatch.setattr("toolkit.controller.deployment_api.workflow_step_labels", lambda _cfg: {"verify": "Verify"})

    view = read_deployment_view(tmp_path, [], "mtls:homelab-ui")

    for _ in range(20):
        if view.preflight_ok:
            break
        threading.Event().wait(0.01)
        view = read_deployment_view(tmp_path, [], "mtls:homelab-ui")

    assert calls == 1
    assert view.state == "ready"
    assert view.node_count == len(cfg.enabled_nodes)
    assert view.generated_config_count == len(cfg.enabled_nodes)
    assert view.total_services == 2
    assert view.preflight_ok is True
    assert view.preflight[0].check_id == "config"
    assert profiles == [{"bootstrap": True, "profile": "controller"}]


def test_active_deployment_jobs_are_principal_scoped_and_kind_filtered(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    visible = store.create_job(
        JobRequest(idempotency_key="a" * 16, operation=DeployOperation()),
        principal="mtls:homelab-ui",
    )
    store.create_job(
        JobRequest(idempotency_key="b" * 16, operation=DeployOperation()),
        principal="mtls:other-ui",
    )
    store.create_job(
        JobRequest(idempotency_key="c" * 16, operation=VerifyOperation()),
        principal="mtls:homelab-ui",
    )

    jobs = store.active_jobs(
        principal="mtls:homelab-ui",
        kinds=frozenset({visible.request.kind}),
    )

    assert [job.job_id for job in jobs] == [visible.job_id]
    assert visible.request.kind in DEPLOYMENT_JOB_KINDS

    system_jobs = store.active_jobs(principal=None, kinds=DEPLOYMENT_JOB_KINDS)
    assert {job.actor for job in system_jobs} == {"mtls:homelab-ui", "mtls:other-ui"}
    view = read_deployment_view(tmp_path, system_jobs, "mtls:homelab-ui")
    manageability = {job.job_id: job.manageable for job in view.active_jobs}
    assert manageability[visible.job_id] is True
    assert any(not manageable for manageable in manageability.values())


def test_mutating_deployment_family_has_one_global_active_slot(tmp_path: Path) -> None:
    store = ControllerStore(tmp_path / "controller.db")
    kinds = frozenset({JobKind.DEPLOY, JobKind.RECOVER, JobKind.GENERATE})
    store.submit_job(
        JobRequest(idempotency_key="deploy-first-12345", operation=DeployOperation()),
        principal="mtls:homelab-ui",
        active_limit=1,
        active_kinds=kinds,
    )

    with pytest.raises(JobQueueLimitError):
        store.submit_job(
            JobRequest(idempotency_key="recover-second-123", operation=RecoverOperation()),
            principal="local:operator",
            active_limit=1,
            active_kinds=kinds,
        )
