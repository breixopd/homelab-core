"""Stable secret names derived from managed project identifiers."""

from __future__ import annotations

import re

_PROJECT_SUBDOMAIN = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,53}[a-z0-9])?$")


def project_database_secret_name(subdomain: str) -> str:
    if not _PROJECT_SUBDOMAIN.fullmatch(subdomain):
        raise ValueError("project database secret requires a valid subdomain")
    return f"{subdomain.upper().replace('-', '_')}_POSTGRES_PASSWORD"
