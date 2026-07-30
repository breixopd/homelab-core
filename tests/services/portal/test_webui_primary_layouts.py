from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
TEMPLATES = ROOT / "toolkit/webui/templates"
STATIC = ROOT / "toolkit/webui/static"


def test_projects_uses_the_shared_disclosure_form_and_compact_directory_table() -> None:
    source = (TEMPLATES / "projects.html").read_text(encoding="utf-8")

    assert 'data-disclosure-toggle="add-project-form"' in source
    assert 'id="add-project-form" class="content-band project-create" hidden' in source
    assert 'class="field-grid project-form"' in source
    assert 'class="responsive-table project-table-wrap"' in source
    assert 'class="data-table project-table"' in source
    assert 'data-disclosure-close="add-project-form"' in source
    assert 'name="revision" value="{{ revision }}"' in source
    assert 'action="/projects/add"' in source
    assert 'name="placement"' in source
    assert 'name="node"' not in source
    assert "{{ proj.placement }}" in source
    assert "{{ proj.node }}" in source
    assert 'action="/projects/remove/{{ proj.subdomain }}"' in source
    assert 'hx-confirm="Remove this project and reconcile the deployment?"' in source


def test_base_template_declares_a_local_favicon() -> None:
    source = (TEMPLATES / "base.html").read_text(encoding="utf-8")

    assert 'rel="icon" href="/static/favicon.svg"' in source
    assert (TEMPLATES.parent / "static" / "favicon.svg").is_file()
    assert "onclick=" not in source
    assert "style=" not in source
    for symbol in ("🔒", "🌐", "✓", "🔴", "⚪"):
        assert symbol not in source


def test_machine_retirement_requires_recovery_evidence_and_exact_confirmation() -> None:
    page = (TEMPLATES / "machine_retire.html").read_text(encoding="utf-8")
    machines = (TEMPLATES / "machines.html").read_text(encoding="utf-8")

    assert 'action="/machines/{{ machine.machine_id }}/retirement-plan"' in page
    assert 'action="/machines/{{ machine.machine_id }}/retire"' in page
    assert "plan.spec.checkpoint_id" in page
    assert "plan.spec.checkpoint_verified_at" in page
    assert "plan.spec.config_revision" in page
    assert "plan.plan_hash" in page
    assert "expected_confirmation" in page
    assert "Any desired-state change invalidates this plan." in page
    assert "style=" not in page
    assert "onclick=" not in page
    assert 'href="/machines/{{ machine.machine_id }}/retire"' in machines


def test_shared_javascript_supports_explicit_disclosure_close_controls() -> None:
    source = (STATIC / "js/app.js").read_text(encoding="utf-8")

    assert "[data-disclosure-close]" in source
    assert "control.dataset.disclosureClose" in source
    assert "controlForDisclosure(target.id)" in source


def test_responsive_tables_contain_intrinsic_width_without_clipping_their_scroller() -> None:
    source = (STATIC / "css/main.css").read_text(encoding="utf-8")

    assert ".responsive-table" in source
    assert "contain: paint" in source
    assert "overflow-x: auto" in source


def test_services_uses_one_filterable_catalog_and_one_observed_runtime_table() -> None:
    catalog = (TEMPLATES / "services.html").read_text(encoding="utf-8")
    runtime = (TEMPLATES / "partials/containers.html").read_text(encoding="utf-8")
    router = (ROOT / "toolkit/webui/routers/services.py").read_text(encoding="utf-8")

    assert "Bookmarks" not in catalog
    assert 'data-table-filter="service-catalog-body"' in catalog
    assert 'data-table-option="service-catalog-body"' in catalog
    assert 'data-filter-limit="12"' in catalog
    assert 'data-table-expand="service-catalog-body"' in catalog
    assert 'id="service-catalog-body"' in catalog
    assert 'class="responsive-table service-catalog-wrap"' in catalog
    assert catalog.count('class="data-table service-catalog"') == 1
    assert 'id="containers-panel"' in catalog
    assert 'hx-trigger="load, every 20s"' in catalog
    assert "container-card" not in runtime
    assert 'class="data-table container-table"' in runtime
    assert 'data-table-filter="container-table-body"' in runtime
    assert 'data-table-option="container-table-body"' in runtime
    assert 'data-filter-default="attention"' in runtime
    assert 'hx-confirm="Stop this service?"' in runtime
    assert 'hx-confirm="Restart this service?"' in runtime
    assert "style=" not in catalog
    assert "style=" not in runtime
    assert "bookmark_groups=view.bookmark_groups" not in router
    for label in ("Service", "Category", "Description", "Routes", "Node"):
        assert f'data-label="{label}"' in catalog
    for label in ("Service", "Node", "State", "Health", "Image", "Actions"):
        assert f'data-label="{label}"' in runtime


