from __future__ import annotations

from pathlib import Path

import pytest
from toolkit.controller.contracts import ConfigApplyOperation
from toolkit.controller.operations import OperationExecutionError, _config_apply_handler, _config_apply_targets
from toolkit.core.config.config import Config, save_config
from toolkit.core.config.mutations import config_revision
from toolkit.core.config.storage import config_path
from toolkit.core.deploy.deploy_workflow import DeployWorkflowResult
from toolkit.core.manifest.catalog import ServiceCatalog
from toolkit.core.manifest.schema import ServiceManifest


class _Context:
    actor = "owner"

    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def check_cancelled(self) -> None:
        return None

    def log(self, message: str, payload: dict | None = None, **_kwargs) -> None:
        self.events.append((message, payload or {}))


def _manifest(
    name: str,
    *,
    category: str,
    placement: str,
    management: dict | None = None,
    enabled_when: list[dict] | None = None,
) -> ServiceManifest:
    return ServiceManifest.model_validate(
        {
            "name": name,
            "label": name.title(),
            "description": f"{name} service",
            "icon": "box",
            "category": category,
            "placement": placement,
            "priority": 10,
            "runtime": "embedded",
            "management": management or {},
            "enabled_when": enabled_when or [],
        }
    )


def test_config_apply_targets_owner_category_and_cross_category_predicate_consumers() -> None:
    owner = _manifest(
        "policy",
        category="media",
        placement="media",
        management={
            "settings": [
                {"key": "mode", "label": "Mode", "type": "select", "default": "one", "choices": ["one", "two"]}
            ]
        },
    )
    same_category = _manifest("worker", category="media", placement="media")
    consumer = _manifest(
        "consumer",
        category="cloud",
        placement="apps",
        enabled_when=[{"setting": "policy.mode", "equals": "one"}],
    )
    unrelated = _manifest("unrelated", category="security", placement="infra")
    catalog = ServiceCatalog((owner, same_category, consumer, unrelated))

    assert _config_apply_targets(Config(), owner, catalog) == ("infra", "media", "apps")


def test_config_apply_generates_deploys_and_verifies_service_node(tmp_path: Path, monkeypatch) -> None:
    save_config(Config(domain="example.com"), config_path(tmp_path))
    revision = config_revision(tmp_path)
    calls: list[dict] = []

    async def fake_workflow(_root, _cfg, **kwargs):
        calls.append(kwargs)
        kwargs["on_log"]("Generating desired state")
        kwargs["on_step"]("generate", "running")
        kwargs["on_progress"]({"percent": "50"})
        return DeployWorkflowResult(True, "Deployment complete", "positive", {"verify": "ok"})

    monkeypatch.setattr("toolkit.core.deploy.deploy_workflow.run_deploy_workflow", fake_workflow)
    context = _Context()

    result = _config_apply_handler(tmp_path)(
        context,
        ConfigApplyOperation(revision_hash=revision, service="music-sync"),
    )

    assert result == {"ok": True, "service": "music-sync", "nodes": ["infra", "media"]}
    assert calls[0]["targets"] == ("infra", "media")
    assert calls[0]["skip_infra"] is True
    assert calls[0]["skip_dns"] is False
    assert calls[0]["preserve_controller"] is False
    assert any(message == "Generating desired state" for message, _payload in context.events)


def test_config_apply_preserves_its_own_controller_runtime(tmp_path: Path, monkeypatch) -> None:
    save_config(Config(domain="example.com"), config_path(tmp_path))
    revision = config_revision(tmp_path)
    calls: list[dict] = []

    async def fake_workflow(_root, _cfg, **kwargs):
        calls.append(kwargs)
        return DeployWorkflowResult(True, "Deployment complete", "positive", {"verify": "ok"})

    monkeypatch.setattr("toolkit.core.deploy.deploy_workflow.run_deploy_workflow", fake_workflow)

    result = _config_apply_handler(tmp_path)(
        _Context(),
        ConfigApplyOperation(revision_hash=revision, service="homelab-ui"),
    )

    assert result["service"] == "homelab-ui"
    assert calls[0]["preserve_controller"] is True


def test_config_apply_rejects_a_superseded_revision(tmp_path: Path) -> None:
    save_config(Config(domain="example.com"), config_path(tmp_path))

    with pytest.raises(OperationExecutionError) as raised:
        _config_apply_handler(tmp_path)(
            _Context(),
            ConfigApplyOperation(revision_hash="a" * 64, service="music-sync"),
        )

    assert raised.value.code == "CONFLICT"
