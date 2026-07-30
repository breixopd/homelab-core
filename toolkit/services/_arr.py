"""Servarr cross-service wiring — prowlarr↔sonarr↔radarr↔bazarr↔seerr.

Shared module for the *arr family automation: registering Prowlarr apps,
configuring download clients + root folders, wiring Bazarr providers,
and headlessly bootstrapping Seerr. These touch multiple *arr services
together, so no single plugin owns them — prowlarr/sonarr/radarr/seerr/bazarr
plugins import from here.

Extracted from ``toolkit.core.ops.automation`` so service authors editing
*arr plugins stay within ``toolkit/services/``.
"""

from __future__ import annotations

import json as _json
import time
from copy import deepcopy
from pathlib import Path

import httpx
from defusedxml import ElementTree


def wire_arr_notifications(arr_url: str, arr_api_key: str, ntfy_url: str, ntfy_topic: str) -> bool:
    """Configure Sonarr/Radarr to send notifications via ntfy webhook.

    Build the payload from the installed service's schema so upgrades can add
    required fields without breaking reconciliation.
    """
    try:
        headers = {"X-Api-Key": arr_api_key}
        existing = httpx.get(
            f"{arr_url}/api/v3/notification",
            headers=headers,
            timeout=15,
        )
        current = None
        if existing.status_code == 200 and isinstance(existing.json(), list):
            current = next(
                (item for item in existing.json() if isinstance(item, dict) and item.get("name") == "ntfy"),
                None,
            )

        schemas = httpx.get(
            f"{arr_url}/api/v3/notification/schema",
            headers=headers,
            timeout=15,
        )
        if schemas.status_code != 200 or not isinstance(schemas.json(), list):
            return False
        webhook_schema = next(
            (item for item in schemas.json() if isinstance(item, dict) and item.get("implementation") == "Webhook"),
            None,
        )
        if webhook_schema is None:
            return False

        payload = deepcopy(webhook_schema)
        payload["name"] = "ntfy"
        payload["onGrab"] = True
        payload["onDownload"] = True
        payload["onUpgrade"] = True
        desired_url = f"{ntfy_url.rstrip('/')}/{ntfy_topic}"
        for field in payload.get("fields", []):
            if field.get("name") == "url":
                field["value"] = desired_url
            elif field.get("name") == "method":
                field["value"] = 1

        if current:
            current_fields = {
                field.get("name"): field.get("value") for field in current.get("fields", []) if isinstance(field, dict)
            }
            if (
                current_fields.get("url") == desired_url
                and current_fields.get("method") == 1
                and all(current.get(event) is True for event in ("onGrab", "onDownload", "onUpgrade"))
            ):
                return True
            payload["id"] = current["id"]
            resp = httpx.put(
                f"{arr_url}/api/v3/notification/{current['id']}",
                json=payload,
                headers=headers,
                timeout=15,
            )
        else:
            resp = httpx.post(
                f"{arr_url}/api/v3/notification",
                json=payload,
                headers=headers,
                timeout=15,
            )
        return resp.status_code in (200, 201, 202)
    except httpx.HTTPError:
        return False


def wire_prowlarr_apps(
    prowlarr_url: str,
    prowlarr_api_key: str,
    sonarr_url: str,
    sonarr_api_key: str,
    radarr_url: str,
    radarr_api_key: str,
) -> list[str]:
    """Register Sonarr and Radarr as applications in Prowlarr so indexers sync automatically.

    Returns list of log messages.
    """
    logs: list[str] = []
    headers = {"X-Api-Key": prowlarr_api_key}

    def _app_exists(name: str) -> bool:
        try:
            resp = httpx.get(f"{prowlarr_url}/api/v1/applications", headers=headers, timeout=10)
            if resp.status_code == 200:
                return any(a.get("name") == name for a in resp.json())
        except httpx.HTTPError:
            pass
        return False

    apps = [
        {
            "name": "Sonarr",
            "implementation": "Sonarr",
            "configContract": "SonarrSettings",
            "syncLevel": "fullSync",
            "animeSyncLevel": "disabled",
            "fields": [
                {"name": "prowlarrUrl", "value": "http://prowlarr:9696"},
                {"name": "baseUrl", "value": sonarr_url},
                {"name": "apiKey", "value": sonarr_api_key},
                {"name": "syncCategories", "value": [5000, 5010, 5020, 5030, 5040, 5045, 5050]},
            ],
            "tags": [],
        },
        {
            "name": "Radarr",
            "implementation": "Radarr",
            "configContract": "RadarrSettings",
            "syncLevel": "fullSync",
            "animeSyncLevel": "disabled",
            "fields": [
                {"name": "prowlarrUrl", "value": "http://prowlarr:9696"},
                {"name": "baseUrl", "value": radarr_url},
                {"name": "apiKey", "value": radarr_api_key},
                {"name": "syncCategories", "value": [2000, 2010, 2020, 2030, 2040, 2045, 2050, 2060, 2070, 2080]},
            ],
            "tags": [],
        },
    ]

    for app in apps:
        name = str(app["name"])
        if _app_exists(name):
            logs.append(f"Prowlarr: {name} already registered — skipping")
            continue
        try:
            resp = httpx.post(
                f"{prowlarr_url}/api/v1/applications",
                json=app,
                headers=headers,
                timeout=15,
            )
            if resp.status_code in (200, 201):
                logs.append(f"Prowlarr: registered {name} app — indexers will sync automatically")
            else:
                logs.append(f"Prowlarr: failed to register {name} ({resp.status_code}): {resp.text[:100]}")
        except httpx.HTTPError as e:
            logs.append(f"Prowlarr: could not reach {name} endpoint: {e}")

    return logs


def extract_servarr_api_key(config_xml_path: str | Path) -> str | None:
    """Read API key from Sonarr/Radarr/Prowlarr config.xml."""
    try:
        tree = ElementTree.parse(str(config_xml_path))
        api_key = tree.find(".//ApiKey")
        if api_key is not None and api_key.text:
            return api_key.text.strip()
    except Exception:
        pass
    return None


def reconcile_servarr_api_key(root: Path, service: str, secrets: dict[str, str], env_key: str) -> str:
    """Prefer live API key from container config.xml or generated .env over secrets when present."""
    config_paths = {
        "sonarr": root / "config" / "sonarr" / "config.xml",
        "radarr": root / "config" / "radarr" / "config.xml",
        "prowlarr": root / "config" / "prowlarr" / "config.xml",
    }
    path = config_paths.get(service)
    if path and path.is_file():
        found = extract_servarr_api_key(path)
        if found:
            return found

    # Fallback: check the service owner first, then other generated node bundles.
    from toolkit.core.config.storage import env_path as _env_path

    nodes: list[str] = []
    try:
        from toolkit.core.config.config import load_config
        from toolkit.core.manifest.placement import service_node

        cfg = load_config(root / "config.yaml")
        owner = service_node(cfg, service)
        nodes = [owner, *(node for node in cfg.enabled_nodes if node != owner)]
    except (OSError, ValueError, KeyError):
        generated = root / "generated"
        if generated.is_dir():
            nodes = sorted(path.name for path in generated.iterdir() if path.is_dir())

    for node in nodes:
        env_file = _env_path(node, root)
        if env_file.is_file():
            for line in env_file.read_text().splitlines():
                line = line.strip()
                if line.startswith(f"{env_key}="):
                    # Guest bundles quote secrets for shell safety; normalize
                    # that representation before sending API credentials.
                    val = line.split("=", 1)[1].strip().strip("'\"")
                    if val:
                        return val

    return secrets.get(env_key, "")


def wait_for_arr_api(base_url: str, api_key: str, *, timeout: int = 60, api_version: str = "v3") -> bool:
    """Wait until Servarr/Prowlarr HTTP API accepts the API key."""
    deadline = time.time() + timeout
    headers = {"X-Api-Key": api_key}
    status_path = f"/api/{api_version}/system/status"
    while time.time() < deadline:
        try:
            resp = httpx.get(f"{base_url}{status_path}", headers=headers, timeout=5)
            if resp.status_code == 200:
                return True
            if resp.status_code == 401:
                return False
        except httpx.HTTPError:
            pass
        time.sleep(3)
    return False


