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
        patch.object(KOMODO_FLEET, "docker_exec", return_value=(0, "[]")) as execute,
    ):
        rc, response = KOMODO_FLEET._komodo_request(tmp_path, "/read/ListServers", {"type": "ListServers"})

    assert (rc, response) == (0, [])
    assert execute.call_args.args == ("komodo-core", ["curl", "--disable", "--config", "-"])
    config = execute.call_args.kwargs["stdin"]
    assert "managed-api-key" not in repr(execute.call_args.args)
    assert "managed-api-secret" not in repr(execute.call_args.args)
    assert "managed-api-key" in config
    assert "managed-api-secret" in config
