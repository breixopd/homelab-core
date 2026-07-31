from __future__ import annotations

import importlib
from pathlib import Path
from unittest.mock import patch

KOMODO_FLEET = importlib.import_module("toolkit.services.komodo-core.fleet")


def test_komodo_fleet_request_keeps_api_credentials_off_process_arguments(tmp_path: Path) -> None:
    with (
        patch.object(
            KOMODO_FLEET,
            "load_secrets_plaintext",
            return_value={"KOMODO_API_KEY": "managed-api-key", "KOMODO_API_SECRET": "managed-api-secret"},
        ),
        patch.object(KOMODO_FLEET, "docker_curl", return_value=(0, "[]")) as execute,
    ):
        rc, response = KOMODO_FLEET._komodo_request(tmp_path, "/read/ListServers", {"type": "ListServers"})

    assert (rc, response) == (0, [])
    assert execute.call_args.args == ("komodo-core", "http://127.0.0.1:9120/read/ListServers")
    assert "managed-api-key" not in repr(execute.call_args.args)
    assert "managed-api-secret" not in repr(execute.call_args.args)
    assert execute.call_args.kwargs["method"] == "POST"
    assert execute.call_args.kwargs["headers"]["X-Api-Key"] == "managed-api-key"
    assert execute.call_args.kwargs["headers"]["X-Api-Secret"] == "managed-api-secret"
