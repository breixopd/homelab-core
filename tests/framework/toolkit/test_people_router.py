from __future__ import annotations

import re
import uuid
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from toolkit.controller.client import ControllerClientError, ControllerRejectedError
from toolkit.controller.contracts import (
    DeleteDirectoryIdentityCommand,
    IdentityOperation,
    InviteUserCommand,
    JobEvent,
    JobRecord,
    JobRequest,
    JobState,
    ReprovisionUserCommand,
    SetUserGroupsCommand,
)
from toolkit.controller.read_models import DirectoryGroupView, DirectoryUsersView, DirectoryUserView
from toolkit.webui.rbac import admin_route_blocked

pytestmark = pytest.mark.anyio

_JOB_ID = "00000000-0000-4000-8000-000000000001"


def _directory_view() -> DirectoryUsersView:
    return DirectoryUsersView(
        domain="test.example.com",
        users=[
            DirectoryUserView(
                id="ada",
                email="ada@example.com",
                display_name="Ada <Admin>",
                groups=["homelab-media", "homelab-cloud"],
                is_protected=False,
            ),
            DirectoryUserView(
                id="admin",
                email="admin@example.com",
                display_name="Directory admin",
                groups=["homelab-admin"],
                is_protected=True,
            ),
        ],
        group_options=[
            DirectoryGroupView(
                name="homelab-media",
                label="Media",
                description="Media services",
                is_default=True,
            ),
            DirectoryGroupView(
                name="homelab-cloud",
                label="Cloud",
                description="Cloud services",
                is_default=False,
            ),
            DirectoryGroupView(
                name="homelab-admin",
                label="Admin",
                description="Operator access",
                is_default=False,
            ),
        ],
        invites_enabled=True,
    )


class PeopleController:
    def __init__(self) -> None:
        self.submitted: list[JobRequest] = []
        self.directory_view = _directory_view()
        self.event_records: list[JobEvent] = []
        self.read_error: ControllerClientError | None = None
        self.submit_error: ControllerClientError | None = None

    def directory_users(self) -> DirectoryUsersView:
        if self.read_error is not None:
            raise self.read_error
        return self.directory_view

    def submit(self, request: JobRequest) -> JobRecord:
        if self.submit_error is not None:
            raise self.submit_error
        self.submitted.append(request)
        now = datetime.now(UTC)
        return JobRecord(
            job_id=_JOB_ID,
            request=request,
            state=JobState.QUEUED,
            actor="mtls:homelab-ui",
            created_at=now,
            updated_at=now,
        )

    def events(self, _job_id: str, *, after: int, limit: int) -> list[JobEvent]:
        assert after >= 0
        assert limit == 200
        return [event for event in self.event_records if event.sequence > after][:limit]

    def get_job(self, _job_id: str):
        return type("TerminalJob", (), {"state": JobState.SUCCEEDED})()

    def close(self) -> None:
        return None


def _create_app(tmp_path: Path, monkeypatch, controller: PeopleController) -> FastAPI:
    (tmp_path / "config.yaml").write_text(
        "domain: test.example.com\nemail: owner@example.com\ntimezone: UTC\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("WEBUI_SESSION_SECRET", "people-router-test-secret")
    monkeypatch.setattr("toolkit.webui.app.controller_client_from_environment", lambda: controller)
    monkeypatch.setattr(
        "toolkit.webui.routers.auth.verify_password",
        lambda _password, _client_ip, email="": (True, "Authenticated"),
    )
    from toolkit.webui.app import create_app

    return create_app(root=tmp_path)


@asynccontextmanager
async def _admin_client(app: FastAPI):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="https://testserver") as client:
        response = await client.post("/login", data={"password": ""}, follow_redirects=False)
        assert response.status_code == 303
        yield client


def _csrf(response_text: str) -> str:
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', response_text)
    assert match is not None
    return match.group(1)


def test_people_htmx_error_swap_policy_preserves_the_identity_job_panel() -> None:
    app_js = (Path(__file__).resolve().parents[3] / "toolkit/webui/static/js/app.js").read_text(encoding="utf-8")

    statuses = re.search(
        r"const PEOPLE_IDENTITY_ERROR_STATUSES = new Set\(\[([^]]+)\]\);",
        app_js,
    )
    assert statuses is not None
    assert {int(status) for status in statuses.group(1).split(", ")} == {400, 409, 429, 503}

    handler = re.search(
        r"function routePeopleIdentityResponse\(event\) \{(?P<body>.*?)\n\}",
        app_js,
        re.DOTALL,
    )
    assert handler is not None
    body = handler.group("body")
    assert "detail.target?.id !== 'identity-job-panel'" in body
    assert "!PEOPLE_IDENTITY_ERROR_STATUSES.has(detail.xhr?.status)" in body
    assert "document.getElementById('identity-error')" in body
    assert "detail.target = errorRegion" in body
    assert "detail.shouldSwap = true" in body
    assert "detail.isError = false" in body
    assert "errorRegion.replaceChildren()" in body
    assert "'htmx:beforeSwap', routePeopleIdentityResponse" in app_js


