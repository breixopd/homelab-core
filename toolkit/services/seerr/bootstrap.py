"""Seerr bootstrap helper: read API key from settings.json after first start."""

from __future__ import annotations

import json
from pathlib import Path


def extract_seerr_api_key(root: Path) -> str | None:
    """Read the Seerr API key from settings.json (written on first startup)."""
    path = root / "data/seerr/config/settings.json"
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text())
        key = (data.get("main") or {}).get("apiKey")
        if key:
            return str(key).strip()
    except (json.JSONDecodeError, OSError, TypeError):
        pass
    return None
