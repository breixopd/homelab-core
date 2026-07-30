from toolkit.services._arr import _prowlarr_flaresolverr_tag_and_proxy, configure_prowlarr_indexers


class _Response:
    def __init__(self, status_code: int, payload=None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text
        self.content = b"{}"

    def json(self):
        return self._payload


def test_configure_indexers_uses_shared_tag_proxy(monkeypatch):
    calls = []

    def fake_get(url, **_kwargs):
        if url.endswith("/indexer"):
            return _Response(200, [])
        if url.endswith("/indexer/schema"):
            return _Response(200, [{"id": 1, "name": "1337x", "definitionName": "1337x", "fields": []}])
        if url.endswith("/appProfile"):
            return _Response(200, [{"id": 7}])
        if url.endswith("/tag"):
            return _Response(200, [])
        if url.endswith("/indexerproxy/schema"):
            return _Response(
                200,
                [{"name": "FlareSolverr", "implementation": "FlareSolverr", "fields": [{"name": "host"}]}],
            )
        if url.endswith("/indexerproxy"):
            return _Response(200, [])
        raise AssertionError(url)

    def fake_write(method, url, **kwargs):
        calls.append((method, url, kwargs.get("json")))
        if method == "POST" and url.endswith("/tag"):
            return _Response(201, {"id": 9, "label": "flaresolverr"})
        if method == "POST" and url.endswith("/indexerproxy"):
            return _Response(202, {"id": 3})
        if method == "POST" and url.endswith("/indexer"):
            return _Response(201, {"id": 4})
        if method == "POST" and url.endswith("/command"):
            return _Response(201, {})
        raise AssertionError((method, url))

    monkeypatch.setattr("httpx.get", fake_get)
    monkeypatch.setattr("httpx.post", lambda url, **kwargs: fake_write("POST", url, **kwargs))
    logs = configure_prowlarr_indexers(
        "http://prowlarr:9696",
        "key",
        "http://flaresolverr:8191/v1",
        wanted_indexers=("1337x",),
    )

    indexer_payload = next(payload for method, url, payload in calls if method == "POST" and url.endswith("/indexer"))
    assert indexer_payload["tags"] == [9]
    assert all(field.get("name") != "flaresolverrUrl" for field in indexer_payload.get("fields", []))
    proxy_payload = next(
        payload for method, url, payload in calls if method == "POST" and url.endswith("/indexerproxy")
    )
    assert proxy_payload["name"] == "FlareSolverr"
    assert proxy_payload["tags"] == [9]
    assert proxy_payload["fields"][0] == {"name": "host", "value": "http://flaresolverr:8191"}
    assert any("added indexer" in message for message in logs)


def test_configure_indexers_preserves_no_proxy_mode(monkeypatch):
    posted = []

    monkeypatch.setattr(
        "httpx.get",
        lambda url, **_kwargs: _Response(
            200,
            [] if url.endswith(("/indexer", "/indexer/schema", "/appProfile")) else [],
        ),
    )
    monkeypatch.setattr(
        "httpx.post",
        lambda url, **kwargs: posted.append((url, kwargs["json"])) or _Response(201, {}),
    )
    configure_prowlarr_indexers("http://prowlarr:9696", "key", wanted_indexers=())
    assert not any(url.endswith(("/tag", "/indexerproxy")) for url, _payload in posted)


def test_existing_proxy_preserves_settings_and_tags(monkeypatch):
    updated = []

    def fake_get(url, **_kwargs):
        if url.endswith("/tag"):
            return _Response(200, [{"id": 9, "label": "flaresolverr"}])
        if url.endswith("/indexerproxy/schema"):
            return _Response(
                200,
                [
                    {
                        "name": "FlareSolverr",
                        "implementation": "FlareSolverr",
                        "fields": [
                            {"name": "host", "value": "http://localhost:8191"},
                            {"name": "requestTimeout", "value": 60},
                        ],
                    }
                ],
            )
        if url.endswith("/indexerproxy"):
            return _Response(
                200,
                [
                    {
                        "id": 3,
                        "name": "FlareSolverr",
                        "implementation": "FlareSolverr",
                        "tags": [4],
                        "fields": [
                            {"name": "host", "value": "http://old:8191"},
                            {"name": "requestTimeout", "value": 120},
                        ],
                    }
                ],
            )
        raise AssertionError(url)

    monkeypatch.setattr("httpx.get", fake_get)
    monkeypatch.setattr(
        "httpx.put",
        lambda url, **kwargs: updated.append((url, kwargs["json"])) or _Response(202, {}),
    )

    tag_id = _prowlarr_flaresolverr_tag_and_proxy(
        "http://prowlarr:9696", {"X-Api-Key": "key"}, "http://flaresolverr:8191"
    )

    assert tag_id == 9
    assert updated[0][1]["tags"] == [4, 9]
    assert updated[0][1]["fields"] == [
        {"name": "host", "value": "http://flaresolverr:8191"},
        {"name": "requestTimeout", "value": 120},
    ]


def test_proxy_failure_skips_protected_indexers(monkeypatch):
    posted = []

    def fake_get(url, **_kwargs):
        if url.endswith("/indexer"):
            return _Response(200, [])
        if url.endswith("/indexer/schema"):
            return _Response(
                200,
                [
                    {"name": "1337x", "definitionName": "1337x", "fields": []},
                    {"name": "YTS", "definitionName": "yts", "fields": []},
                ],
            )
        if url.endswith("/appProfile"):
            return _Response(200, [{"id": 7}])
        if url.endswith("/tag"):
            return _Response(500, [])
        raise AssertionError(url)

    monkeypatch.setattr("httpx.get", fake_get)
    monkeypatch.setattr(
        "httpx.post",
        lambda url, **kwargs: posted.append((url, kwargs["json"])) or _Response(201, {}),
    )

    logs = configure_prowlarr_indexers(
        "http://prowlarr:9696",
        "key",
        "http://flaresolverr:8191",
        wanted_indexers=("1337x", "yts"),
    )

    posted_indexers = [payload for url, payload in posted if url.endswith("/indexer")]
    assert [payload["definitionName"] for payload in posted_indexers] == ["yts"]
    assert "Prowlarr: FlareSolverr proxy reconciliation failed" in logs
    assert any("skipped protected indexer '1337x'" in message for message in logs)


def test_configure_indexers_removes_only_unwanted_public_definitions(monkeypatch):
    removed = []

    def fake_get(url, **_kwargs):
        if url.endswith("/indexer"):
            return _Response(
                200,
                [
                    {
                        "id": 1,
                        "name": "Renamed YTS",
                        "definitionName": "yts",
                        "privacy": "public",
                        "tags": [],
                    },
                    {
                        "id": 2,
                        "name": "Old Public",
                        "definitionName": "old-public",
                        "privacy": "public",
                        "tags": [],
                    },
                    {
                        "id": 3,
                        "name": "Private Tracker",
                        "definitionName": "private-tracker",
                        "privacy": "private",
                        "tags": [],
                    },
                ],
            )
        if url.endswith("/indexer/schema"):
            return _Response(200, [{"name": "YTS", "definitionName": "yts", "fields": []}])
        if url.endswith("/appProfile"):
            return _Response(200, [{"id": 7}])
        raise AssertionError(url)

    monkeypatch.setattr("httpx.get", fake_get)
    monkeypatch.setattr(
        "httpx.delete",
        lambda url, **_kwargs: removed.append(url) or _Response(204),
    )
    monkeypatch.setattr("toolkit.services._arr.trigger_prowlarr_indexer_sync", lambda *_args: True)

    logs = configure_prowlarr_indexers(
        "http://prowlarr:9696",
        "key",
        wanted_indexers=("yts",),
    )

    assert removed == ["http://prowlarr:9696/api/v1/indexer/2"]
    assert any("removed unconfigured public indexer 'Old Public'" in message for message in logs)
    assert logs[-1] == "Prowlarr: triggered indexer sync to Sonarr/Radarr"
