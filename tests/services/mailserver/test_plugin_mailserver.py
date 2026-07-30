"""Unit tests for mailserver plugin verify()."""

from __future__ import annotations

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig


def _plugin():
    for name in dir(mod := load_plugin("mailserver")):
        obj = getattr(mod, name)
        if isinstance(obj, type) and name.endswith("Plugin"):
            return obj()
    raise RuntimeError("no plugin")


def test_dkim_selector_matches_dms() -> None:
    from toolkit.core.ops.dns import email_dns_records

    record = next(
        r
        for r in email_dns_records("example.com", "192.0.2.10", dkim_txt="v=DKIM1; p=abc")
        if r.type == "TXT" and "domainkey" in r.name
    )
    assert record.name == "mail._domainkey.example.com"


def test_mail_data_manifest_preserves_dovecot_delivery_ownership() -> None:
    from toolkit.core.manifest.catalog import load_service_catalog

    manifest = next(item for item in load_service_catalog().manifests if item.name == "mailserver")
    mail_data = next(asset for asset in manifest.data_specs if asset.name == "mail-data")

    assert mail_data.manage_permissions is True
    assert (mail_data.host_uid, mail_data.host_gid) == (5000, 5000)


def test_imap_login_does_not_put_credentials_in_command(monkeypatch, tmp_path):
    from toolkit.services.mailserver import bootstrap

    calls = []

    def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return 0, "OK OK"

    monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)
    monkeypatch.setattr(
        "toolkit.services.mailserver.bootstrap._container_running",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "toolkit.core.manifest.placement.service_address",
        lambda *_args, **_kwargs: "10.10.10.12",
    )
    email, password = "admin@example.com", "mail-password-command-canary"
    ok, _detail = bootstrap._imap_login_works(Config(domain="example.com"), email, password, root=tmp_path)
    assert ok
    command = calls[0][0][2]
    assert email not in repr(command)
    assert password not in repr(command)
    assert "CERT_NONE" not in repr(command)
    assert "starttls" in repr(command)
    assert calls[0][1]["secret_environment"] == {
        "HOMELAB_IMAP_USER": email,
        "HOMELAB_IMAP_PASSWORD": password,
        "HOMELAB_MAIL_HOSTNAME": "mail.example.com",
    }


def test_mail_roundtrip_does_not_put_credentials_in_command(tmp_path):
    plugin = _plugin()
    calls = []
    email, password = "admin@example.com", "roundtrip-password-command-canary"

    def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return 0, "OK"

    check = plugin._check_mail_roundtrip(
        Config(domain="example.com", email=email, services=ServicesConfig(email=True)),
        {"SSO_USER_PASSWORD": password},
        "example.com",
        "10.10.10.12",
        tmp_path,
        fake_exec,
    )
    assert check.passed
    command = calls[0][0][2]
    assert email not in repr(command)
    assert password not in repr(command)
    assert "CERT_NONE" not in repr(command)
    assert calls[0][1]["secret_environment"]["HOMELAB_VERIFY_EMAIL"] == email
    assert calls[0][1]["secret_environment"]["HOMELAB_VERIFY_PASSWORD"] == password
    assert calls[0][1]["secret_environment"]["HOMELAB_VERIFY_HOSTNAME"] == "mail.example.com"


