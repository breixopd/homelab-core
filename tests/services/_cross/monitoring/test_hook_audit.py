from toolkit.core.deploy.hook_audit import HookSeverity, classify_hook_message


def test_classify_ok_messages():
    assert classify_hook_message("Prowlarr: added indexer 'YTS'") == HookSeverity.OK
    assert classify_hook_message("Vaultwarden: synced 12 login item(s) via API") == HookSeverity.OK


def test_classify_cloudflare_skip_is_ok():
    msg = "Prowlarr: skipped indexer '1337x' (Cloudflare blocked on VPN egress — add via FlareSolverr later)"
    assert classify_hook_message(msg) == HookSeverity.OK


def test_classify_benign_http_probes_are_ok():
    assert classify_hook_message("AdGuard: unexpected status HTTP 401 — manual check needed") == HookSeverity.OK
    assert classify_hook_message("DMS: DKIM keys generated (example.com, selector=mail, rsa-2048)") == HookSeverity.OK


def test_classify_critical_hook_error():
    assert classify_hook_message("Hook error: connection refused") == HookSeverity.CRITICAL
    assert classify_hook_message("Plugin error: service setup failed") == HookSeverity.CRITICAL


def test_classify_missing_security_runtime_as_critical():
    assert classify_hook_message("WARNING: Wazuh Manager: systemd unit not active") == HookSeverity.CRITICAL
    assert (
        classify_hook_message("Wazuh → ntfy integration: not installed (deploy security role)") == HookSeverity.CRITICAL
    )


def test_classify_service_setup_error_as_critical():
    assert classify_hook_message("Uptime Kuma: setup error (unable to connect)") == HookSeverity.CRITICAL


def test_classify_warning_prefix_is_warning():
    """A message explicitly prefixed with WARNING: that isn't allowlisted is a warning."""
    assert classify_hook_message("WARNING: something unusual happened") == HookSeverity.WARNING


def test_classify_not_active_is_warning():
    """Generic 'not active' messages (not allowlisted) classify as warnings."""
    assert classify_hook_message("WARNING: custom service: not active on this host") == HookSeverity.WARNING
