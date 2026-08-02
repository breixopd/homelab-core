from __future__ import annotations

import re
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from toolkit.controller.client import ControllerUnavailableError
from toolkit.controller.contracts import JobRecord, JobState
from toolkit.controller.desired_state_api import read_machines_view
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.storage import config_path
from toolkit.core.machines import MachineSpec
from toolkit.webui.routers.machines import _local_redirect

pytestmark = pytest.mark.anyio


def test_machine_redirects_reject_absolute_and_backslash_urls() -> None:
    assert _local_redirect("/machines/worker-east") == "/machines/worker-east"
    assert _local_redirect("https://attacker.example/") == "/"
    assert _local_redirect(r"/machines/worker-east\\retire") == "/"


class MachinesController:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.created = []
        self.updated = []
        self.removed = []
        self.jobs = []
        self.generation_available = True
        self.plans = []
        self.approvals = []

    def machines_view(self):
        return read_machines_view(self.root)

    def create_machine(self, request):
        self.created.append(request)
        return self.machines_view()

    def update_machine(self, machine_id, request):
        self.updated.append((machine_id, request))
        return self.machines_view()

    def remove_machine(self, request):
        self.removed.append(request)
        return self.machines_view()

    def submit(self, request) -> JobRecord:
        if not self.generation_available:
            raise ControllerUnavailableError
        self.jobs.append(request)
        now = datetime(2026, 7, 15, tzinfo=UTC)
        return JobRecord(
            job_id="job-generate-machines",
            request=request,
            state=JobState.QUEUED,
            actor="ui:homelab-ui",
            created_at=now,
            updated_at=now,
        )

    def create_destruction_plan(self, request):
        self.plans.append(request)
        return SimpleNamespace(
            plan_id="plan-retire-worker-east",
            plan_hash="a" * 64,
            spec=SimpleNamespace(
                action="retire_machine",
                scopes=["worker-east"],
                config_revision=read_machines_view(self.root).revision,
                checkpoint_id="b" * 32,
                checkpoint_verified_at=datetime(2026, 7, 15, tzinfo=UTC),
            ),
        )

    def get_plan(self, plan_id):
        assert plan_id == "plan-retire-worker-east"
        return self.create_destruction_plan(SimpleNamespace(action="retire_machine", scopes=["worker-east"]))

    def approve_plan(self, plan_id, *, plan_hash, confirmation):
        self.approvals.append((plan_id, plan_hash, confirmation))
        return SimpleNamespace(token="approval-token-123456")

    def close(self) -> None:
        return None


def _app(tmp_path: Path, monkeypatch, controller: MachinesController) -> FastAPI:
    monkeypatch.setenv("WEBUI_SESSION_SECRET", "machines-router-test-secret-value")
    monkeypatch.setattr("toolkit.webui.app.controller_client_from_environment", lambda: controller)
    monkeypatch.setattr("toolkit.webui.routers.auth.verify_password", lambda *_args, **_kwargs: (True, "ok"))
    from toolkit.webui.app import create_app

    return create_app(root=tmp_path)


