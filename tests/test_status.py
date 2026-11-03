from datetime import timedelta

from conftest import NOW
from work_briefing import status


def test_failure_then_success_cycle_and_streak_counting(tmp_path):
    assert status.load(tmp_path) == {}
    st = status.record_failure(tmp_path, "boom", NOW)
    assert st["consecutive_failures"] == 1 and st["last_success"] == ""
    st = status.record_failure(tmp_path, "boom again", NOW + timedelta(minutes=5))
    assert st["consecutive_failures"] == 2 and status.load(tmp_path)["last_error"] == "boom again"
    status.record_success(tmp_path, NOW + timedelta(minutes=10))
    ok = status.load(tmp_path)
    assert ok["consecutive_failures"] == 0 and ok["last_error"] == "" and ok["previous_failures"] == 2


def test_last_success_survives_later_failures(tmp_path):
    status.record_success(tmp_path, NOW)
    st = status.record_failure(tmp_path, "x", NOW + timedelta(hours=1))
    assert st["last_success"] == NOW.isoformat()


def test_one_notice_per_streak_and_threshold(tmp_path):
    assert not status.should_notify(tmp_path)
    status.record_failure(tmp_path, "x", NOW)
    assert status.should_notify(tmp_path) and not status.should_notify(tmp_path, threshold=2)
    status.mark_notified(tmp_path)
    status.record_failure(tmp_path, "x", NOW + timedelta(minutes=5))
    assert not status.should_notify(tmp_path)  # same streak
    status.record_success(tmp_path, NOW + timedelta(minutes=10))
    status.record_failure(tmp_path, "x", NOW + timedelta(minutes=20))
    assert status.should_notify(tmp_path)  # a new streak


def test_banner_is_added_replaced_and_removed_without_touching_the_body(tmp_path):
    (tmp_path / "latest.md").write_text("# Briefing\nbody\n")
    status.record_failure(tmp_path, "first", NOW)
    status.record_failure(tmp_path, "second", NOW)
    text = (tmp_path / "latest.md").read_text()
    assert text.count("last refresh failed") == 1 and "second" in text and "first" not in text and text.endswith("# Briefing\nbody\n")
    status.record_success(tmp_path, NOW)
    assert (tmp_path / "latest.md").read_text() == "# Briefing\nbody\n"


def test_banner_is_skipped_when_there_is_no_briefing_yet_and_mentions_missing_history(tmp_path):
    status.record_failure(tmp_path, "x", NOW)
    assert not (tmp_path / "latest.md").exists()
    assert "no earlier briefing" in status.banner_text(status.load(tmp_path))
    status.record_success(tmp_path, NOW)
    assert "from 2026-11-02 12:00" in status.banner_text({"consecutive_failures": 1, "last_error": "e", "last_success": NOW.isoformat()})


def test_error_descriptions_are_one_redacted_line_and_permanent_errors_are_recognised():
    e = RuntimeError("line one\nline two token=abc123 " + "x" * 400)
    msg = status.describe_error(e)
    assert "\n" not in msg and "abc123" not in msg and len(msg) <= 300 and msg.startswith("RuntimeError: line one line two")
    assert status.is_permanent("gh: not found") and status.is_permanent("You are not logged in") and not status.is_permanent("timeout")


def test_damaged_status_file_reads_as_empty(tmp_path):
    (tmp_path / "status.json").write_text("[1,2]")
    assert status.load(tmp_path) == {}
    (tmp_path / "status.json").write_text("{bad")
    assert status.load(tmp_path) == {}
