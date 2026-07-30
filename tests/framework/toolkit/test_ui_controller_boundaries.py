from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def _imports(relative_path: str) -> set[str]:
    source = (ROOT / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    return imported


def test_services_router_has_no_host_or_domain_authority() -> None:
    imports = _imports("toolkit/webui/routers/services.py")

    assert "subprocess" not in imports
    assert not any(name.startswith("toolkit.core") for name in imports)
    assert "toolkit.webui" not in imports


def test_graph_router_is_a_typed_controller_proxy() -> None:
    imports = _imports("toolkit/webui/routers/graph.py")

    assert "subprocess" not in imports
    assert not any(name.startswith("toolkit.core") for name in imports)
    assert "toolkit.webui" not in imports
    assert "toolkit.webui.graph_data" not in imports


def test_dashboard_router_has_no_runtime_or_config_authority() -> None:
    imports = _imports("toolkit/webui/routers/dashboard.py")

    assert "subprocess" not in imports
    assert not any(name.startswith("toolkit.core") for name in imports)
    assert "toolkit.webui" not in imports
    assert "toolkit.webui.prometheus_helpers" not in imports


def test_dns_router_has_no_config_secret_or_job_authority() -> None:
    imports = _imports("toolkit/webui/routers/dns.py")

    assert not any(name.startswith("toolkit.core") for name in imports)
    assert "toolkit.webui" not in imports


def test_settings_router_has_no_config_generation_or_maintenance_authority() -> None:
    imports = _imports("toolkit/webui/routers/settings.py")

    assert not any(name.startswith("toolkit.core") for name in imports)
    assert "toolkit.webui" not in imports


def test_projects_router_has_no_config_or_generation_authority() -> None:
    imports = _imports("toolkit/webui/routers/projects.py")

    assert not any(name.startswith("toolkit.core") for name in imports)
    assert "toolkit.webui" not in imports


def test_account_router_has_no_config_or_service_catalog_authority() -> None:
    imports = _imports("toolkit/webui/routers/account.py")

    assert not any(name.startswith("toolkit.core") for name in imports)
    assert "toolkit.services.sdk" not in imports
    assert "toolkit.webui" not in imports


def test_invite_router_has_no_secret_directory_or_token_authority() -> None:
    imports = _imports("toolkit/webui/routers/invite.py")

    assert not any(name.startswith("toolkit.core") for name in imports)
    assert "toolkit.webui" not in imports


def test_webhook_router_is_a_raw_controller_proxy() -> None:
    imports = _imports("toolkit/webui/routers/webhooks.py")

    assert not any(name.startswith("toolkit.core") for name in imports)
    assert "hmac" not in imports
    assert "os" not in imports
    assert "toolkit.webui" not in imports


def test_setup_router_has_no_config_secret_or_bootstrap_file_authority() -> None:
    imports = _imports("toolkit/webui/routers/setup.py")

    assert not any(name.startswith("toolkit.core") for name in imports)
    assert "toolkit.webui" not in imports
    assert "toolkit.webui.bootstrap" not in imports


def test_deploy_router_has_no_workflow_filesystem_or_process_authority() -> None:
    imports = _imports("toolkit/webui/routers/deploy.py")

    assert "subprocess" not in imports
    assert not any(name.startswith("toolkit.core") for name in imports)
    assert "toolkit.webui" not in imports
    assert "toolkit.webui.sse" not in imports


def test_people_router_has_no_identity_secret_host_or_config_authority() -> None:
    imports = _imports("toolkit/webui/routers/people.py")

    assert "subprocess" not in imports
    assert not any(name.startswith("toolkit.core") for name in imports)
    assert not any(name.startswith("toolkit.services") for name in imports)
    assert not any(fragment in name for name in imports for fragment in ("docker", "ssh", "sops", "secrets"))
    assert not {
        "toolkit.webui.bootstrap",
        "toolkit.webui.graph_data",
        "toolkit.webui.prometheus_helpers",
        "toolkit.webui.sse",
        "toolkit.webui.workflow",
    }.intersection(imports)


def test_webui_registers_only_controller_backed_privileged_routes() -> None:
    source = (ROOT / "toolkit/webui/app.py").read_text(encoding="utf-8")

    for router_name in ("hosts", "updates"):
        assert f"app.include_router({router_name}.router)" not in source

    operations = (ROOT / "toolkit/webui/routers/operations.py").read_text(encoding="utf-8")
    assert "request.app.state.controller.operations_view" in operations
    assert "request.app.state.controller.submit" in operations
    assert "toolkit.core." not in operations

    router_exports = (ROOT / "toolkit/webui/routers/__init__.py").read_text(encoding="utf-8")
    for router_name in ("hosts", "updates"):
        assert f'"{router_name}"' not in router_exports
    assert '"operations"' in router_exports