@asynccontextmanager
async def _client(app: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as client:
        login = await client.post("/login", data={"password": ""}, follow_redirects=False)
        assert login.status_code == 303
        yield client


def _csrf(html: str) -> str:
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', html)
    assert match
    return match.group(1)


async def test_machines_page_renders_inventory_and_queues_generation_after_create(
    tmp_path: Path,
    monkeypatch,
) -> None:
    save_config(Config(domain="example.test"), config_path(tmp_path))
    controller = MachinesController(tmp_path)
    app = _app(tmp_path, monkeypatch, controller)
    async with _client(app) as client:
        page = await client.get("/machines")
        response = await client.post(
            "/machines",
            data={
                "csrf_token": _csrf(page.text),
                "revision": read_machines_view(tmp_path).revision,
                "machine_id": "worker-east",
                "kind": "lxc",
                "enabled": "on",
                "hostname": "worker-01",
                "address": "10.10.10.20",
                "vmid": "820",
                "labels": "compute",
                "cores": "2",
                "memory_mb": "2048",
                "root_disk_gb": "32",
                "data_disks": "[]",
                "private_bridge": "vmbr1",
                "gateway": "10.10.10.1",
                "cidr": "24",
                "startup_order": "40",
                "nesting": "on",
                "keyctl": "on",
                "ssh_port": "22",
                "resource_limits": "{}",
            },
            follow_redirects=False,
        )

    assert page.status_code == 200
    assert "Machine inventory" in page.text
    assert "infra-01" in page.text
    assert response.status_code == 303
    assert response.headers["location"] == "/jobs/job-generate-machines"
    assert controller.created[0].machine_id == "worker-east"
    assert controller.created[0].spec.address == "10.10.10.20"
    assert controller.jobs[0].operation.kind.value == "GENERATE"


async def test_machine_save_reports_generation_queue_failure_accurately(tmp_path: Path, monkeypatch) -> None:
    save_config(Config(domain="example.test"), config_path(tmp_path))
    controller = MachinesController(tmp_path)
    controller.generation_available = False
    app = _app(tmp_path, monkeypatch, controller)
    async with _client(app) as client:
        page = await client.get("/machines")
        response = await client.post(
            "/machines/infra",
            data={
                "csrf_token": _csrf(page.text),
                "revision": read_machines_view(tmp_path).revision,
                "kind": "lxc",
                "enabled": "on",
                "managed": "on",
                "hostname": "infra-01",
                "address": "10.10.10.10",
                "vmid": "800",
                "labels": "control,ingress,observability",
                "cores": "6",
                "memory_mb": "12288",
                "root_disk_gb": "64",
                "data_disks": "[]",
                "private_bridge": "vmbr1",
                "public_bridge": "vmbr0",
                "gateway": "10.10.10.1",
                "cidr": "24",
                "startup_order": "10",
                "nesting": "on",
                "keyctl": "on",
                "ssh_port": "22",
                "resource_limits": "{}",
            },
            follow_redirects=False,
        )

    assert response.status_code == 303
    assert "Machine+was+saved%2C+but+generation+could+not+be+queued" in response.headers["location"]
    assert controller.updated
    assert controller.jobs == []


async def test_machine_retirement_renders_plan_and_queues_approved_job(tmp_path: Path, monkeypatch) -> None:
    worker = MachineSpec(
        managed=True,
        hostname="worker-01",
        address="10.10.10.20",
        gateway="10.10.10.1",
        vmid=820,
        labels=("compute",),
    )
    save_config(
        Config(domain="example.test", machines={**Config().machines, "worker-east": worker}),
        config_path(tmp_path),
    )
    controller = MachinesController(tmp_path)
    app = _app(tmp_path, monkeypatch, controller)
    async with _client(app) as client:
        review = await client.get("/machines/worker-east/retire")
        planned = await client.post(
            "/machines/worker-east/retirement-plan",
            data={"csrf_token": _csrf(review.text)},
            follow_redirects=False,
        )
        plan_page = await client.get(planned.headers["location"])
        rejected = await client.post(
            "/machines/worker-east/retire",
            data={
                "csrf_token": _csrf(plan_page.text),
                "plan_id": "plan-retire-worker-east",
                "confirmation": "worker-east",
            },
            follow_redirects=False,
        )
        assert controller.approvals == []
        approved = await client.post(
            "/machines/worker-east/retire",
            data={
                "csrf_token": _csrf(plan_page.text),
                "plan_id": "plan-retire-worker-east",
                "confirmation": "RETIRE MACHINE worker-east",
            },
            follow_redirects=False,
        )

    assert review.status_code == 200
    assert "Create retirement plan" in review.text
    assert planned.status_code == 303
    assert planned.headers["location"] == "/machines/worker-east/retire?plan=plan-retire-worker-east"
    assert "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb" in plan_page.text
    assert "2026-07-15 00:00 UTC" in plan_page.text
    assert "RETIRE MACHINE worker-east" in plan_page.text
    assert rejected.status_code == 303
    assert "Typed+retirement+confirmation+does+not+match" in rejected.headers["location"]
    assert approved.status_code == 303
    assert approved.headers["location"] == "/jobs/job-generate-machines"
    assert controller.approvals == [("plan-retire-worker-east", "a" * 64, "RETIRE MACHINE worker-east")]
    operation = controller.jobs[-1].operation
    assert operation.action == "retire_machine"
    assert operation.config_revision == read_machines_view(tmp_path).revision