def test_identity_job_done_handler_presents_partial_failure_as_a_warning() -> None:
    deploy_js = (Path(__file__).resolve().parents[3] / "toolkit/webui/static/js/deploy.js").read_text(encoding="utf-8")

    done_handler = re.search(
        r"source\.addEventListener\('done'.*?source\.addEventListener\('error'",
        deploy_js,
        re.DOTALL,
    )
    assert done_handler is not None
    body = done_handler.group(0)
    assert "const partial = payload.partial === true" in body
    assert "partial ? 'partial failure' : 'failed'" in body
    assert "partial ? 'warn' : 'critical'" in body


async def test_people_page_renders_the_typed_directory_view(tmp_path: Path, monkeypatch) -> None:
    app = _create_app(tmp_path, monkeypatch, PeopleController())
    async with _admin_client(app) as client:
        response = await client.get("/people")

    assert response.status_code == 200
    assert "test.example.com" in response.text
    assert "ada@example.com" in response.text
    assert "Ada &lt;Admin&gt;" in response.text
    assert "homelab-media" in response.text
    assert "Ada <Admin>" not in response.text


async def test_people_page_renders_semantic_accessible_controls(tmp_path: Path, monkeypatch) -> None:
    app = _create_app(tmp_path, monkeypatch, PeopleController())
    async with _admin_client(app) as client:
        response = await client.get("/people")

    assert response.status_code == 200
    assert "<h1>People</h1>" in response.text
    assert '<h2 id="directory-identities-heading">Directory identities</h2>' in response.text
    assert '<a href="/people" class="nav-link active" aria-current="page">' in response.text
    assert '<table class="data-table people-table"' in response.text
    assert '<label for="invite-email">Email</label>' in response.text
    assert '<label for="invite-display-name">Display name</label>' in response.text
    assert 'aria-label="Access groups for ada"' in response.text
    assert 'aria-label="Delete directory identity ada"' in response.text
    assert 'hx-post="/people/jobs/ada/groups"' in response.text
    assert 'hx-post="/people/jobs/ada/reprovision"' in response.text
    assert 'hx-post="/people/jobs/ada/delete-directory"' in response.text
    assert "This does not revoke downstream sessions or delete vault data." in response.text
    assert '<div id="identity-error" class="people-identity-error" role="alert" aria-live="assertive"' in response.text
    assert response.text.index('id="identity-error"') < response.text.index('id="identity-job-panel"')

    for prefix in ("invite", "ada"):
        for group_name, description in (
            ("homelab-media", "Media services"),
            ("homelab-cloud", "Cloud services"),
            ("homelab-admin", "Operator access"),
        ):
            description_id = f"{prefix}-group-description-{group_name}"
            assert f'aria-describedby="{description_id}"' in response.text
            assert f'id="{description_id}"' in response.text
            assert description in response.text


async def test_people_workspace_uses_compact_disclosures_and_aligned_detail_rows(tmp_path: Path, monkeypatch) -> None:
    app = _create_app(tmp_path, monkeypatch, PeopleController())
    async with _admin_client(app) as client:
        response = await client.get("/people")

    assert response.status_code == 200
    assert 'data-disclosure-toggle="invite-person"' in response.text
    assert 'id="invite-person" class="content-band people-invite" hidden' in response.text
    assert 'class="people-field span-6"' in response.text
    assert 'class="choice-grid people-choice-grid"' in response.text
    assert 'class="sr-only" id="invite-group-description-homelab-media"' in response.text
    assert 'class="row-actions people-row-actions"' in response.text
    assert 'data-row-panel="access-ada"' in response.text
    assert 'id="access-ada" class="people-detail-row" hidden' in response.text
    assert 'data-row-panel="delete-ada"' in response.text
    assert 'id="delete-ada" class="people-detail-row people-detail-danger" hidden' in response.text
    assert "onclick=" not in response.text
    assert "style=" not in response.text


