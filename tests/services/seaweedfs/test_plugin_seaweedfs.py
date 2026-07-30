"""Unit tests for seaweedfs plugin verify()."""

from __future__ import annotations

import json

from tests.helpers.plugins import load_plugin
from toolkit.core.config.config import Config, ServicesConfig


def _plugin():
    module = load_plugin("seaweedfs")
    for name in dir(module):
        if not name.endswith("Plugin") or name == "ServicePlugin":
            continue
        obj = getattr(module, name)
        if isinstance(obj, type):
            return obj()
    raise RuntimeError("no seaweedfs plugin")


def test_seaweedfs_post_start_bootstraps_buckets(monkeypatch):
    monkeypatch.setattr(
        "toolkit.services.seaweedfs.bootstrap.bootstrap_seaweedfs_buckets",
        lambda cfg, secrets: ["buckets ready"],
    )

    assert _plugin().post_start(Config(), {}) == ["buckets ready"]


def test_bucket_bootstrap_retries_s3_readiness(monkeypatch):
    from toolkit.services.seaweedfs import bootstrap

    calls = []

    def fake_shell(_secrets, command):
        calls.append(command)
        if len(calls) < 3:
            return 1, "filer is starting"
        return 0, "nextcloud\tadmin\nimmich\tadmin\nbackups\tadmin\n"

    monkeypatch.setattr(bootstrap, "_weed_shell", fake_shell)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _seconds: None)
    cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))

    assert bootstrap.bootstrap_seaweedfs_buckets(cfg, {}) == [
        "SeaweedFS: buckets already present (backups, immich, nextcloud)"
    ]
    assert calls == ["s3.bucket.list"] * 3


def test_bucket_bootstrap_retries_bucket_creation(monkeypatch):
    from toolkit.services.seaweedfs import bootstrap

    calls = []

    def fake_shell(_secrets, command):
        calls.append(command)
        if command == "s3.bucket.list":
            return 0, ""
        if calls.count(command) < 3:
            return 1, "S3 control plane is starting"
        return 0, "created"

    monkeypatch.setattr(bootstrap, "_weed_shell", fake_shell)
    monkeypatch.setattr(bootstrap.time, "sleep", lambda _seconds: None)
    cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))

    logs = bootstrap.bootstrap_seaweedfs_buckets(cfg, {}, buckets=("nextcloud",))
    assert logs == ["SeaweedFS: created buckets nextcloud"]
    assert calls == [
        "s3.bucket.list",
        "s3.bucket.create -name nextcloud -owner admin",
        "s3.bucket.create -name nextcloud -owner admin",
        "s3.bucket.create -name nextcloud -owner admin",
    ]


def test_s3_auth_probe_does_not_put_credentials_in_command(monkeypatch, tmp_path):
    from toolkit.services.seaweedfs.plugin import _check_seaweedfs_s3_auth

    calls = []
    monkeypatch.setattr(
        "toolkit.services.sdk.docker_curl",
        lambda *_args, **_kwargs: (1, "Forbidden"),
    )

    def fake_exec(*args, **kwargs):
        calls.append((args, kwargs))
        return 0, "bucket\n"

    monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", fake_exec)
    access, secret = "access-command-canary", "secret-command-canary"
    check = _check_seaweedfs_s3_auth(
        Config(domain="example.com", services=ServicesConfig(cloud=True)),
        "10.10.10.12",
        tmp_path,
        {"SEAWEEDFS_S3_ACCESS_KEY": access, "SEAWEEDFS_S3_SECRET_KEY": secret},
    )
    assert check.passed
    command = calls[0][0][2]
    assert access not in repr(command)
    assert secret not in repr(command)
    assert calls[0][1]["secret_environment"] == {
        "AWS_ACCESS_KEY_ID": access,
        "AWS_SECRET_ACCESS_KEY": secret,
    }


class TestSeaweedfsVerify:
    def test_skips_localhost(self, tmp_path):
        cfg = Config(domain="localhost", services=ServicesConfig(cloud=True))
        checks = _plugin().verify(cfg, {}, "10.10.10.12", tmp_path)
        assert checks[0].passed

    def test_cluster_and_filer(self, tmp_path, monkeypatch):
        cfg = Config(domain="example.com", services=ServicesConfig(cloud=True))

        def fake_curl(_cfg, _ip, container, url, **_kw):
            if "9333/cluster/status" in url:
                return 0, json.dumps({"Leader": "127.0.0.1:9333", "IsLeader": True})
            if ":8888/" in url:
                return 0, "<html>filer</html>"
            if ":8333/status" in url:
                return 0, "ok"
            if ":8333/" == url.split("localhost")[-1] if "localhost" in url else False:
                return 1, "Forbidden"
            if url.endswith("8333/"):
                return 1, "Forbidden"
            return 1, ""

        monkeypatch.setattr("toolkit.services.sdk.container_exists_on_vm", lambda *_a, **_k: True)
        monkeypatch.setattr("toolkit.services.sdk.docker_curl", fake_curl)
        monkeypatch.setattr("toolkit.services.sdk.docker_exec_on_vm", lambda *_a, **_k: (1, "no aws"))
        monkeypatch.setattr(
            "toolkit.services.sdk.ssh_on_vm",
            lambda _cfg, source_ip, *_a, **_k: (
                0,
                "OPEN" if source_ip == cfg.node_ip("infra") else "CLOSED",
                "",
            ),
        )
        sw_mod = load_plugin("seaweedfs")
        monkeypatch.setattr(
            sw_mod,
            "_check_seaweedfs_buckets",
            lambda *_a, **_k: type("VC", (), {"passed": True, "check": "buckets", "detail": "ok"})(),
        )

        checks = {
            c.check: c
            for c in _plugin().verify(
                cfg,
                {"SEAWEEDFS_S3_ACCESS_KEY": "k", "SEAWEEDFS_S3_SECRET_KEY": "s"},
                "10.10.10.12",
                tmp_path,
            )
        }
        assert checks["cluster_leader"].passed
        assert checks["filer"].passed
        assert checks["s3_auth"].passed
        assert checks["s3_host_exposure"].passed
        assert "media" in checks["s3_host_exposure"].detail
        assert "infra" in checks["s3_host_exposure"].detail