def _prowlarr_default_app_profile_id(prowlarr_url: str, headers: dict[str, str]) -> int:
    try:
        resp = httpx.get(f"{prowlarr_url}/api/v1/appProfile", headers=headers, timeout=10)
        if resp.status_code == 200:
            profiles = resp.json()
            if profiles:
                return int(profiles[0].get("id", 1))
    except (httpx.HTTPError, ValueError, TypeError):
        pass
    return 1


def extract_bazarr_api_key(config_path: str | Path) -> str | None:
    """Read API key from Bazarr config.yaml (generated on first startup)."""
    path = Path(config_path)
    if not path.is_file():
        return None
    try:
        import yaml

        data = yaml.safe_load(path.read_text()) or {}
        if isinstance(data, dict):
            auth = data.get("auth") or {}
            if isinstance(auth, dict) and auth.get("apikey"):
                return str(auth["apikey"]).strip()
            if data.get("apikey"):
                return str(data["apikey"]).strip()
    except Exception:
        pass
    return None


def configure_arr_root_folder(arr_url: str, api_key: str, path: str) -> bool:
    """Configure root folder for Sonarr or Radarr. Idempotent — skips if exists.

    Args:
        arr_url: Base URL (e.g. http://sonarr:8989)
        api_key: Servarr API key
        path: Root folder path (e.g. /data/media/tv)
    """
    try:
        existing = httpx.get(
            f"{arr_url}/api/v3/rootFolder",
            headers={"X-Api-Key": api_key},
            timeout=10,
        )
        if existing.status_code == 200:
            for rf in existing.json():
                if rf.get("path") == path:
                    return True  # Already configured

        resp = httpx.post(
            f"{arr_url}/api/v3/rootFolder",
            json={"path": path},
            headers={"X-Api-Key": api_key},
            timeout=10,
        )
        return resp.status_code in (200, 201)
    except httpx.HTTPError:
        return False


def configure_arr_download_client(
    arr_url: str,
    api_key: str,
    qbit_host: str = "qbittorrent",
    qbit_port: int = 8080,
    qbit_user: str = "",
    qbit_password: str = "",
    category: str = "",
) -> bool:
    """Configure qBittorrent as download client for Sonarr or Radarr. Idempotent.

    Always runs ``/api/v3/downloadclient/test`` — M2 requires a passing test, not just presence.
    """
    headers = {"X-Api-Key": api_key}

    def _apply_qbit_fields(client_obj: dict) -> dict:
        if not isinstance(client_obj, dict):
            return client_obj
        fields = client_obj.get("fields") or []
        for fld in fields:
            if not isinstance(fld, dict):
                continue
            name = fld.get("name")
            if name == "host":
                fld["value"] = qbit_host
            elif name == "port":
                fld["value"] = qbit_port
            elif name == "username" and qbit_user:
                fld["value"] = qbit_user
            elif name == "password" and qbit_password:
                fld["value"] = qbit_password
            elif name == "tvCategory" and category:
                fld["value"] = category
            elif name == "movieCategory" and category:
                fld["value"] = category
        return client_obj

    def _test_client(client_obj: dict) -> bool:
        try:
            # Include id for existing clients so Sonarr doesn't reject with
            # "Should be unique" on the /test endpoint.
            test_resp = httpx.post(
                f"{arr_url}/api/v3/downloadclient/test",
                json=client_obj,
                headers=headers,
                timeout=15,
            )
            if test_resp.status_code in (200, 201):
                return True
            return False
        except httpx.HTTPError:
            return False

    try:
        existing = httpx.get(f"{arr_url}/api/v3/downloadclient", headers=headers, timeout=10)
        if existing.status_code == 200:
            for dc in existing.json():
                if dc.get("implementation") == "QBittorrent":
                    updated = _apply_qbit_fields(dict(dc))
                    if updated.get("id"):
                        put_resp = httpx.put(
                            f"{arr_url}/api/v3/downloadclient/{updated['id']}",
                            json=updated,
                            headers=headers,
                            timeout=15,
                        )
                        if put_resp.status_code not in (200, 202):
                            return False
                    return _test_client(updated)

        fields = [
            {"name": "host", "value": qbit_host},
            {"name": "port", "value": qbit_port},
        ]
        if qbit_user:
            fields.append({"name": "username", "value": qbit_user})
        if qbit_password:
            fields.append({"name": "password", "value": qbit_password})
        if category:
            fields.append({"name": "tvCategory", "value": category})
            fields.append({"name": "movieCategory", "value": category})

        payload = {
            "name": "qBittorrent",
            "implementation": "QBittorrent",
            "configContract": "QBittorrentSettings",
            "protocol": "torrent",
            "enable": True,
            "priority": 1,
            "tags": [],
            "fields": fields,
        }
        resp = httpx.post(
            f"{arr_url}/api/v3/downloadclient",
            json=payload,
            headers=headers,
            timeout=10,
        )
        if resp.status_code not in (200, 201):
            return False
        created = resp.json() if resp.content else payload
        return _test_client(created if isinstance(created, dict) else payload)
    except httpx.HTTPError:
        return False


# ── Prowlarr public indexer auto-config (schema API) ────────────

# Indexers that benefit from FlareSolverr (Cloudflare-protected sites)
_FLARESOLVERR_INDEXERS = {"1337x", "thepiratebay", "eztv", "limetorrents", "torrentgalaxy", "torrentgalaxyclone"}


def _normalize_flaresolverr_url(flaresolverr_url: str) -> str:
    """Prowlarr expects the FlareSolverr base URL without the ``/v1`` path."""
    url = (flaresolverr_url or "").strip().rstrip("/")
    if url.endswith("/v1"):
        return url[:-3]
    return url


def _prowlarr_flaresolverr_tag_and_proxy(
    prowlarr_url: str, headers: dict[str, str], flaresolverr_base: str
) -> int | None:
    """Reconcile Prowlarr's tag-based FlareSolverr proxy.

    Prowlarr applies indexer proxies through tags; ``flaresolverrUrl`` is not
    an indexer field in current releases. Return the shared tag id when
    reconciliation succeeds.
    """
    try:
        tags_response = httpx.get(f"{prowlarr_url}/api/v1/tag", headers=headers, timeout=10)
        tags = tags_response.json() if tags_response.status_code == 200 else []
        tag = next((item for item in tags if str(item.get("label", "")).lower() == "flaresolverr"), None)
        if tag is None:
            created = httpx.post(
                f"{prowlarr_url}/api/v1/tag",
                json={"label": "flaresolverr"},
                headers=headers,
                timeout=10,
            )
            if created.status_code not in (200, 201):
                return None
            tag = created.json()
        tag_id = int(tag.get("id"))

        schema_response = httpx.get(f"{prowlarr_url}/api/v1/indexerproxy/schema", headers=headers, timeout=10)
        schemas = schema_response.json() if schema_response.status_code == 200 else []
        schema = next(
            (
                item
                for item in schemas
                if str(item.get("implementation", item.get("name", ""))).lower() == "flaresolverr"
            ),
            None,
        )
        if schema is None:
            return None
        fields = deepcopy(schema.get("fields") or [])
        for field in fields:
            if field.get("name") == "host":
                field["value"] = flaresolverr_base
        desired = {k: v for k, v in schema.items() if k not in ("id",)}
        desired["name"] = str(desired.get("name") or "FlareSolverr")
        desired["fields"] = fields
        desired["tags"] = [tag_id]

        existing_response = httpx.get(f"{prowlarr_url}/api/v1/indexerproxy", headers=headers, timeout=10)
        existing = existing_response.json() if existing_response.status_code == 200 else []
        proxy = next(
            (
                item
                for item in existing
                if str(item.get("implementation", item.get("name", ""))).lower() == "flaresolverr"
            ),
            None,
        )
        if proxy is None:
            response = httpx.post(
                f"{prowlarr_url}/api/v1/indexerproxy", json=desired, headers=headers, timeout=15
            )
            if response.status_code not in (200, 201, 202):
                return None
        else:
            current_fields = {str(f.get("name")): f.get("value") for f in proxy.get("fields") or []}
            current_tags = [int(value) for value in proxy.get("tags") or [] if str(value).isdigit()]
            if current_fields.get("host") != flaresolverr_base or tag_id not in current_tags:
                payload = {k: v for k, v in proxy.items() if k != "id"}
                payload["tags"] = [*current_tags, tag_id] if tag_id not in current_tags else current_tags
                payload["fields"] = deepcopy(proxy.get("fields") or [])
                for field in payload["fields"]:
                    if field.get("name") == "host":
                        field["value"] = flaresolverr_base
                response = httpx.put(
                    f"{prowlarr_url}/api/v1/indexerproxy/{proxy.get('id')}",
                    json=payload,
                    headers=headers,
                    timeout=15,
                )
                if response.status_code not in (200, 202):
                    return None
        return tag_id
    except (httpx.HTTPError, ValueError, TypeError, KeyError):
        return None


