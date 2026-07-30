from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]


def test_rsync_reports_actual_file_changes_only() -> None:
    task = (ROOT / "automation/ansible/tasks/_rsync-push.yml").read_text(encoding="utf-8")

    assert "--itemize-changes" in task
    assert "--omit-dir-times" in task or "rsync -azO" in task
    assert "rsync_push_result.stdout | trim | length > 0" in task
    assert "changed_when: rsync_push_result.rc == 0\n" not in task
