"""Pass secret container environment variables through stdin, never argv."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence

_ENVIRONMENT_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
SECRET_ENV_MARKER = "__HOMELAB_SECRET_ENV_END__"
_SECRET_ENV_WRAPPER = f"""set -eu
marker={SECRET_ENV_MARKER}
found=0
while IFS= read -r assignment; do
    if [ \"$assignment\" = \"$marker\" ]; then
        found=1
        break
    fi
    export \"$assignment\"
done
[ \"$found\" -eq 1 ] || exit 64
exec \"$@\"
"""


def wrap_command_with_secret_environment(
    command: Sequence[str],
    *,
    environment: Mapping[str, str] | None = None,
    secret_environment: Mapping[str, str] | None = None,
    stdin: str | None = None,
) -> tuple[list[str], str | None]:
    """Wrap a command so secret environment values travel only over stdin.

    The fixed shell wrapper consumes ``NAME=value`` records through a marker,
    exports them, and then execs the requested command with the caller's stdin
    still available.  Regular ``environment`` values remain Docker ``-e``
    arguments because they are intentionally non-secret.
    """
    if not secret_environment:
        return list(command), stdin

    public_names = set((environment or {}).keys())
    records: list[str] = []
    for name, value in secret_environment.items():
        if not isinstance(name, str) or not _ENVIRONMENT_NAME.fullmatch(name):
            raise ValueError(f"invalid secret environment variable name: {name!r}")
        if name in public_names:
            raise ValueError(f"secret environment variable duplicates public environment: {name}")
        if not isinstance(value, str) or any(ord(char) < 32 or ord(char) == 127 for char in value):
            raise ValueError(f"secret environment variable {name} contains control characters")
        if SECRET_ENV_MARKER in value:
            raise ValueError(f"secret environment variable {name} contains reserved marker")
        records.append(f"{name}={value}")

    payload = "\n".join([*records, SECRET_ENV_MARKER, ""])
    if stdin is not None:
        payload += stdin
    return ["sh", "-ec", _SECRET_ENV_WRAPPER, "homelab-secret-env", *command], payload