def trigger_prowlarr_indexer_sync(prowlarr_url: str, api_key: str) -> bool:
    """Push configured Prowlarr indexers to linked Sonarr/Radarr apps."""
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    try:
        resp = httpx.post(
            f"{prowlarr_url}/api/v1/command",
            headers=headers,
            json={"name": "ApplicationIndexerSync"},
            timeout=30,
        )
        return resp.status_code in (200, 201)
    except httpx.HTTPError:
        return False


def reconcile_prowlarr_application_urls(prowlarr_url: str, api_key: str) -> list[str]:
    """Ensure Sonarr/Radarr apps point at the in-network Prowlarr hostname. Idempotent."""
    logs: list[str] = []
    headers = {"X-Api-Key": api_key, "Content-Type": "application/json"}
    expected = "http://prowlarr:9696"
    try:
        resp = httpx.get(f"{prowlarr_url}/api/v1/applications", headers=headers, timeout=10)
        if resp.status_code != 200:
            return logs
        for app in resp.json():
            if not isinstance(app, dict):
                continue
            fields = app.get("fields") or []
            current = next((f.get("value") for f in fields if f.get("name") == "prowlarrUrl"), "")
            if current == expected:
                continue
            for field in fields:
                if field.get("name") == "prowlarrUrl":
                    field["value"] = expected
            payload = {k: v for k, v in app.items() if k != "id"}
            put = httpx.put(
                f"{prowlarr_url}/api/v1/applications/{app.get('id')}",
                json=payload,
                headers=headers,
                timeout=15,
            )
            if put.status_code in (200, 202):
                logs.append(f"Prowlarr: repaired {app.get('name', 'app')} prowlarrUrl → {expected}")
    except httpx.HTTPError as exc:
        logs.append(f"Prowlarr: could not reconcile application URLs ({exc})")
    return logs


def configure_prowlarr_indexers(
    prowlarr_url: str,
    api_key: str,
    flaresolverr_url: str = "",
    wanted_indexers: tuple[str, ...] = (),
) -> list[str]:
    """Reconcile public indexers in Prowlarr using the schema API. Idempotent.

    Uses GET /api/v1/indexer/schema to discover built-in definitions, then POSTs
    each wanted indexer and removes public definitions omitted from the desired
    list. Private indexers are operator-owned and remain untouched. Includes
    ``"tags": []`` to avoid Prowlarr bug #1080.

    Returns list of log messages.
    """
    logs: list[str] = []
    headers = {"X-Api-Key": api_key}
    flaresolverr_base = _normalize_flaresolverr_url(flaresolverr_url)

    # Get existing indexers to avoid duplicates and repair protected ones.
    existing_indexers: list[dict] = []
    try:
        resp = httpx.get(f"{prowlarr_url}/api/v1/indexer", headers=headers, timeout=10)
        if resp.status_code == 200:
            existing_indexers = [idx for idx in resp.json() if isinstance(idx, dict)]
    except httpx.HTTPError:
        pass

    # Fetch the schema to discover built-in Cardigann definitions
    schema_defs: list[dict] = []
    try:
        resp = httpx.get(f"{prowlarr_url}/api/v1/indexer/schema", headers=headers, timeout=15)
        if resp.status_code == 200:
            schema_defs = resp.json()
    except httpx.HTTPError as e:
        logs.append(f"Prowlarr: could not fetch indexer schema: {e}")
        return logs

    changed = 0
    for indexer in existing_indexers:
        definition = _indexer_definition_name(indexer)
        if str(indexer.get("privacy", "")).lower() != "public" or definition in wanted_indexers:
            continue
        indexer_id = indexer.get("id")
        if not indexer_id:
            continue
        try:
            removed = httpx.delete(
                f"{prowlarr_url}/api/v1/indexer/{indexer_id}",
                headers=headers,
                timeout=15,
            )
            if removed.status_code in (200, 202, 204):
                changed += 1
                logs.append(f"Prowlarr: removed unconfigured public indexer '{indexer.get('name', definition)}'")
        except httpx.HTTPError as exc:
            logs.append(f"Prowlarr: could not remove '{indexer.get('name', definition)}': {exc}")

    flaresolverr_tag_id: int | None = None
    if flaresolverr_base:
        flaresolverr_tag_id = _prowlarr_flaresolverr_tag_and_proxy(prowlarr_url, headers, flaresolverr_base)
        if flaresolverr_tag_id is None:
            logs.append("Prowlarr: FlareSolverr proxy reconciliation failed")
    profile_id = _prowlarr_default_app_profile_id(prowlarr_url, headers)
    for schema in schema_defs:
        def_name = schema.get("definitionName", "").lower()
        if def_name not in wanted_indexers:
            continue
        display_name = schema.get("name", def_name)
        protected = def_name in _FLARESOLVERR_INDEXERS
        existing = next((idx for idx in existing_indexers if _indexer_definition_name(idx) == def_name), None)
        if existing is not None:
            if flaresolverr_tag_id is not None and protected:
                current_tags = [int(value) for value in existing.get("tags") or [] if str(value).isdigit()]
                if flaresolverr_tag_id not in current_tags:
                    payload = {k: v for k, v in existing.items() if k != "id"}
                    payload["tags"] = [*current_tags, flaresolverr_tag_id]
                    try:
                        repaired = httpx.put(
                            f"{prowlarr_url}/api/v1/indexer/{existing.get('id')}",
                            json=payload,
                            headers=headers,
                            timeout=15,
                        )
                        if repaired.status_code in (200, 202):
                            logs.append(f"Prowlarr: attached FlareSolverr to existing indexer '{display_name}'")
                    except httpx.HTTPError:
                        pass
            continue
        if protected and flaresolverr_base and flaresolverr_tag_id is None:
            logs.append(f"Prowlarr: skipped protected indexer '{display_name}' (FlareSolverr unavailable)")
            continue

        # Build payload from schema template; always include tags: []
        payload = {k: v for k, v in schema.items() if k not in ("id",)}
        payload["tags"] = []
        payload["appProfileId"] = profile_id

        if flaresolverr_tag_id is not None and protected:
            payload["tags"] = [flaresolverr_tag_id]

        try:
            resp = httpx.post(
                f"{prowlarr_url}/api/v1/indexer",
                json=payload,
                headers=headers,
                timeout=15,
            )
            if resp.status_code in (200, 201):
                changed += 1
                logs.append(f"Prowlarr: added indexer '{display_name}'")
            elif resp.status_code == 400 and "cloudfl" in (resp.text or "").lower():
                logs.append(
                    f"Prowlarr: skipped indexer '{display_name}' "
                    "(Cloudflare blocked on VPN egress — add via FlareSolverr later)"
                )
            else:
                logs.append(f"Prowlarr: skipped indexer '{display_name}' (HTTP {resp.status_code})")
        except httpx.HTTPError as e:
            logs.append(f"Prowlarr: could not add '{display_name}': {e}")

    if changed and trigger_prowlarr_indexer_sync(prowlarr_url, api_key):
        logs.append("Prowlarr: triggered indexer sync to Sonarr/Radarr")
    elif not logs:
        logs.append("Prowlarr: all wanted indexers already configured")
    return logs


