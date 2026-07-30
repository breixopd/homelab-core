from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from toolkit.controller.contracts import DeployOperation, JobRecord, JobRequest, JobState
from toolkit.controller.read_models import DeploymentPreflightCheck, DeploymentView

pytestmark = pytest.mark.anyio


class DeploymentController:
    def __init__(self) -> None:
        self.submitted: JobRequest | None = None
        self.preflight_ok = True

    def deployment_view(self) -> DeploymentView:
        return DeploymentView(
            state="ready",
            enabled_targets=["infra", "apps", "media"],
            node_count=3,
            total_services=12,
            category_count=4,
            generated_config_count=3,
            step_labels={"preflight": "Pre-flight checks"},
            preflight=[
                DeploymentPreflightCheck(
                    check_id="config",
                    label="Configuration",
                    ok=self.preflight_ok,
                    detail="",
                )
            ],
            preflight_ok=self.preflight_ok,
            active_jobs=[],
        )

    def submit(self, request: JobRequest) -> JobRecord:
        self.submitted = request
        now = datetime.now(UTC)
        return JobRecord(
            job_id="00000000-0000-4000-8000-000000000001",
            request=request,
            state=JobState.QUEUED,
            actor="mtls:homelab-ui",
            created_at=now,
            updated_at=now,
        )

    def close(self) -> None:
        return None


def _create_app(tmp_path: Path, monkeypatch, controller: DeploymentController) -> FastAPI:
    (tmp_path / "config.yaml").write_text("domain: test.example.com\nemail: owner@example.com\n")
    monkeypatch.setenv("WEBUI_SESSION_SECRET", "deploy-router-test-secret")
    monkeypatch.setattr("toolkit.webui.app.controller_client_from_environment", lambda: controller)
    monkeypatch.setattr(
        "toolkit.webui.routers.auth.verify_password",
        lambda _password, _client_ip, email="": (True, "Authenticated"),
    )
    from toolkit.webui.app import create_app

    return create_app(root=tmp_path)


async def test_deploy_route_submits_one_typed_controller_job(tmp_path: Path, monkeypatch) -> None:
    controller = DeploymentController()
    app = _create_app(tmp_path, monkeypatch, controller)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as client:
        assert (await client.post("/login", data={"password": ""})).status_code == 303
        page = await client.get("/deploy")
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)
        assert csrf is not None
        response = await client.post(
            "/deploy/jobs/deploy",
            data={
                "csrf_token": csrf.group(1),
                "target_vm": "apps",
                "skip_infra": "true",
                "skip_dns": "true",
            },
        )

    assert response.status_code == 200
    assert "00000000-0000-4000-8000-000000000001" in response.text
    assert controller.submitted is not None
    assert isinstance(controller.submitted.operation, DeployOperation)
    assert controller.submitted.operation.target == "apps"
    assert controller.submitted.operation.skip_infrastructure is True
    assert controller.submitted.operation.skip_dns is True


async def test_deploy_route_removes_redundant_pipeline_subjobs(tmp_path: Path, monkeypatch) -> None:
    controller = DeploymentController()
    app = _create_app(tmp_path, monkeypatch, controller)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as client:
        await client.post("/login", data={"password": ""})
        page = await client.get("/deploy")
        csrf = re.search(r'<meta name="csrf-token" content="([^"]+)"', page.text)
        assert csrf is not None
        for action in ("hooks", "verify-hooks", "qa"):
            response = await client.post(
                f"/deploy/jobs/{action}",
                data={"csrf_token": csrf.group(1)},
            )
            assert response.status_code == 404

    assert controller.submitted is None


async def test_preflight_refresh_updates_status_and_deploy_control(tmp_path: Path, monkeypatch) -> None:
    controller = DeploymentController()
    controller.preflight_ok = False
    app = _create_app(tmp_path, monkeypatch, controller)
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as client:
        await client.post("/login", data={"password": ""})
        page = await client.get("/deploy")
        assert re.search(r'<button class="btn btn-primary"\s+disabled', page.text)
        assert page.text.count('id="preflight-status"') == 1
        assert page.text.count('id="deploy-primary-action"') == 1

        controller.preflight_ok = True
        refreshed = await client.get("/partials/deploy/preflight")

    assert refreshed.status_code == 200
    assert 'id="preflight-status"' in refreshed.text
    assert 'hx-swap-oob="true"' in refreshed.text
    action = re.search(r'<span id="deploy-primary-action".*?</span>', refreshed.text, re.DOTALL)
    assert action is not None
    assert "disabled" not in action.group(0)
