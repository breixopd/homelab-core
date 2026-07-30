"""Detect containers running images older than a threshold."""

from __future__ import annotations

import datetime
import subprocess
from collections.abc import Callable


def list_stale_container_images(
    *,
    max_age_days: int = 90,
    docker_bin: str = "docker",
    run: Callable[[list[str], int], subprocess.CompletedProcess] | None = None,
) -> list[dict]:
    """Return containers whose image was created more than max_age_days ago."""
    _run = run or (
        lambda cmd, timeout: subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    )
    old: list[dict] = []
    result = _run([docker_bin, "ps", "--format", "{{.Names}}\t{{.Image}}"], 15)
    if result.returncode != 0:
        return old

    now = datetime.datetime.now(datetime.UTC)
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t", 1)
        if len(parts) < 2:
            continue
        name, image = parts
        inspect = _run([docker_bin, "inspect", "--format", "{{.Created}}", image], 10)
        if inspect.returncode != 0:
            continue
        created = inspect.stdout.strip()
        if "T" not in created:
            continue
        try:
            ts = created
            if "." in ts:
                base, frac = ts.split(".", 1)
                tz_start = next((i for i, c in enumerate(frac) if c in "Z+-"), len(frac))
                frac_digits = frac[:tz_start][:6]
                tz_suffix = frac[tz_start:]
                ts = f"{base}.{frac_digits}{tz_suffix}"
            ts = ts.replace("Z", "+00:00")
            created_dt = datetime.datetime.fromisoformat(ts)
            age_days = (now - created_dt).days
            if age_days > max_age_days:
                old.append({"name": name, "image": image, "age_days": age_days})
        except (ValueError, IndexError):
            continue
    return old