def wire_bazarr_arr(
    bazarr_url: str,
    bazarr_api_key: str,
    sonarr_url: str,
    sonarr_api_key: str,
    radarr_url: str,
    radarr_api_key: str,
) -> list[str]:
    """Configure Bazarr Sonarr/Radarr via /api/system/settings (Bazarr 1.x). Idempotent."""
    logs: list[str] = []
    headers = {"X-API-KEY": bazarr_api_key}
    settings_url = f"{bazarr_url}/api/system/settings"

    from urllib.parse import urlparse

    sonarr_host = urlparse(sonarr_url).hostname or "sonarr"
    sonarr_port = urlparse(sonarr_url).port or 8989
    radarr_host = urlparse(radarr_url).hostname or "radarr"
    radarr_port = urlparse(radarr_url).port or 7878

    def _sonarr_configured(current: dict) -> bool:
        return bool(current.get("sonarr", {}).get("ip")) and bool(current.get("sonarr", {}).get("apikey"))

    def _radarr_configured(current: dict) -> bool:
        return bool(current.get("radarr", {}).get("ip")) and bool(current.get("radarr", {}).get("apikey"))

    try:
        cur = httpx.get(settings_url, headers=headers, timeout=10)
        cur.raise_for_status()
        existing = cur.json() if cur.content else {}
    except httpx.HTTPError as exc:
        logs.append(f"Bazarr: could not read settings ({exc})")
        return logs

    form: dict[str, str] = {}
    general = existing.get("general", {}) if isinstance(existing, dict) else {}
    if not general.get("languages") and not general.get("language"):
        form["settings-general-languages"] = "eng"

    if not _sonarr_configured(existing):
        form.update(
            {
                "settings-general-use_sonarr": "true",
                "settings-sonarr-ip": sonarr_host,
                "settings-sonarr-port": str(sonarr_port),
                "settings-sonarr-apikey": sonarr_api_key,
                "settings-sonarr-ssl": "false",
            }
        )
    else:
        logs.append("Bazarr: Sonarr already configured — skipping")

    if not _radarr_configured(existing):
        form.update(
            {
                "settings-general-use_radarr": "true",
                "settings-radarr-ip": radarr_host,
                "settings-radarr-port": str(radarr_port),
                "settings-radarr-apikey": radarr_api_key,
                "settings-radarr-ssl": "false",
            }
        )
    else:
        logs.append("Bazarr: Radarr already configured — skipping")

    if not form:
        return logs

    try:
        resp = httpx.post(settings_url, headers=headers, data=form, timeout=20)
        if resp.status_code in (200, 201, 204):
            if "settings-general-languages" in form:
                logs.append("Bazarr: default languages set (eng)")
            if "settings-general-use_sonarr" in form:
                logs.append("Bazarr: configured Sonarr")
            if "settings-general-use_radarr" in form:
                logs.append("Bazarr: configured Radarr")
        else:
            logs.append(f"Bazarr: settings update HTTP {resp.status_code}")
    except httpx.HTTPError as exc:
        logs.append(f"Bazarr: could not update settings ({exc})")
    return logs


def wire_bazarr_providers(
    bazarr_url: str,
    bazarr_api_key: str,
    *,
    opensubtitles_user: str = "",
    opensubtitles_password: str = "",
    flaresolverr_url: str = "",
) -> list[str]:
    """Configure Bazarr subtitle providers via /api/system/settings.

    Enables the built-in embedded provider and Bazarr's separate Subsync
    feature. When OpenSubtitles credentials are supplied, also enables that
    provider with the given username/password. All changes are idempotent.

    Requires ``OPENSUBTITLES_USER`` and ``OPENSUBTITLES_PASSWORD`` in secrets
    (set via `secrets set` or the WebUI). Without them, only credential-free
    providers are enabled.
    """
    logs: list[str] = []
    headers = {"X-API-KEY": bazarr_api_key}
    settings_url = f"{bazarr_url}/api/system/settings"

    try:
        cur = httpx.get(settings_url, headers=headers, timeout=10)
        cur.raise_for_status()
        existing = cur.json() if cur.content else {}
    except httpx.HTTPError as exc:
        logs.append(f"Bazarr: could not read provider settings ({exc})")
        return logs

    # Subsync is a feature, not a provider in current Bazarr releases.
    desired_providers: list[str] = ["embeddedsubtitles"]
    if opensubtitles_user and opensubtitles_password:
        desired_providers.append("opensubtitles")

    current_providers = existing.get("general", {}).get("enabled_providers", "")
    if isinstance(current_providers, list):
        # Older wiring stored a single "a[]b" string — treat it as misconfigured.
        current_set = {p for item in current_providers for p in str(item).split("[]") if p}
    else:
        current_set = {p.strip() for p in str(current_providers).split("[]") if p.strip()}

    # Bazarr's settings POST expects list values as repeated form fields.
    form: dict[str, str | list[str]] = {}
    if set(desired_providers) != current_set:
        form["settings-general-enabled_providers"] = desired_providers
    subsync = existing.get("subsync")
    if not isinstance(subsync, dict) or subsync.get("use_subsync") is not True:
        form["settings-subsync-use_subsync"] = "true"

    if opensubtitles_user and opensubtitles_password:
        os_section = existing.get("opensubtitles", {})
        if os_section.get("username") != opensubtitles_user:
            form["settings-opensubtitles-username"] = opensubtitles_user
            form["settings-opensubtitles-password"] = opensubtitles_password
            form["settings-opensubtitles-use_hash_search"] = "True"

    if flaresolverr_url:
        # Bazarr installations do not all expose the opensubtitlesorg
        # settings section (and therefore reject its form fields).  Never
        # fabricate that section or migrate an installation just to wire an
        # optional FlareSolverr value.
        os_org = existing.get("opensubtitlesorg")
        if isinstance(os_org, dict) and os_org.get("hash") != flaresolverr_url:
            form["settings-opensubtitlesorg-hash"] = flaresolverr_url

    if not form:
        logs.append(f"Bazarr: providers and Subsync already configured ({', '.join(desired_providers)})")
        return logs

    try:
        resp = httpx.post(settings_url, headers=headers, data=form, timeout=20)
        if resp.status_code in (200, 201, 204):
            configured = [p for p in desired_providers if p != "embeddedsubtitles"]
            if "opensubtitles" in configured and opensubtitles_user:
                logs.append("Bazarr: OpenSubtitles provider configured with credentials")
            else:
                logs.append("Bazarr: embedded provider and Subsync enabled")
        else:
            logs.append(f"Bazarr: failed provider update (HTTP {resp.status_code})")
    except httpx.HTTPError as exc:
        logs.append(f"Bazarr: could not update providers ({exc})")
    return logs


