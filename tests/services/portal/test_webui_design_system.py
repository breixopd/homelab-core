from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = ROOT / "toolkit/webui/templates"
STATIC = ROOT / "toolkit/webui/static"


def test_base_owns_one_aligned_workspace_and_accessible_mobile_navigation() -> None:
    source = (TEMPLATES / "base.html").read_text(encoding="utf-8")

    assert 'class="skip-link"' in source
    assert 'id="main-content"' in source
    assert 'class="workspace-header"' in source
    assert 'class="workspace-body"' in source
    assert source.count('class="workspace"') == 1
    assert "data-nav-toggle" in source
    assert 'aria-controls="sidebar"' in source
    assert "data-nav-dismiss" in source
    assert "onclick=" not in source
    assert "style=" not in source


def test_admin_navigation_is_grouped_by_real_workflows() -> None:
    source = (TEMPLATES / "base.html").read_text(encoding="utf-8")

    for group in ("Operate", "Manage", "Configure"):
        assert f">{group}<" in source
    for href in ("/", "/services", "/deploy", "/jobs", "/people", "/projects", "/settings", "/secrets", "/dns"):
        assert f'href="{href}"' in source
    assert "Updates" not in source
    assert "Maintenance" not in source
    assert "Backups" not in source
    assert "Fleet" not in source


def test_shared_shell_has_no_remote_fonts_or_global_chart_bundle() -> None:
    source = (TEMPLATES / "base.html").read_text(encoding="utf-8")

    assert "fonts.googleapis.com" not in source
    assert "fonts.gstatic.com" not in source
    assert "echarts" not in source.lower()
    assert 'src="/static/js/app.js?v={{ static_revision }}"' in source


def test_every_public_ui_template_uses_only_local_runtime_assets() -> None:
    pages = ("base.html", "login.html", "invite_activate.html", "setup.html", "account.html")

    for page in pages:
        source = (TEMPLATES / page).read_text(encoding="utf-8")
        assert 'src="https://' not in source
        assert 'href="https://fonts.' not in source
        assert "style=" not in source
    assert 'src="/static/js/htmx-2.0.10.min.js?v={{ static_revision }}"' in (TEMPLATES / "base.html").read_text(
        encoding="utf-8"
    )
    assert (STATIC / "js/htmx-2.0.10.min.js").is_file()


def test_homelab_service_endpoints_open_in_new_tabs() -> None:
    source = (TEMPLATES / "services.html").read_text(encoding="utf-8")

    assert 'class="service-endpoint" href="{{ route.url }}" target="_blank" rel="noopener"' in source


def test_generated_portal_has_no_remote_presentation_assets() -> None:
    source = (ROOT / "toolkit/services/portal/templates/portal.html.j2").read_text(encoding="utf-8")

    assert "fonts.googleapis.com" not in source
    assert "fonts.gstatic.com" not in source
    assert "data:image" not in source
    assert 'href="/favicon.svg"' in source
    assert 'class="skip-link"' in source
    assert 'id="main-content"' in source
    assert 'aria-live="polite"' in source
    assert "0 reachable" not in source
    assert 'fetch("/api/portal/status"' in source
    assert 'data-status-service="{{ svc.service }}"' in source
    assert "Checking live service status" in source
    assert "Live status is temporarily unavailable" in source
    assert "prefers-reduced-motion" in source


def test_portal_destinations_open_in_isolated_new_tabs() -> None:
    source = (ROOT / "toolkit/services/portal/templates/portal.html.j2").read_text(encoding="utf-8")

    assert 'class="pin" href="{{ link.href }}" target="_blank" rel="noopener noreferrer"' in source
    assert source.count('target="_blank"') == source.count('rel="noopener noreferrer"')
    assert (
        'href="https://{{ svc.url }}"\n              target="_blank"\n              rel="noopener noreferrer"'
    ) in source
    assert (
        'href="https://{{ endpoint.url }}"\n                target="_blank"\n                rel="noopener noreferrer"'
    ) in source
    assert 'href="https://{{ proj.url }}"\n            target="_blank"\n            rel="noopener noreferrer"' in source


def test_dashboard_does_not_name_metrics_implementations() -> None:
    controller = (ROOT / "toolkit/controller/dashboard_api.py").read_text(encoding="utf-8")
    template = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")

    assert "/services/grafana" not in controller
    assert "/services/prometheus" not in controller
    assert "/services/grafana" not in template
    assert "Open Grafana" not in template


def test_css_uses_one_compact_spacing_and_radius_system() -> None:
    source = (STATIC / "css/main.css").read_text(encoding="utf-8")

    assert "--content-gutter: 1.5rem" in source
    assert "--sidebar-width: 14rem" in source
    assert "--radius: 6px" in source
    assert "--radius-sm: 4px" in source
    assert "grid-template-columns: var(--sidebar-width) minmax(0, 1fr)" in source
    assert ".workspace-header" in source
    assert ".workspace-body" in source
    assert ".choice-grid" in source
    assert ".content-band" in source
    assert "linear-gradient" not in source


def test_disabled_commands_are_visually_distinct_from_available_actions() -> None:
    source = (STATIC / "css/main.css").read_text(encoding="utf-8")

    assert ".btn:disabled" in source
    assert "cursor: not-allowed" in source
    assert "opacity: 0.45" in source


def test_desktop_menu_control_does_not_consume_a_workspace_grid_cell() -> None:
    source = (STATIC / "css/main.css").read_text(encoding="utf-8")

    assert ".sidebar-toggle.btn { display: none; }" in source
    assert "@media (max-width: 1023px)" in source
    assert ".sidebar-toggle.btn { display: inline-flex; }" in source


def test_small_grid_fields_expand_to_a_usable_width_on_mobile() -> None:
    source = (STATIC / "css/main.css").read_text(encoding="utf-8")
    mobile = source.split("@media (max-width: 639px)", maxsplit=1)[1]

    assert ".span-1,\n  .span-2,\n  .span-3," in mobile


def test_shared_lucide_icons_render_as_current_color_strokes() -> None:
    source = (STATIC / "css/main.css").read_text(encoding="utf-8")

    assert ".nav-icon," in source
    assert ".button-icon," in source
    assert ".btn-icon," in source
    assert ".badge-icon {" in source
    assert "fill: none;" in source
    assert "stroke: currentColor;" in source
    assert "stroke-linecap: round;" in source


def test_shared_javascript_owns_navigation_and_csrf_without_inline_script() -> None:
    base = (TEMPLATES / "base.html").read_text(encoding="utf-8")
    app_js = (STATIC / "js/app.js").read_text(encoding="utf-8")

    assert base.count("<script>") == 0
    assert "[data-nav-toggle]" in app_js
    assert "[data-nav-dismiss]" in app_js
    assert "csrf_token" in app_js
    assert "htmx:configRequest" in app_js


def test_shared_disclosures_restore_focus_when_escape_closes_the_active_panel() -> None:
    app_js = (STATIC / "js/app.js").read_text(encoding="utf-8")

    assert "function disclosureTarget(control)" in app_js
    assert "function setDisclosure(control, open" in app_js
    assert "function controlForDisclosure(targetId)" in app_js
    assert "target.contains(document.activeElement)" in app_js
    assert 'input:not([type="hidden"])' in app_js
    assert "control.focus()" in app_js
    assert '`[data-row-panel="${row.id}"]`' not in app_js