async def test_protected_people_have_status_but_no_mutation_controls(tmp_path: Path, monkeypatch) -> None:
    app = _create_app(tmp_path, monkeypatch, PeopleController())
    async with _admin_client(app) as client:
        response = await client.get("/people")

    protected_row = re.search(r'<tr[^>]*data-user-id="admin".*?</tr>', response.text, re.DOTALL)
    assert protected_row is not None
    assert "Protected" in protected_row.group(0)
    assert "/people/jobs/admin/" not in protected_row.group(0)
    assert "Edit access" not in protected_row.group(0)
    assert "Reissue activation" not in protected_row.group(0)
    assert "Delete directory identity" not in protected_row.group(0)


async def test_invite_form_is_hidden_when_secure_invites_are_unavailable(tmp_path: Path, monkeypatch) -> None:
    controller = PeopleController()
    controller.directory_view = controller.directory_view.model_copy(
        update={
            "invites_enabled": False,
            "invite_disabled_reason": "Identity invitation credentials are not configured.",
        }
    )
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        response = await client.get("/people")

    assert response.status_code == 200
    assert 'hx-post="/people/jobs/invite"' not in response.text
    assert "Identity invitation credentials are not configured." in response.text


@pytest.mark.parametrize(
    ("path", "form", "command_type", "expected"),
    [
        (
            "/people/jobs/invite",
            {
                "email": " Person@Example.com ",
                "display_name": " Person Example ",
                "groups": ["homelab-media", "homelab-cloud"],
            },
            InviteUserCommand,
            {
                "email": "person@example.com",
                "display_name": "Person Example",
                "groups": ["homelab-media", "homelab-cloud"],
            },
        ),
        (
            "/people/jobs/ada/groups",
            {"groups": ["homelab-cloud"]},
            SetUserGroupsCommand,
            {"user_id": "ada", "groups": ["homelab-cloud"]},
        ),
        (
            "/people/jobs/ada/reprovision",
            {},
            ReprovisionUserCommand,
            {"user_id": "ada"},
        ),
        (
            "/people/jobs/ada/delete-directory",
            {"confirmation": "ada"},
            DeleteDirectoryIdentityCommand,
            {"user_id": "ada", "confirmation": "ada"},
        ),
    ],
)
async def test_people_commands_submit_typed_identity_jobs(
    tmp_path: Path,
    monkeypatch,
    path: str,
    form: dict[str, str | list[str]],
    command_type: type,
    expected: dict[str, str | list[str]],
) -> None:
    controller = PeopleController()
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        page = await client.get("/people")
        response = await client.post(path, data={**form, "csrf_token": _csrf(page.text)})

    assert response.status_code == 200
    assert _JOB_ID in response.text
    assert f"/people/stream/{_JOB_ID}" in response.text
    assert len(controller.submitted) == 1
    submitted = controller.submitted[0]
    uuid.UUID(submitted.idempotency_key)
    assert isinstance(submitted.operation, IdentityOperation)
    assert isinstance(submitted.operation.command, command_type)
    for field, value in expected.items():
        assert getattr(submitted.operation.command, field) == value


async def test_delete_requires_exact_user_id_confirmation(tmp_path: Path, monkeypatch) -> None:
    controller = PeopleController()
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        page = await client.get("/people")
        response = await client.post(
            "/people/jobs/ada/delete-directory",
            data={"confirmation": "Ada", "csrf_token": _csrf(page.text)},
        )

    assert response.status_code == 400
    assert response.text == "Identity request was invalid"
    assert controller.submitted == []


@pytest.mark.parametrize("status_code", [409, 429])
async def test_controller_conflict_is_reported_without_exception_details(
    tmp_path: Path,
    monkeypatch,
    status_code: int,
) -> None:
    controller = PeopleController()
    controller.submit_error = ControllerRejectedError(
        "CONFLICT",
        "another operation includes private@example.com",
        {"secret": "do-not-render"},
        "correlation-private",
        status_code,
    )
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        page = await client.get("/people")
        response = await client.post(
            "/people/jobs/ada/reprovision",
            data={"csrf_token": _csrf(page.text)},
        )

    assert response.status_code == status_code
    assert response.text == "Another identity mutation is already active"
    assert "private@example.com" not in response.text
    assert "do-not-render" not in response.text


async def test_invalid_invite_has_a_bounded_no_pii_error(tmp_path: Path, monkeypatch) -> None:
    controller = PeopleController()
    app = _create_app(tmp_path, monkeypatch, controller)
    private_email = "private-person"
    private_name = "Private Person"
    async with _admin_client(app) as client:
        page = await client.get("/people")
        response = await client.post(
            "/people/jobs/invite",
            data={
                "email": private_email,
                "display_name": private_name,
                "groups": ["homelab-media"],
                "csrf_token": _csrf(page.text),
            },
        )

    assert response.status_code == 400
    assert response.text == "Identity request was invalid"
    assert len(response.content) < 128
    assert private_email not in response.text
    assert private_name not in response.text
    assert controller.submitted == []


