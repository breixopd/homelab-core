from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = ROOT / "toolkit/webui/templates"
STATIC = ROOT / "toolkit/webui/static"


def test_error_page_is_a_shared_themed_recovery_surface() -> None:
    page = (TEMPLATES / "error.html").read_text(encoding="utf-8")
    fragment = (TEMPLATES / "partials/error_fragment.html").read_text(encoding="utf-8")
    css = (STATIC / "css/main.css").read_text(encoding="utf-8")

    assert page.startswith('{% extends "base.html" %}')
    assert 'aria-labelledby="error-heading"' in page
    assert 'href="{{ retry_url }}"' in page
    assert 'href="/"' in page
    assert 'role="alert"' in fragment
    assert 'href="{{ retry_url }}"' in fragment
    assert ".webui-error" in css
    assert ".webui-error-fragment" in css
    assert "style=" not in page
    assert "onclick=" not in page


def test_controller_failures_use_the_shared_error_renderer() -> None:
    routers = (
        "dashboard",
        "services",
        "operations",
        "machines",
        "jobs",
        "settings",
        "dns",
        "secrets",
        "deploy",
        "account",
    )
    for router in routers:
        source = (ROOT / "toolkit/webui/routers" / f"{router}.py").read_text(encoding="utf-8")
        assert "from toolkit.webui.error_pages import render_error" in source
        assert 'temporarily unavailable", status_code=503' not in source