def test_shared_javascript_filters_table_rows_and_reports_the_visible_count() -> None:
    source = (STATIC / "js/app.js").read_text(encoding="utf-8")

    assert "[data-table-filter]" in source
    assert "function applyTableView(body)" in source
    assert "function initializeTableViews(root = document)" in source
    assert "row.hidden = !visible" in source
    assert "data-filter-count" in source
    assert "data-table-option" in source
    assert "data-table-expand" in source
    assert "htmx:afterSwap" in source


def test_deployments_uses_compact_pipeline_disclosures_and_script_free_job_partials() -> None:
    page = (TEMPLATES / "deploy.html").read_text(encoding="utf-8")
    deploy_button = (TEMPLATES / "partials/deploy_button.html").read_text(encoding="utf-8")
    router = (ROOT / "toolkit/webui/routers/deploy.py").read_text(encoding="utf-8")
    deploy_job = (TEMPLATES / "partials/deploy_job.html").read_text(encoding="utf-8")
    identity_job = (TEMPLATES / "partials/identity_job.html").read_text(encoding="utf-8")
    dns_job = (TEMPLATES / "partials/dns_job.html").read_text(encoding="utf-8")

    assert 'class="deployment-summary"' in page
    assert 'data-disclosure-toggle="deploy-options"' in page
    assert 'id="deploy-options" class="deployment-options" hidden' in page
    assert 'data-disclosure-toggle="advanced-options"' in page
    assert 'id="advanced-options" class="deployment-advanced" hidden' in page
    assert 'hx-post="/deploy/jobs/deploy"' in deploy_button
    for endpoint in ("generate", "recover", "verify"):
        assert f'hx-post="/deploy/jobs/{endpoint}"' in page
    assert 'id="job-panel"' in page
    assert "Live controller job output" in page
    assert "active_jobs|length" not in page
    assert 'id="step-list" class="deployment-steps"' in page
    assert "onclick=" not in page
    assert "style=" not in page
    assert 'page_title="Deployments"' in router
    assert 'role="progressbar"' in deploy_job
    assert 'aria-valuenow="0"' in deploy_job
    assert "data-job-cancel" in deploy_job
    assert "progress-detail-{{ job_id }}" in deploy_job
    assert "Last error" not in deploy_job
    for partial in (deploy_job, identity_job, dns_job):
        assert "<script>" not in partial
        assert "style=" not in partial


def test_deploy_javascript_auto_attaches_jobs_without_selector_interpolation() -> None:
    source = (STATIC / "js/deploy.js").read_text(encoding="utf-8")

    assert "function attachWithin(root)" in source
    assert "root.querySelectorAll('[data-job-id]')" in source
    assert "'htmx:afterSwap'" in source
    assert "dataset.jobId === jobId" in source
    assert "item.dataset.step === step" in source
    assert "setAttribute('aria-valuenow'" in source
    assert "cancel.hidden = true" in source
    assert '`[data-job-id="${jobId}"]`' not in source
    assert '`#step-list li[data-step="${step}"]`' not in source


def test_network_and_secrets_share_compact_feedback_form_and_table_patterns() -> None:
    network = (TEMPLATES / "dns.html").read_text(encoding="utf-8")
    secrets = (TEMPLATES / "secrets.html").read_text(encoding="utf-8")
    dns_router = (ROOT / "toolkit/webui/routers/dns.py").read_text(encoding="utf-8")

    assert 'class="content-band network-address-band"' in network
    assert 'class="field-grid network-address-form"' in network
    assert 'class="responsive-table dns-records-wrap"' in network
    assert 'id="dns-job-panel"' in network
    assert 'hx-confirm="Remove DNS records that are no longer desired?"' in network
    assert 'page_title="Network"' in dns_router
    assert 'request.query_params.get("flash")' in dns_router
    assert 'request.query_params.get("error")' in dns_router
    assert "style=" not in network

    assert 'class="content-band secret-storage-band"' in secrets
    assert 'class="field-grid secret-fields"' in secrets
    assert 'class="responsive-table generated-secret-wrap"' in secrets
    assert 'hx-confirm="Rotate every generated secret?"' in secrets
    assert 'formaction="/secrets/generate"' in secrets
    assert 'formaction="/secrets/rotate"' in secrets
    assert "style=" not in secrets