class TestMailserverVerify:
    def test_missing_enabled_container_fails(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(email=True))
        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_args, **_kwargs: False)

        checks = _plugin().verify(cfg, {}, "10.10.10.12", tmp_path)

        assert len(checks) == 1
        assert checks[0].check == "container"
        assert not checks[0].passed

    def test_dkim_dns_fails_closed_when_dns_tools_are_unavailable(self, monkeypatch):
        def missing_tool(*_args, **_kwargs):
            raise FileNotFoundError("dig")

        monkeypatch.setattr("subprocess.run", missing_tool)

        check = _plugin()._check_dkim_dns("example.com")

        assert not check.passed
        assert "unavailable" in check.detail.lower()

    def test_dkim_dns_rejects_non_dkim_txt_response(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *_args, **_kwargs: type("P", (), {"returncode": 0, "stdout": '"not a dkim record"', "stderr": ""})(),
        )

        check = _plugin()._check_dkim_dns("example.com")

        assert not check.passed
        assert "missing" in check.detail.lower()

    def test_dkim_dns_uses_host_fallback_when_dig_has_no_answer(self, monkeypatch):
        def query(command, **_kwargs):
            if command[0] == "dig":
                return type("P", (), {"returncode": 1, "stdout": "", "stderr": "SERVFAIL"})()
            return type("P", (), {"returncode": 0, "stdout": '"v=DKIM1; k=rsa; p=abc"', "stderr": ""})()

        monkeypatch.setattr("subprocess.run", query)

        check = _plugin()._check_dkim_dns("example.com")

        assert check.passed
        assert "found" in check.detail.lower()

    def test_dkim_dns_uses_configured_resolver_after_local_cache_miss(self, monkeypatch):
        def query(command, **_kwargs):
            answer = '"v=DKIM1; k=rsa; p=abc"' if "@1.1.1.1" in command else ""
            return type("P", (), {"returncode": 0, "stdout": answer, "stderr": ""})()

        monkeypatch.setattr("subprocess.run", query)

        check = _plugin()._check_dkim_dns("example.com", ("1.1.1.1",))

        assert check.passed

    def test_dkim_dns_requires_all_configured_resolvers_and_ignores_local_cache(self, monkeypatch):
        commands: list[list[str]] = []

        def query(command, **_kwargs):
            commands.append(command)
            answer = '"v=DKIM1; k=rsa; p=abc"' if "@1.1.1.1" in command else ""
            return type("P", (), {"returncode": 0, "stdout": answer, "stderr": ""})()

        monkeypatch.setattr("subprocess.run", query)

        check = _plugin()._check_dkim_dns("example.com", ("1.1.1.1", "8.8.8.8"))

        assert check.passed is False
        assert all(any(part.startswith("@") for part in command) for command in commands)

    def test_dmarc_dns_fails_closed_when_dns_tools_are_unavailable(self, monkeypatch):
        monkeypatch.setattr("subprocess.run", lambda *_args, **_kwargs: (_ for _ in ()).throw(FileNotFoundError()))

        check = _plugin()._check_dmarc_dns("example.com")

        assert not check.passed
        assert "unavailable" in check.detail.lower()

    def test_dmarc_dns_rejects_non_dmarc_txt_response(self, monkeypatch):
        monkeypatch.setattr(
            "subprocess.run",
            lambda *_args, **_kwargs: type("P", (), {"returncode": 0, "stdout": '"not a policy"', "stderr": ""})(),
        )

        check = _plugin()._check_dmarc_dns("example.com")

        assert not check.passed
        assert "missing" in check.detail.lower()

    def test_skips_when_email_disabled(self, tmp_path):
        cfg = Config(domain="example.com", services=ServicesConfig(email=False))
        checks = {c.check: c for c in _plugin().verify(cfg, {}, "10.10.10.12", tmp_path)}
        assert all(c.passed for c in checks.values())

    def test_skips_on_localhost(self, tmp_path):
        cfg = Config(domain="localhost", services=ServicesConfig(email=True))
        checks = _plugin().verify(cfg, {"SSO_USER_PASSWORD": "x"}, "10.10.10.12", tmp_path)
        assert all(c.passed for c in checks)
        assert "localhost" in checks[0].detail

    def test_dms_health_and_roundtrip(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(email=True), email="admin@example.com")
        secrets = {"SSO_USER_PASSWORD": "secret"}

        def fake_exec(_cfg, container, cmd, _ip, _root, **kw):
            if container != "mailserver":
                return 1, ""
            joined = " ".join(cmd) if isinstance(cmd, list) else cmd
            if "dms-healthcheck" in joined:
                return 0, ""
            if "supervisorctl" in joined or "pgrep" in joined:
                return 0, "postfix RUNNING\ndovecot RUNNING"
            if "postqueue" in joined or "mailq" in joined:
                return 0, "Mail queue is empty"
            if "python3" in joined and "-c" in joined:
                return 0, "OK"
            return 0, ""

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)
        imap_calls = []
        monkeypatch.setattr(
            "toolkit.services.mailserver.bootstrap._imap_login_works",
            lambda *_a, **kwargs: imap_calls.append(kwargs) or (True, "IMAP ok"),
        )
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: type("P", (), {"returncode": 0, "stdout": "v=DKIM1; k=rsa; p=abc", "stderr": ""})(),
        )

        checks = {c.check: c for c in _plugin().verify(cfg, secrets, "10.10.10.12", tmp_path)}
        assert checks["dms_health"].passed
        assert checks["client_tls"].passed
        assert checks["mail_roundtrip"].passed
        assert checks["imap_login"].passed
        assert imap_calls == [{"root": tmp_path}]

    def test_roundtrip_failure(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(email=True))
        secrets = {"SSO_USER_PASSWORD": "secret"}

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr(
            "toolkit.services.sdk.docker_exec_on_vm",
            lambda *_a, **_k: (0, "MISSING"),
        )
        monkeypatch.setattr(
            "toolkit.services.mailserver.bootstrap._imap_login_works",
            lambda *_a, **_k: (True, "ok"),
        )
        monkeypatch.setattr(
            "subprocess.run",
            lambda *a, **k: type("P", (), {"returncode": 0, "stdout": "", "stderr": ""})(),
        )
        checks = {c.check: c for c in _plugin().verify(cfg, secrets, "10.10.10.12", tmp_path)}
        assert not checks["mail_roundtrip"].passed

    def test_mail_queue_unreadable_fails(self, tmp_path):
        check = _plugin()._check_mail_queue(
            Config(),
            "10.10.10.12",
            tmp_path,
            lambda *_args, **_kwargs: (1, ""),
        )

        assert not check.passed


def test_dms_state_permission_repair_uses_image_ids(monkeypatch):
    from toolkit.services.mailserver.bootstrap import repair_dms_state_permissions

    calls: list[str] = []

    def fake_ssh(_cfg, command, **_kwargs):
        calls.append(command)
        return (0, "105 107 108" if len(calls) == 1 else "DMS: repaired Postfix queue permissions", "")

    monkeypatch.setattr("toolkit.services.mailserver.bootstrap._mail_ssh_exec", fake_ssh)
    logs = repair_dms_state_permissions(Config(domain="example.com"), state_path="/opt/homelab/data/dms/state")

    assert logs == ["DMS: repaired Postfix queue permissions"]
    assert "id -u postfix" in calls[0]
    assert "105:0" in calls[1]
    assert "108" in calls[1]


def test_dms_state_permission_repair_rejects_unsafe_path(monkeypatch):
    from toolkit.services.mailserver.bootstrap import repair_dms_state_permissions

    monkeypatch.setattr(
        "toolkit.services.mailserver.bootstrap._mail_ssh_exec",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("unexpected SSH call")),
    )

    assert "unsafe path" in repair_dms_state_permissions(Config(domain="example.com"), state_path="../state")[0]
