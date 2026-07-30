from __future__ import annotations

from toolkit.core.config.config import Config, ServicesConfig
from toolkit.core.registry.mesh_status import mesh_status_snapshot
from toolkit.core.verify.models import VerifyCheck, VerifyStatus


def test_mesh_status_projects_not_applicable_router_as_neutral(tmp_path, monkeypatch) -> None:
    cfg = Config(domain="example.com", services=ServicesConfig(security=True))
    monkeypatch.setattr(
        "toolkit.services.headscale.plugin.check_nodes",
        lambda *_args, **_kwargs: VerifyCheck("headscale", "nodes", True, "1/1 node(s) online"),
    )
    monkeypatch.setattr(
        "toolkit.services.headscale.plugin.check_subnet_router",
        lambda *_args, **_kwargs: VerifyCheck(
            "headscale",
            "subnet_router",
            True,
            "not required",
            status=VerifyStatus.NOT_APPLICABLE,
        ),
    )

    snapshot = mesh_status_snapshot(cfg, tmp_path)

    assert snapshot.enabled is True
    assert snapshot.subnet_router_ok is None
    assert snapshot.subnet_router_detail == "not required"
    assert snapshot.nodes_online == 1
    assert snapshot.nodes_total == 1
