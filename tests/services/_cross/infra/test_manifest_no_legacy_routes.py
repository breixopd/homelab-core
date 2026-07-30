from __future__ import annotations

import ast
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[4]
CATEGORIES = ROOT / "toolkit" / "categories"


def test_category_layer_has_no_route_contract() -> None:
    category_source = (CATEGORIES / "__init__.py").read_text(encoding="utf-8")
    loader_source = (CATEGORIES / "yaml_loader.py").read_text(encoding="utf-8")
    plugin_trees = [ast.parse(path.read_text(encoding="utf-8")) for path in CATEGORIES.glob("*/plugin.py")]

    for token in ("class Route", "_yaml_routes", "_hook_routes", "def routes("):
        assert token not in category_source
    for token in ("build_routes", '"_routes"', "routes_override"):
        assert token not in loader_source
    assert "media_routes" not in {
        node.name
        for tree in plugin_trees
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
    }

    for path in CATEGORIES.glob("*/category.yaml"):
        document = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        assert "routes" not in document, path
        assert "routes_override" not in document, path


def test_production_has_no_secondary_exposure_or_auth_authority() -> None:
    production_files = [
        *ROOT.glob("toolkit/**/*.py"),
        *ROOT.glob("toolkit/**/*.html"),
        *ROOT.glob("toolkit/**/*.j2"),
        ROOT / "config.yaml",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in production_files if path.is_file())

    for token in (
        "service_exposure",
        "core.generate.exposure",
        "OIDC_NATIVE_SERVICES",
        "ALWAYS_PUBLIC_SERVICES",
        "PUBLIC_EXPOSURE_CANDIDATES",
        "RECOMMENDED_EXPOSURE",
    ):
        assert token not in combined

    generate_source = (ROOT / "toolkit" / "core" / "generate" / "generate.py").read_text(encoding="utf-8")
    assert '"oidc_client_id": "headscale"' not in generate_source