def wire_seerr_arr(
    seerr_url: str,
    seerr_api_key: str,
    sonarr_url: str,
    sonarr_api_key: str,
    radarr_url: str,
    radarr_api_key: str,
    *,
    jellyfin_url: str = "",
    jellyfin_api_key: str = "",
    jellyfin_user: str = "",
    jellyfin_password: str = "",
    plex_token: str = "",
) -> list[str]:
    """Headlessly bootstrap Seerr: create the admin from Jellyfin, link the media
    server, register Sonarr/Radarr, and finalize setup so no web wizard is needed.

    Seerr (ghcr.io/seerr-team/seerr) is the maintained successor to Overseerr/
    Jellyseerr and supports Jellyfin, Emby and Plex from one service; its API is the
    Overseerr v1 lineage (``/api/v1/settings/*``).
    """
    from urllib.parse import urlparse

    logs: list[str] = []
    headers = {"X-Api-Key": seerr_api_key, "Content-Type": "application/json"}

    def _public() -> dict:
        try:
            resp = httpx.get(f"{seerr_url}/api/v1/settings/public", timeout=10)
            if resp.status_code == 200:
                return resp.json() or {}
        except httpx.HTTPError:
            pass
        return {}

    # 1. Create the first admin account from Jellyfin credentials (headless wizard).
    initialized = bool(_public().get("initialized"))
    jf = urlparse(jellyfin_url) if jellyfin_url else None
    jf_hostname = (jf.hostname if jf and jf.hostname else "jellyfin") if jellyfin_url else ""
    jf_port = (jf.port if jf and jf.port else 8096) if jellyfin_url else 8096
    if initialized:
        logs.append("Seerr: already initialized")
    elif not initialized and jf_hostname and jellyfin_user and jellyfin_password:
        final_status = 0
        final_error = ""
        for attempt in range(7):
            try:
                resp = httpx.post(
                    f"{seerr_url}/api/v1/auth/jellyfin",
                    json={
                        "username": jellyfin_user,
                        "password": jellyfin_password,
                        "hostname": jf_hostname,
                        "port": jf_port,
                        "urlBase": "",
                        "useSsl": False,
                        "email": jellyfin_user,
                        "serverType": 2,  # MediaServerType.JELLYFIN
                    },
                    timeout=20,
                )
                final_status = resp.status_code
                if resp.status_code in (200, 201):
                    logs.append("Seerr: admin account created from Jellyfin")
                    initialized = True
                    break
                if resp.status_code < 500:
                    break
            except httpx.HTTPError as error:
                final_error = str(error)
            if attempt < 6:
                time.sleep(5)
        if not initialized:
            detail = f"HTTP {final_status}" if final_status else final_error or "media server unavailable"
            logs.append(f"Seerr: Jellyfin admin bootstrap failed ({detail})")

    if not initialized:
        logs.append("Seerr: setup deferred until media server authentication succeeds")
        return logs

    # 2. Link the Jellyfin media server.
    if jf_hostname and jellyfin_api_key and initialized:
        current_jellyfin: dict = {}
        try:
            current = httpx.get(f"{seerr_url}/api/v1/settings/jellyfin", headers=headers, timeout=10)
            if current.status_code == 200 and isinstance(current.json(), dict):
                current_jellyfin = current.json()
        except (httpx.HTTPError, ValueError):
            pass
        current_host = current_jellyfin.get("hostname") or current_jellyfin.get("ip")
        if current_host == jf_hostname and (
            current_jellyfin.get("serverID") or current_jellyfin.get("serverId") or current_jellyfin.get("id")
        ):
            logs.append("Seerr: Jellyfin server already linked")
        else:
            try:
                resp = httpx.post(
                    f"{seerr_url}/api/v1/settings/jellyfin",
                    json={
                        "hostname": jf_hostname,
                        "port": jf_port,
                        "useSsl": False,
                        "urlBase": "",
                        "apiKey": jellyfin_api_key,
                    },
                    headers=headers,
                    timeout=20,
                )
                if resp.status_code in (200, 201):
                    logs.append("Seerr: Jellyfin server linked")
                else:
                    logs.append(f"Seerr: failed Jellyfin link (HTTP {resp.status_code})")
            except httpx.HTTPError as e:
                logs.append(f"Seerr: failed Jellyfin link: {e}")

    def _exists(endpoint: str, name: str) -> bool:
        try:
            resp = httpx.get(f"{seerr_url}/api/v1/settings/{endpoint}", headers=headers, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                return isinstance(data, list) and any(
                    isinstance(item, dict) and item.get("name") == name for item in data
                )
        except (httpx.HTTPError, ValueError):
            pass
        return False

    def _wire_arr(endpoint: str, name: str, hostname: str, port: int, api_key: str, root_dir: str) -> None:
        if _exists(endpoint, name):
            logs.append(f"Seerr: {name} already configured — skipping")
            return
        test_body = {
            "name": name,
            "hostname": hostname,
            "port": port,
            "apiKey": api_key,
            "useSsl": False,
            "baseUrl": "",
        }
        profile_id = 1
        profile_name = "Any"
        try:
            test = httpx.post(
                f"{seerr_url}/api/v1/settings/{endpoint}/test",
                json=test_body,
                headers=headers,
                timeout=15,
            )
            if test.status_code == 200:
                profiles = test.json().get("profiles") or []
                if profiles:
                    profile_id = profiles[0].get("id", 1)
                    profile_name = profiles[0].get("name", profile_name)
        except httpx.HTTPError:
            pass
        payload = {
            **test_body,
            "activeProfileId": profile_id,
            "activeProfileName": profile_name,
            "activeDirectory": root_dir,
            "is4k": False,
            "isDefault": True,
            "syncEnabled": True,
            "enableSeasonFolders": True,
        }
        if endpoint == "sonarr":
            payload["activeLanguageProfileId"] = 1
        if endpoint == "radarr":
            payload["minimumAvailability"] = "released"
        try:
            resp = httpx.post(
                f"{seerr_url}/api/v1/settings/{endpoint}",
                json=payload,
                headers=headers,
                timeout=15,
            )
            if resp.status_code in (200, 201):
                logs.append(f"Seerr: configured {name}")
            elif resp.status_code == 400:
                logs.append(f"Seerr: {name} already configured")
            else:
                logs.append(f"Seerr: skipped {name} configure (HTTP {resp.status_code})")
        except httpx.HTTPError as e:
            logs.append(f"Seerr: could not configure {name}: {e}")

    sonarr_host = urlparse(sonarr_url).hostname or "sonarr"
    sonarr_port = urlparse(sonarr_url).port or 8989
    radarr_host = urlparse(radarr_url).hostname or "radarr"
    radarr_port = urlparse(radarr_url).port or 7878

    _wire_arr("sonarr", "Sonarr", sonarr_host, sonarr_port, sonarr_api_key, "/data/tv")
    _wire_arr("radarr", "Radarr", radarr_host, radarr_port, radarr_api_key, "/data/movies")

    # 3. Optionally link Plex too (Seerr supports Plex + Jellyfin simultaneously).
    if plex_token:
        try:
            resp = httpx.post(
                f"{seerr_url}/api/v1/settings/plex",
                json={"token": plex_token},
                headers=headers,
                timeout=15,
            )
            if resp.status_code in (200, 201):
                logs.append("Seerr: Plex linked")
            else:
                logs.append(f"Seerr: failed Plex link (HTTP {resp.status_code})")
        except httpx.HTTPError as e:
            logs.append(f"Seerr: failed Plex link: {e}")

    # 4. Finalize setup so the web wizard never blocks first use.
    try:
        resp = httpx.post(
            f"{seerr_url}/api/v1/settings/initialize",
            headers=headers,
            timeout=15,
        )
        if resp.status_code in (200, 201):
            logs.append("Seerr: setup finalized (initialized)")
    except httpx.HTTPError as e:
        logs.append(f"Seerr: initialize error: {e}")

    return logs


# ── Verify helpers (Servarr / Bazarr / Seerr) ────────────────────

_ARR_HEALTH_ERROR_SEVERITIES = frozenset({"error", "warning"})


def servarr_get(cfg, base_url, container, vm_ip, root, headers):
    """Build a per-request GET adapter for a Servarr HTTP API."""
    import httpx

    from toolkit.services.sdk import docker_curl

    def _get(path):
        if cfg.is_multi_node:
            from urllib.parse import urlparse

            parsed = urlparse(base_url)
            port = f":{parsed.port}" if parsed.port and parsed.port not in (80, 443) else ""
            rc, body = docker_curl(cfg, vm_ip, container, f"http://localhost{port}{path}", root=root, headers=headers)
            if rc == 0 and body:

                class _Resp:
                    status_code = 200

                    def json(self):
                        return _json.loads(body)

                return _Resp()
            return None
        return httpx.get(f"{base_url}{path}", headers=headers, timeout=15)

    return _get


def _health_issue_labels(entries: list) -> list[str]:
    issues: list[str] = []
    for item in entries:
        if not isinstance(item, dict):
            continue
        severity = str(item.get("severity", "")).lower()
        if severity not in _ARR_HEALTH_ERROR_SEVERITIES:
            continue
        check_type = str(item.get("type", item.get("source", "HealthCheck")))
        message = str(item.get("message", "")).strip()
        label = f"{check_type}" + (f": {message[:60]}" if message else "")
        issues.append(label)
    return issues


def verify_servarr_health(
    service: str,
    cfg,
    base_url: str,
    container: str,
    vm_ip: str,
    root: Path,
    api_key: str,
    *,
    api_version: str = "v3",
):
    """``GET /api/{version}/health`` — fail when Servarr reports error/warning health issues."""
    from toolkit.services.sdk import VerifyCheck

    headers = {"X-Api-Key": api_key}
    _get = servarr_get(cfg, base_url, container, vm_ip, root, headers)
    resp = _get(f"/api/{api_version}/health")
    if not resp or resp.status_code != 200:
        return VerifyCheck(service, "health", False, "health API unreachable")
    try:
        payload = resp.json()
    except (_json.JSONDecodeError, TypeError, ValueError):
        return VerifyCheck(service, "health", False, "invalid health JSON")
    entries = payload if isinstance(payload, list) else []
    issues = _health_issue_labels(entries)
    if issues:
        return VerifyCheck(service, "health", False, "; ".join(issues[:4]))
    return VerifyCheck(service, "health", True, "no health issues")


def verify_arr_resources(service: str, cfg, base_url: str, container: str, vm_ip: str, root: Path, api_key: str):
    """Root folders present and qBittorrent download client configured."""
    from toolkit.services.sdk import VerifyCheck

    headers = {"X-Api-Key": api_key}
    _get = servarr_get(cfg, base_url, container, vm_ip, root, headers)
    checks: list = []
    resp = _get("/api/v3/rootFolder")
    if resp and resp.status_code == 200:
        folders = resp.json()
        count = len(folders) if isinstance(folders, list) else 0
        checks.append(VerifyCheck(service, "root_folders", count > 0, f"{count} configured"))
    else:
        checks.append(VerifyCheck(service, "root_folders", False, "API unreachable"))
    resp = _get("/api/v3/downloadclient")
    if resp and resp.status_code == 200:
        clients = resp.json()
        qbit = [c for c in (clients or []) if isinstance(c, dict) and c.get("implementation") == "QBittorrent"]
        status = "connected" if qbit else "missing"
        checks.append(VerifyCheck(service, "download_client", bool(qbit), f"qBittorrent {status}"))
    else:
        checks.append(VerifyCheck(service, "download_client", False, "API unreachable"))
    return checks


def verify_arr_prowlarr_indexers(
    service: str, cfg, base_url: str, container: str, vm_ip: str, root: Path, api_key: str
):
    """At least one enabled indexer synced from Prowlarr."""
    from toolkit.services.sdk import VerifyCheck

    headers = {"X-Api-Key": api_key}
    _get = servarr_get(cfg, base_url, container, vm_ip, root, headers)
    resp = _get("/api/v3/indexer")
    if not resp or resp.status_code != 200:
        return VerifyCheck(service, "indexers", False, "indexer API unreachable")
    indexers = resp.json() if isinstance(resp.json(), list) else []

    def _field(indexer: dict, name: str) -> str:
        for fld in indexer.get("fields") or []:
            if isinstance(fld, dict) and fld.get("name") == name:
                return str(fld.get("value") or "")
        return ""

    def _from_prowlarr(indexer: dict) -> bool:
        enabled = indexer.get("enable")
        if enabled is None:
            enabled = any(
                indexer.get(flag) is True for flag in ("enableRss", "enableAutomaticSearch", "enableInteractiveSearch")
            )
        if not enabled:
            return False
        base_url_val = _field(indexer, "baseUrl").lower()
        if "prowlarr" in base_url_val or ":9696" in base_url_val:
            return True
        return "prowlarr" in str(indexer.get("name", "")).lower()

    synced = [idx for idx in indexers if isinstance(idx, dict) and _from_prowlarr(idx)]

    def _enabled(indexer: dict) -> bool:
        if indexer.get("enable") is not None:
            return bool(indexer.get("enable"))
        return any(
            indexer.get(flag) is True for flag in ("enableRss", "enableAutomaticSearch", "enableInteractiveSearch")
        )

    enabled = [idx for idx in indexers if isinstance(idx, dict) and _enabled(idx)]
    ok = len(synced) > 0
    detail = (
        f"{len(synced)} Prowlarr-synced / {len(enabled)} enabled"
        if ok
        else f"{len(enabled)} enabled, none from Prowlarr"
    )
    return VerifyCheck(service, "indexers", ok, detail)


def verify_arr_downloadclient_test(cfg, service: str, container: str, port: int, vm_ip: str, root: Path, api_key: str):
    """POST ``/api/v3/downloadclient/test`` for the configured qBittorrent client."""
    import httpx

    from toolkit.services.sdk import VerifyCheck, docker_curl

    if not api_key:
        return VerifyCheck(service, "download_client_test", False, "API key missing")
    headers = {"X-Api-Key": api_key}
    rc, out = docker_curl(
        cfg,
        vm_ip,
        container,
        f"http://127.0.0.1:{port}/api/v3/downloadclient",
        root=root,
        headers=headers,
        timeout=20,
    )
    if rc != 0 or not out:
        return VerifyCheck(service, "download_client_test", False, "downloadclient API unreachable")
    try:
        clients = _json.loads(out)
    except _json.JSONDecodeError:
        return VerifyCheck(service, "download_client_test", False, "invalid downloadclient JSON")
    qbit = [c for c in clients if isinstance(c, dict) and c.get("implementation") == "QBittorrent"]
    if not qbit:
        return VerifyCheck(service, "download_client_test", False, "qBittorrent client missing")
    body = _json.dumps(qbit[0])
    trc, _tout = docker_curl(
        cfg,
        vm_ip,
        container,
        f"http://127.0.0.1:{port}/api/v3/downloadclient/test",
        root=root,
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
        body=body,
        timeout=25,
    )
    if trc == 0:
        return VerifyCheck(service, "download_client_test", True, "test passed")
    if not cfg.is_multi_node:
        try:
            resp = httpx.post(
                f"http://localhost:{port}/api/v3/downloadclient/test",
                json=qbit[0],
                headers={"X-Api-Key": api_key},
                timeout=15,
            )
            if resp.status_code in (200, 201):
                return VerifyCheck(service, "download_client_test", True, "test passed")
            return VerifyCheck(service, "download_client_test", False, f"HTTP {resp.status_code}")
        except httpx.HTTPError as exc:
            return VerifyCheck(service, "download_client_test", False, str(exc)[:80])
    return VerifyCheck(service, "download_client_test", False, "downloadclient/test failed")


def verify_arr_standard(
    service: str, cfg, base_url: str, container: str, port: int, vm_ip: str, root: Path, api_key: str
):
    """Sonarr/Radarr: health, resources, Prowlarr indexers, download-client test."""
    checks = [verify_servarr_health(service, cfg, base_url, container, vm_ip, root, api_key)]
    checks.extend(verify_arr_resources(service, cfg, base_url, container, vm_ip, root, api_key))
    checks.append(verify_arr_prowlarr_indexers(service, cfg, base_url, container, vm_ip, root, api_key))
    checks.append(verify_arr_downloadclient_test(cfg, service, container, port, vm_ip, root, api_key))
    return checks


def _indexer_definition_name(indexer: dict) -> str:
    for key in ("definitionName", "implementationName", "indexerName", "name"):
        val = indexer.get(key)
        if val:
            return str(val).lower()
    for fld in indexer.get("fields") or []:
        if isinstance(fld, dict) and fld.get("name") == "definitionName":
            return str(fld.get("value") or "").lower()
    return ""


def verify_prowlarr_flaresolverr(cfg, base_url: str, container: str, vm_ip: str, root: Path, api_key: str):
    """Confirm protected indexers share a tag with an enabled FlareSolverr proxy."""
    from toolkit.services.sdk import VerifyCheck

    headers = {"X-Api-Key": api_key}
    _get = servarr_get(cfg, base_url, container, vm_ip, root, headers)
    resp = _get("/api/v1/indexer")
    if not resp or resp.status_code != 200:
        return VerifyCheck("prowlarr", "flaresolverr", False, "indexer API unreachable")
    indexers = resp.json() if isinstance(resp.json(), list) else []
    cf_indexers = [
        idx for idx in indexers if isinstance(idx, dict) and _indexer_definition_name(idx) in _FLARESOLVERR_INDEXERS
    ]
    flaresolverr_enabled: bool | None = None
    try:
        from toolkit.core.manifest.catalog import load_service_catalog
        from toolkit.core.manifest.routes import service_is_enabled

        flaresolverr_enabled = service_is_enabled(cfg, load_service_catalog().require("flaresolverr"))
        if not flaresolverr_enabled:
            return VerifyCheck("prowlarr", "flaresolverr", True, "FlareSolverr disabled (skipped)")
    except (ImportError, KeyError, OSError, TypeError, ValueError):
        # Verification remains useful for minimal test/config objects that do
        # not expose the service catalog; in that case infer from live state.
        pass
    proxy_response = _get("/api/v1/indexerproxy")
    tag_response = _get("/api/v1/tag")
    proxies = proxy_response.json() if proxy_response and proxy_response.status_code == 200 else []
    tags = tag_response.json() if tag_response and tag_response.status_code == 200 else []
    flare_proxy = next(
        (
            item
            for item in proxies
            if isinstance(item, dict)
            and str(item.get("implementation", item.get("name", ""))).lower() == "flaresolverr"
        ),
        None,
    )
    if flare_proxy is None:
        return VerifyCheck("prowlarr", "flaresolverr", False, "FlareSolverr proxy missing")
    proxy_tags = {int(value) for value in flare_proxy.get("tags") or [] if str(value).isdigit()}
    flare_tag_ids = {
        int(item["id"])
        for item in tags
        if isinstance(item, dict)
        and str(item.get("label", "")).lower() == "flaresolverr"
        and str(item.get("id", "")).isdigit()
    }
    shared_tags = proxy_tags & flare_tag_ids
    if not shared_tags:
        return VerifyCheck("prowlarr", "flaresolverr", False, "FlareSolverr proxy tag missing")
    if not cf_indexers:
        return VerifyCheck("prowlarr", "flaresolverr", True, "proxy ready; no protected indexer active")
    wired = [
        idx
        for idx in cf_indexers
        if shared_tags & {int(value) for value in idx.get("tags") or [] if str(value).isdigit()}
    ]
    ok = len(wired) == len(cf_indexers) and bool(shared_tags)
    detail = (
        f"{len(wired)}/{len(cf_indexers)} CF indexer(s) share FlareSolverr tag"
        if ok
        else "CF indexers missing FlareSolverr tag"
    )
    return VerifyCheck("prowlarr", "flaresolverr", ok, detail)


def verify_prowlarr_indexers(cfg, base_url: str, container: str, vm_ip: str, root: Path, api_key: str):
    from toolkit.services.sdk import VerifyCheck

    headers = {"X-Api-Key": api_key}
    _get = servarr_get(cfg, base_url, container, vm_ip, root, headers)
    checks: list = []
    resp = _get("/api/v1/indexer")
    if resp and resp.status_code == 200:
        indexers = resp.json() if isinstance(resp.json(), list) else []
        enabled = [i for i in indexers if isinstance(i, dict) and i.get("enable")]
        checks.append(
            VerifyCheck(
                "prowlarr",
                "indexers",
                len(indexers) > 0 and len(enabled) > 0,
                f"{len(enabled)} enabled / {len(indexers)} total",
            )
        )
    else:
        checks.append(VerifyCheck("prowlarr", "indexers", False, "API unreachable"))

    checks.append(verify_prowlarr_flaresolverr(cfg, base_url, container, vm_ip, root, api_key))
    return checks


def verify_prowlarr_applications(cfg, base_url: str, container: str, vm_ip: str, root: Path, api_key: str):
    from toolkit.services.sdk import VerifyCheck

    headers = {"X-Api-Key": api_key}
    _get = servarr_get(cfg, base_url, container, vm_ip, root, headers)
    resp = _get("/api/v1/applications")
    if not resp or resp.status_code != 200:
        return VerifyCheck("prowlarr", "applications", False, "API unreachable")
    apps = resp.json() if isinstance(resp.json(), list) else []
    by_name = {str(a.get("name", "")): a for a in apps if isinstance(a, dict)}
    missing = [name for name in ("Sonarr", "Radarr") if name not in by_name]
    if missing:
        return VerifyCheck("prowlarr", "applications", False, f"missing: {', '.join(missing)}")
    bad_sync = [
        name
        for name in ("Sonarr", "Radarr")
        if str(by_name[name].get("syncLevel", "")).lower() not in ("fullsync", "full")
    ]
    if bad_sync:
        return VerifyCheck("prowlarr", "applications", False, f"sync not full: {', '.join(bad_sync)}")
    return VerifyCheck("prowlarr", "applications", True, f"{len(apps)} connected (Sonarr+Radarr fullSync)")


def verify_prowlarr_standard(cfg, base_url: str, container: str, vm_ip: str, root: Path, api_key: str):
    checks = [
        verify_servarr_health("prowlarr", cfg, base_url, container, vm_ip, root, api_key, api_version="v1"),
    ]
    checks.extend(verify_prowlarr_indexers(cfg, base_url, container, vm_ip, root, api_key))
    checks.append(verify_prowlarr_applications(cfg, base_url, container, vm_ip, root, api_key))
    return checks


def _bazarr_settings(cfg, vm_ip: str, root: Path, api_key: str) -> dict | None:
    import httpx

    from toolkit.services.sdk import docker_curl

    headers = {"X-API-KEY": api_key} if api_key else {}
    if cfg.is_multi_node:
        rc, body = docker_curl(
            cfg, vm_ip, "bazarr", "http://localhost:6767/api/system/settings", root=root, headers=headers
        )
        if rc != 0 or not body:
            return None
        try:
            return _json.loads(body)
        except _json.JSONDecodeError:
            return None
    try:
        resp = httpx.get("http://localhost:6767/api/system/settings", headers=headers, timeout=10)
        if resp.status_code != 200:
            return None
        return resp.json()
    except httpx.HTTPError:
        return None


def resolve_bazarr_api_key(cfg, secrets: dict[str, str], root: Path) -> str:
    from toolkit.services import get_service_plugin

    plugin = get_service_plugin("bazarr")
    state_path = (
        plugin.manifest.host_sources["BAZARR_CONFIG_SOURCE"].path if plugin is not None else "data/bazarr/config"
    )
    if cfg.is_multi_node:
        from toolkit.core.config.storage import DEFAULT_HOMELAB_ROOT
        from toolkit.core.manifest.placement import service_address
        from toolkit.services.sdk import ssh_on_vm

        config_path = Path(DEFAULT_HOMELAB_ROOT) / state_path / "config" / "config.yaml"
        rc, out, _ = ssh_on_vm(
            cfg,
            service_address(cfg, "bazarr"),
            f"sed -n 's/^[[:space:]]*apikey:[[:space:]]*//p' {config_path} 2>/dev/null | head -1",
            root=root,
            timeout=20,
        )
        if rc == 0 and (out or "").strip():
            return (out or "").strip().splitlines()[-1]
        return ""
    return extract_bazarr_api_key(root / state_path / "config" / "config.yaml") or ""


def verify_bazarr_health(cfg, vm_ip: str, root: Path, api_key: str):
    import httpx

    from toolkit.services.sdk import VerifyCheck, docker_curl

    headers = {"X-API-KEY": api_key} if api_key else {}
    if cfg.is_multi_node:
        rc, body = docker_curl(
            cfg, vm_ip, "bazarr", "http://localhost:6767/api/system/status", root=root, headers=headers
        )
        if rc != 0 or not body:
            return VerifyCheck("bazarr", "health", False, "status API unreachable")
        return VerifyCheck("bazarr", "health", True, "status ok")
    try:
        resp = httpx.get("http://localhost:6767/api/system/status", headers=headers, timeout=10)
        if resp.status_code != 200:
            return VerifyCheck("bazarr", "health", False, f"HTTP {resp.status_code}")
        return VerifyCheck("bazarr", "health", True, "status ok")
    except httpx.HTTPError:
        return VerifyCheck("bazarr", "health", False, "API unreachable")


def verify_bazarr_languages(settings: dict | None):
    from toolkit.services.sdk import VerifyCheck

    if not settings:
        return VerifyCheck("bazarr", "languages", False, "settings API unreachable")
    general = settings.get("general", {}) if isinstance(settings, dict) else {}
    languages = general.get("languages", general.get("language", ""))
    has_languages = bool(languages)
    lang_detail = str(languages)[:80] if has_languages else "none configured"
    return VerifyCheck("bazarr", "languages", has_languages, f"languages={lang_detail}")


def verify_bazarr_providers(settings: dict | None):
    from toolkit.services.sdk import VerifyCheck

    if not settings:
        return VerifyCheck("bazarr", "providers", False, "settings API unreachable")
    general = settings.get("general", {}) if isinstance(settings, dict) else {}
    providers = general.get("enabled_providers", "")
    if isinstance(providers, list):
        names = [str(p) for p in providers if p]
    else:
        names = [p.strip() for p in str(providers).split("[]") if p.strip()]
    desired = {"embeddedsubtitles"}
    missing = sorted(desired - set(names))
    subsync = settings.get("subsync")
    subsync_enabled = isinstance(subsync, dict) and subsync.get("use_subsync") is True
    ok = not missing and subsync_enabled
    detail = ", ".join(names) if names else "none configured"
    if missing:
        detail += f"; missing desired: {', '.join(missing)}"
    if not subsync_enabled:
        detail += "; Subsync disabled"
    return VerifyCheck("bazarr", "providers", ok, detail)


def verify_bazarr_arr_links(settings: dict | None):
    from toolkit.services.sdk import VerifyCheck

    if not settings:
        return VerifyCheck("bazarr", "arr_links", False, "settings API unreachable")
    sonarr_ok = bool(settings.get("sonarr", {}).get("ip")) and bool(settings.get("sonarr", {}).get("apikey"))
    radarr_ok = bool(settings.get("radarr", {}).get("ip")) and bool(settings.get("radarr", {}).get("apikey"))
    ok = sonarr_ok and radarr_ok
    parts = [name for name, flag in (("Sonarr", sonarr_ok), ("Radarr", radarr_ok)) if flag]
    return VerifyCheck("bazarr", "arr_links", ok, " + ".join(parts) if parts else "Sonarr/Radarr not configured")


def verify_bazarr_standard(cfg, secrets: dict[str, str], vm_ip: str, root: Path):
    from toolkit.services.sdk import VerifyCheck

    api_key = resolve_bazarr_api_key(cfg, secrets, root)
    if not api_key:
        return [VerifyCheck("bazarr", "api", True, "skipped (no BAZARR_API_KEY)")]
    settings = _bazarr_settings(cfg, vm_ip, root, api_key)
    return [
        verify_bazarr_health(cfg, vm_ip, root, api_key),
        verify_bazarr_languages(settings),
        verify_bazarr_providers(settings),
        verify_bazarr_arr_links(settings),
    ]


def resolve_seerr_api_key(secrets: dict[str, str], root: Path) -> str:
    from toolkit.services.seerr.bootstrap import extract_seerr_api_key

    disk = extract_seerr_api_key(root) if root else None
    if disk:
        return disk
    return secrets.get("SEERR_API_KEY", "")


def verify_seerr_status(cfg, vm_ip: str, root: Path):
    import httpx

    from toolkit.services.sdk import VerifyCheck, container_exists_on_vm, docker_curl

    if cfg.domain == "localhost":
        return VerifyCheck("seerr", "status", True, "skipped (localhost)")
    if not container_exists_on_vm(cfg, vm_ip, "seerr", root):
        return VerifyCheck("seerr", "status", True, "skipped (container missing)")

    url = "http://localhost:5055/api/v1/status"
    if cfg.is_multi_node:
        rc, body = docker_curl(cfg, vm_ip, "seerr", url, root=root, timeout=10)
        if rc != 0 or not body:
            return VerifyCheck("seerr", "status", False, "status API unreachable")
        try:
            data = _json.loads(body)
        except _json.JSONDecodeError:
            return VerifyCheck("seerr", "status", False, "invalid JSON")
    else:
        try:
            resp = httpx.get("http://localhost:5055/api/v1/status", timeout=8)
            if resp.status_code != 200:
                return VerifyCheck("seerr", "status", False, f"HTTP {resp.status_code}")
            data = resp.json()
        except httpx.HTTPError as exc:
            return VerifyCheck("seerr", "status", False, str(exc)[:80])
    version = data.get("version") if isinstance(data, dict) else None
    ok = bool(version)
    return VerifyCheck("seerr", "status", ok, f"version {version}" if ok else "no version in response")


def verify_seerr_connections(cfg, secrets: dict[str, str], vm_ip: str, root: Path):
    import httpx

    from toolkit.services.sdk import VerifyCheck, docker_curl

    api_key = resolve_seerr_api_key(secrets, root)
    if cfg.is_multi_node:
        # Seerr owns its API key in its runtime settings file.  The controller
        # bundle is intentionally immutable on guests, so prefer the live
        # service value when verifying from the controller.
        from toolkit.services.sdk import docker_exec_on_vm

        rc, settings = docker_exec_on_vm(
            cfg,
            "seerr",
            ["cat", "/app/config/settings.json"],
            vm_ip,
            root,
            timeout=15,
        )
        if rc == 0 and settings:
            try:
                runtime_key = (_json.loads(settings).get("main") or {}).get("apiKey")
            except (_json.JSONDecodeError, TypeError):
                runtime_key = None
            if runtime_key:
                api_key = str(runtime_key)
    if not api_key:
        return VerifyCheck("seerr", "connections", True, "skipped (no SEERR_API_KEY)")
    headers = {"X-Api-Key": api_key}

    def _fetch(path: str) -> tuple[int, dict | list | None]:
        url = f"http://localhost:5055{path}"
        if cfg.is_multi_node:
            rc, body = docker_curl(cfg, vm_ip, "seerr", url, root=root, headers=headers, timeout=12)
            if rc != 0 or not body:
                return 0, None
            try:
                return 200, _json.loads(body)
            except _json.JSONDecodeError:
                return 0, None
        try:
            resp = httpx.get(url, headers=headers, timeout=12)
            if resp.status_code != 200:
                return resp.status_code, None
            return 200, resp.json()
        except httpx.HTTPError:
            return 0, None

    status, public = _fetch("/api/v1/settings/public")
    if status != 200 or not isinstance(public, dict):
        return VerifyCheck("seerr", "connections", False, "settings API unreachable")
    if not public.get("initialized"):
        return VerifyCheck("seerr", "connections", False, "not initialized — re-run deploy hooks on media")

    missing: list[str] = []
    from toolkit.core.manifest.settings import service_setting_str

    if service_setting_str(cfg, "media-library", "server") in ("jellyfin", "both"):
        _, jellyfin = _fetch("/api/v1/settings/jellyfin")
        if not isinstance(jellyfin, dict) or not (
            (jellyfin.get("hostname") or jellyfin.get("ip"))
            and (jellyfin.get("serverID") or jellyfin.get("serverId") or jellyfin.get("id"))
        ):
            missing.append("Jellyfin")
    _, sonarr = _fetch("/api/v1/settings/sonarr")
    if not isinstance(sonarr, list) or not any(
        isinstance(item, dict) and (item.get("hostname") or item.get("ip")) and (item.get("apiKey") or item.get("id"))
        for item in sonarr
    ):
        missing.append("Sonarr")
    _, radarr = _fetch("/api/v1/settings/radarr")
    if not isinstance(radarr, list) or not any(
        isinstance(item, dict) and (item.get("hostname") or item.get("ip")) and (item.get("apiKey") or item.get("id"))
        for item in radarr
    ):
        missing.append("Radarr")
    ok = not missing
    detail = "linked" if ok else f"missing: {', '.join(missing)}"
    return VerifyCheck("seerr", "connections", ok, detail)


def verify_seerr_standard(cfg, secrets: dict[str, str], vm_ip: str, root: Path):
    return [verify_seerr_status(cfg, vm_ip, root), verify_seerr_connections(cfg, secrets, vm_ip, root)]
