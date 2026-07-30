from pathlib import Path

import yaml


def test_vfs_cache_age_tracks_media_cache_cold_age_setting() -> None:
    compose_path = Path(__file__).resolve().parents[3] / "toolkit/services/rclone/compose.yaml"
    compose = yaml.safe_load(compose_path.read_text(encoding="utf-8"))
    command = compose["services"]["rclone"]["command"]

    assert "--vfs-cache-max-age ${COLD_AFTER_DAYS:-15}d" in command
    assert "RCLONE_CACHE_MAX_AGE" not in command
