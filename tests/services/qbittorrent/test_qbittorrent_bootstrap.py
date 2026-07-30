from __future__ import annotations

import json
from pathlib import Path

import yaml
from toolkit.services.qbittorrent.bootstrap import _ensure_qbittorrent_save_path


def test_manifest_declares_vpn_runtime_for_plugin_reconciliation() -> None:
    manifest = yaml.safe_load(
        (Path(__file__).parents[3] / "toolkit/services/qbittorrent/service.yaml").read_text(encoding="utf-8")
    )
    assert manifest["runtimes"]["qbittorrent-vpn"]["compose_profile"] == "media-vpn"


def test_save_path_reconciliation_never_uses_a_shell(monkeypatch) -> None:
    exec_calls: list[list[str]] = []
    curl_calls: list[tuple[str, dict]] = []
    curl_responses: list[tuple[int, str]] = [
        (0, ""),
        (0, json.dumps({"save_path": "/downloads"})),
        (0, ""),
    ]
    exec_responses: list[tuple[int, str]] = [(0, ""), (0, "")]

    def fake_exec(_container: str, command: list[str], **_kwargs):
        exec_calls.append(command)
        return exec_responses.pop(0)

    def fake_curl(_container: str, url: str, **kwargs):
        curl_calls.append((url, kwargs))
        return curl_responses.pop(0)

    monkeypatch.setattr("toolkit.services.qbittorrent.bootstrap.docker_exec", fake_exec)
    monkeypatch.setattr("toolkit.services.qbittorrent.bootstrap.docker_curl", fake_curl)
    save_path = "/data/path;touch /tmp/injected"

    assert _ensure_qbittorrent_save_path("qbittorrent", "user&admin", "p'ass&word", save_path)
    assert all(command[:2] != ["sh", "-c"] for command in exec_calls)
    assert ["test", "-d", save_path] in exec_calls
    assert ["test", "-w", save_path] in exec_calls
    login_url, login = curl_calls[0]
    assert login_url.endswith("/auth/login")
    assert login["method"] == "POST"
    assert login["body"] == "username=user%26admin&password=p%27ass%26word"
    assert login["cookie_jar"] == "/tmp/qbt-cookies"
    update_url, update = curl_calls[-1]
    assert update_url.endswith("/app/setPreferences")
    assert update["method"] == "POST"
    assert update["cookie_file"] == "/tmp/qbt-cookies"
    assert update["body"].startswith("json=")