def test_settings_uses_an_indexed_compact_form_without_duplicate_generation_actions() -> None:
    page = (TEMPLATES / "settings.html").read_text(encoding="utf-8")
    router = (ROOT / "toolkit/webui/routers/settings.py").read_text(encoding="utf-8")

    assert 'class="settings-layout"' in page
    assert 'class="settings-index"' in page
    assert "data-section-jump" in page
    assert 'class="settings-sections"' in page
    for section_id in (
        "general",
        "services",
        "ssh",
        "proxmox",
        "network",
    ):
        assert f'id="settings-{section_id}"' in page
        assert f'href="#settings-{section_id}"' in page
    assert 'class="choice-grid settings-choice-grid"' in page
    assert 'class="settings-action-bar"' in page
    assert 'formaction="/settings/generate"' in page
    assert "/settings/validate" not in page
    assert '@router.post("/settings/validate")' not in router
    assert "form-grid surface panel" not in page
    assert "style=" not in page
    assert "service_exposure" not in page
    assert "exp_" not in page

    for field_name in (
        "domain",
        "email",
        "timezone",
        "ssh_auth",
        "ssh_key_file",
        "proxmox_api_url",
        "proxmox_control_host",
        "proxmox_ssh_user",
        "proxmox_ssh_port",
        "proxmox_ssh_key_file",
        "proxmox_ssh_connect_timeout",
        "proxmox_ssh_command_timeout",
        "proxmox_ssh_retries",
        "proxmox_node",
        "proxmox_storage",
        "proxmox_template_datastore",
        "proxmox_template_url",
        "proxmox_template_checksum",
        "proxmox_tls_ca_file",
        "proxmox_provision_machines",
        "expose_internet",
        "dns_provider",
        "dns_public_ip",
        "dns_proxy",
    ):
        assert f'name="{field_name}"' in page

    for plugin_owned_field in (
        "media_server",
        "hw_transcode",
        "media_vpn",
        "media_tdarr",
        "tdarr_cpu_workers",
        "qbittorrent_port",
        "vpn_server_countries",
    ):
        assert f'name="{plugin_owned_field}"' not in page
    assert 'href="/services"' in page
    assert "values.machines" not in page

    for plugin_owned_field in (
        "media_cache",
        "music_sync",
        "music_sync_interval",
        "cache_cold_after",
        "cache_uplink_mbps",
    ):
        assert f'name="{plugin_owned_field}"' not in page


def test_shared_javascript_supports_the_mobile_settings_section_jump() -> None:
    source = (STATIC / "js/app.js").read_text(encoding="utf-8")

    assert "[data-section-jump]" in source
    assert "document.getElementById(control.value)" in source
    assert "target.scrollIntoView" in source


def test_operations_tables_are_self_identifying_on_mobile() -> None:
    page = (TEMPLATES / "operations.html").read_text(encoding="utf-8")
    styles = (STATIC / "css/main.css").read_text(encoding="utf-8")
    settings = (TEMPLATES / "settings.html").read_text(encoding="utf-8")

    assert page.count("operations-table") == 4
    for label in (
        "Node",
        "Freshness",
        "Recent snapshots",
        "Latest size",
        "Restore point",
        "Size",
        "Validation",
        "Host",
        "Address",
        "Services",
        "Status",
        "Actions",
        "Service",
        "Current",
        "Target",
        "Release notes",
    ):
        assert f'data-label="{label}"' in page
    assert 'class="alert alert-error" role="alert"' in page
    assert ".operations-table td::before" in styles
    assert "content: attr(data-label)" in styles
    assert "/settings/maintenance/run" not in settings