async def test_directory_read_failure_renders_a_degraded_management_page(tmp_path: Path, monkeypatch) -> None:
    controller = PeopleController()
    controller.read_error = ControllerClientError("private directory failure: /secret/path")
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        response = await client.get("/people")

    assert response.status_code == 200
    assert "<h1>People</h1>" in response.text
    assert "Directory users are temporarily unavailable" in response.text
    assert "Identity changes are disabled until the directory connection recovers." in response.text
    assert 'hx-post="/people/jobs/invite"' not in response.text
    assert "/secret/path" not in response.text


async def test_controller_failure_while_submitting_is_a_bounded_503(tmp_path: Path, monkeypatch) -> None:
    controller = PeopleController()
    controller.submit_error = ControllerClientError("private@example.com at /secret/path")
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        page = await client.get("/people")
        response = await client.post(
            "/people/jobs/ada/reprovision",
            data={"csrf_token": _csrf(page.text)},
        )

    assert response.status_code == 503
    assert response.text == "Identity request could not be queued"
    assert "private@example.com" not in response.text
    assert "/secret/path" not in response.text
    assert len(response.content) < 128


async def test_people_stream_uses_the_shared_controller_event_adapter(tmp_path: Path, monkeypatch) -> None:
    app = _create_app(tmp_path, monkeypatch, PeopleController())
    async with _admin_client(app) as client:
        response = await client.get("/people/stream/job-123?after=-5")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: done" in response.text
    assert "Job completed" in response.text


async def test_people_stream_replays_controller_events_in_sequence(tmp_path: Path, monkeypatch) -> None:
    controller = PeopleController()
    now = datetime.now(UTC)
    controller.event_records = [
        JobEvent(
            job_id="job-123",
            sequence=1,
            timestamp=now,
            level="INFO",
            message="Identity queued",
        ),
        JobEvent(
            job_id="job-123",
            sequence=2,
            timestamp=now,
            level="INFO",
            message="Access updated",
        ),
    ]
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        response = await client.get("/people/stream/job-123?after=0")

    assert response.status_code == 200
    first = response.text.index("id: 1\nevent: log\ndata: Identity queued")
    second = response.text.index("id: 2\nevent: log\ndata: Access updated")
    done = response.text.index("event: done")
    assert first < second < done


async def test_people_controller_calls_run_in_the_threadpool(tmp_path: Path, monkeypatch) -> None:
    controller = PeopleController()
    app = _create_app(tmp_path, monkeypatch, controller)
    from toolkit.webui.routers import people

    calls: list[str] = []

    async def record_threadpool(function, *args, **kwargs):
        calls.append(function.__name__)
        return function(*args, **kwargs)

    monkeypatch.setattr(people, "run_in_threadpool", record_threadpool)
    async with _admin_client(app) as client:
        page = await client.get("/people")
        response = await client.post(
            "/people/jobs/ada/reprovision",
            data={"csrf_token": _csrf(page.text)},
        )

    assert response.status_code == 200
    assert calls == ["directory_users", "submit"]


async def test_family_users_cannot_access_people_or_its_stream(tmp_path: Path, monkeypatch) -> None:
    controller = PeopleController()
    app = _create_app(tmp_path, monkeypatch, controller)
    async with _admin_client(app) as client:
        page = await client.get("/people")
        with (
            patch("toolkit.webui.auth.authelia_user", return_value="family@example.com"),
            patch("toolkit.webui.rbac.authelia_user", return_value="family@example.com"),
            patch("toolkit.webui.rbac.authelia_groups", return_value=["homelab-media"]),
        ):
            people_response = await client.get("/people", follow_redirects=False)
            stream_response = await client.get("/people/stream/job-123", follow_redirects=False)
            mutation_response = await client.post(
                "/people/jobs/ada/reprovision",
                data={"csrf_token": _csrf(page.text)},
                follow_redirects=False,
            )

    assert people_response.status_code == 303
    assert people_response.headers["location"] == "/"
    assert stream_response.status_code == 303
    assert stream_response.headers["location"] == "/"
    assert mutation_response.status_code == 303
    assert mutation_response.headers["location"] == "/"
    assert controller.submitted == []
    assert admin_route_blocked("/people")
    assert admin_route_blocked("/people/stream/job-123")