def test_overview_is_dependency_free_and_prioritizes_operational_state() -> None:
    page = (TEMPLATES / "dashboard.html").read_text(encoding="utf-8")
    router = (ROOT / "toolkit/webui/routers/dashboard.py").read_text(encoding="utf-8")

    assert 'class="overview-summary"' in page
    assert 'class="content-band overview-resources"' in page
    assert 'class="content-band overview-readiness"' in page
    assert 'class="resource-meter"' in page
    assert 'role="progressbar"' in page
    assert 'id="{{ name }}-history"' in page
    assert "('memory', 'Memory', metrics.memory_history_data)" in page
    assert "('disk', 'Disk', metrics.disk_history_data)" in page
    assert "<canvas" in page
    assert 'id="overview-attention-panel"' in page
    assert 'hx-get="/partials/dashboard/attention"' in page
    assert "partials/unhealthy.html" not in page
    assert "Next action" in page
    assert "Bookmarks" not in page
    assert "Categories" not in page
    assert "echarts" not in page.lower()
    assert "cdn.jsdelivr.net" not in page
    assert "window.__DASHBOARD_METRICS__" not in page
    assert "<script>" not in page
    assert "style=" not in page
    assert 'src="/static/js/dashboard.js?v={{ static_revision }}"' in page
    assert "chart_options" not in router
    assert 'page_title="Overview"' in router
    assert router.count("bookmark_groups=view.bookmark_groups") == 1


def test_local_dashboard_javascript_draws_and_refreshes_accessible_metrics() -> None:
    source = (STATIC / "js/dashboard.js").read_text(encoding="utf-8")

    assert "getContext('2d')" in source
    assert "function drawHistory(name, points)" in source
    assert "['cpu', 'memory', 'disk']" in source
    assert "function updateMetric(name, value)" in source
    assert "setAttribute('aria-valuenow'" in source
    assert "document.hidden" in source
    assert "fetch('/api/dashboard/metrics'" in source
    assert "AbortController" in source
    assert "refresh.inFlight" in source
    assert "echarts" not in source.lower()


def test_mobile_catalogs_and_machine_resources_use_readable_row_layouts() -> None:
    styles = (STATIC / "css/main.css").read_text(encoding="utf-8")

    assert ".service-catalog tr[data-filter-row]" in styles
    assert ".container-table tr[data-filter-row]" in styles
    assert ".generated-secret-table tr[data-filter-row]" in styles
    assert ".machine-table tr:not(.machine-edit-row)" in styles
    assert ".machine-edit-row td" in styles
    assert ".service-catalog tr[data-filter-row] td:nth-child(3) { display: none; }" in styles
    assert ".service-catalog tr[data-filter-row] td:nth-child(2)::before" in styles


def test_machines_has_a_dedicated_compact_inventory_and_complete_editor() -> None:
    page = (TEMPLATES / "machines.html").read_text(encoding="utf-8")
    fields = (TEMPLATES / "partials/machine_fields.html").read_text(encoding="utf-8")
    router = (ROOT / "toolkit/webui/routers/machines.py").read_text(encoding="utf-8")

    assert 'class="data-table machine-table"' in page
    assert 'data-disclosure-toggle="machine-create"' in page
    assert page.count('data-disclosure-group="machines"') >= 2
    assert 'action="/machines/{{ machine.machine_id }}"' in page
    assert 'formaction="/machines/{{ machine.machine_id }}/remove"' in page
    assert 'href="/deploy"' in page
    assert "style=" not in page
    assert "style=" not in fields
    for field_name in (
        "kind",
        "hostname",
        "address",
        "vmid",
        "labels",
        "cores",
        "memory_mb",
        "root_disk_gb",
        "data_disks",
        "private_bridge",
        "public_bridge",
        "gateway",
        "ssh_user",
        "ssh_port",
        "resource_limits",
        "cloud_image_url",
        "cloud_image_sha256",
    ):
        assert f'name="{field_name}"' in fields
    assert "JobRequest(idempotency_key=str(uuid.uuid4()), operation=GenerateOperation(validate_output=True))" in router


def test_generated_secrets_are_attention_first_and_filterable() -> None:
    page = (TEMPLATES / "secrets.html").read_text(encoding="utf-8")

    assert 'id="generated-secret-filter"' in page
    assert 'data-table-filter="generated-secret-body"' in page
    assert 'data-table-option="generated-secret-body"' in page
    assert 'id="generated-secret-body" data-filter-default="missing"' in page
    assert 'data-filter-value="{% if spec.is_configured %}configured{% else %}missing{% endif %}"' in page
    assert 'data-filter-count="generated-secret-body"' in page
    assert "data-filter-empty" in page
