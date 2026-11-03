from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import NOW
from work_briefing import reports

TUE = NOW + timedelta(days=1)
FRI = NOW - timedelta(days=3)


def row(when, id_, kind="status", text="x", progress=False, key=None):
    r = {"ts": when.isoformat(), "id": id_, "kind": kind, "text": text, "progress": progress, "attention": False, "_ts": when}
    if key is not None:
        r["key"] = key
    return r


def latest(plan=(), items=(), generated=NOW):
    return {"generated": generated.isoformat(), "plan": list(plan), "items": list(items)}


def it(id="a#1", status="in_progress", bucket="ready", updated=NOW - timedelta(days=2), prs=(), **kw):
    return {"id": id, "title": f"Title {id}", "status": status, "bucket": bucket, "action": "Continue", "reasons": [],
            "updated": updated.isoformat(), "prs": list(prs), **kw}


def test_previous_working_day_skips_the_weekend():
    assert reports.previous_working_day(date(2026, 11, 2)) == date(2026, 10, 30)
    assert reports.previous_working_day(date(2026, 11, 3)) == date(2026, 11, 2)
    assert reports.previous_working_day(date(2026, 11, 1)) == date(2026, 10, 30)


def test_last_working_days_cross_weekends_and_end_on_friday_on_a_weekend():
    assert reports.last_working_days(date(2026, 11, 3)) == [date(2026, 10, 28), date(2026, 10, 29), date(2026, 10, 30),
                                                           date(2026, 11, 2), date(2026, 11, 3)]
    assert reports.last_working_days(date(2026, 11, 1))[-1] == date(2026, 10, 30)
    assert reports.last_working_days(date(2026, 11, 6), 1) == [date(2026, 11, 6)]


def test_activity_filters_by_window_and_kind_and_derives_keys_for_rows_without_one():
    log = [row(NOW, "a#1:status", "status", "s", True, key="a#1"), row(NOW, "a#2:my_comments", "my_comments", "c", True),
           row(NOW, "a#3:comments", "comments", "n"), row(NOW, "a#4:bucket", "bucket", "b"),
           row(NOW, "cleared:u", "cleared", "gone", True), row(NOW - timedelta(days=2), "a#5:status", "status", "old", True)]
    got = reports.activity(log, NOW - timedelta(hours=1), NOW + timedelta(hours=1))
    assert [(e.key, e.kind) for e in got] == [("", "cleared"), ("a#1", "status"), ("a#2", "my_comments")]


def test_progress_flag_makes_any_kind_count_and_the_window_end_is_exclusive():
    log = [row(NOW, "a#1:ci", "ci_ok", "ok", True, key="a#1")]
    assert len(reports.activity(log, NOW, NOW + timedelta(seconds=1))) == 1
    assert reports.activity(log, NOW - timedelta(hours=1), NOW) == []


def test_times_are_shown_in_the_callers_time_zone():
    cet = timezone(timedelta(hours=1))
    log = [row(datetime(2026, 11, 2, 23, 30, tzinfo=timezone.utc), "a#1:status", "status", "s", True, key="a#1")]
    [e] = reports.activity(log, datetime(2026, 11, 3, 0, 0, tzinfo=cet), datetime(2026, 11, 3, 9, 0, tzinfo=cet))
    assert e.when.day == 3 and e.when.hour == 0


# --- stand-up -------------------------------------------------------------------------------------------------------


def test_standup_on_monday_looks_back_to_friday_and_lists_plan_and_blockers():
    log = [row(FRI.replace(hour=15), "a#1:status", "status", "a#1 status in_review -> done", True, key="a#1"),
           row(NOW - timedelta(hours=1), "a#2:my_comments", "my_comments", "a#2: posted 1 comment(s)", True, key="a#2")]
    plan = [{"rank": 1, "ref": "a#2", "title": "Do the thing", "next": "Open the PR"}]
    blocked = it("a#3", "blocked", "blocked", reasons=["waiting on Bob"], action="Still blocked")
    text = reports.standup(log, latest(plan, [it("a#2"), blocked]), NOW)
    assert "# Stand-up 2026-11-02 (Mon)" in text and "## Last working day (Friday) (Fri 2026-10-30)" in text
    assert "a#1 status in_review -> done" in text and "Fri 15:00" in text
    assert "## Already today" in text and "a#2: posted 1 comment(s)" in text
    assert "1. **a#2** Do the thing" in text and "Next: Open the PR" in text
    assert "- **a#3** Title a#3 (blocked): Still blocked. waiting on Bob" in text


def test_standup_midweek_says_yesterday_and_copes_with_an_empty_log():
    text = reports.standup([], latest(), TUE)
    assert "## Yesterday (Mon 2026-11-02)\n- Nothing logged." in text and "Already today" not in text
    assert "(no plan in the latest briefing)" in text and "Nothing blocked or waiting." in text


def test_reports_warn_when_the_briefing_is_missing_or_not_from_today():
    assert "No briefing found yet" in reports.standup([], None, NOW)
    assert "not today" in reports.standup([], latest(generated=NOW - timedelta(days=1)), NOW)
    assert "not today" not in reports.standup([], latest(), NOW)


def test_events_without_an_item_are_listed_separately():
    log = [row(NOW - timedelta(hours=1), "cleared:u", "cleared", "No longer waiting on you: review", True)]
    assert "- Not tied to an item\n  - 11:00: No longer waiting on you: review" in reports.standup(log, latest(), NOW)


# --- week -----------------------------------------------------------------------------------------------------------


def week_log():
    d = lambda days, h=10: (NOW - timedelta(days=days)).replace(hour=h)  # noqa: E731
    return [
        row(d(3), "a#1:status", "status", "a#1 status in_progress -> done", True, key="a#1"),
        row(d(3, 11), "a#1:my_comments", "my_comments", "a#1: posted 3 comment(s)", True, key="a#1"),
        row(d(4, 9), "a#2:status", "status", "a#2 status todo -> in_progress", True, key="a#2"),
        row(d(0, 9), "a#2:u:state", "pr_state", "a#2: PR o/r#7 OPEN -> MERGED", True, key="a#2"),
        row(d(0, 10), "a#3:status", "status", "a#3 status in_progress -> blocked", False, key="a#3"),
        row(d(0, 10), "a#4:left", "left", "a#4 left your list", True, key="a#4"),
        row(d(9), "a#9:status", "status", "too old", True, key="a#9"),
    ]


def test_week_counts_and_groups_by_item_without_hours():
    text = reports.week(week_log(), latest(items=[it("a#2")]), NOW)
    assert "# Week Tue 2026-10-27 to Mon 2026-11-02" in text
    assert "- 4 item(s) with activity, 2 advanced, 2 closed out (done, or left my list)" in text
    assert "- 1 PR(s) merged, 3 comment(s) posted by me" in text
    assert "No hours" in text and "a#9" not in text
    assert "- **a#2** Title a#2 (in_progress)\n  - Thu 09:00: a#2 status todo -> in_progress" in text


def test_week_says_when_the_log_starts_late_or_is_empty():
    late = [row(NOW, "a#1:status", "status", "s", True, key="a#1")]
    assert "The event log starts Mon 2026-11-02; earlier days in this window are not included." in reports.week(late, latest(), NOW)
    empty = reports.week([], latest(), NOW)
    assert "The event log is empty" in empty and "Nothing logged in this period." in empty
    full = [row(NOW - timedelta(days=8), "a#1:status", "status", "s", True, key="a#1"), *late]
    assert "event log starts" not in reports.week(full, latest(), NOW)


def test_closed_unmerged_prs_are_not_counted_as_merged():
    log = [row(NOW - timedelta(hours=1), "a#1:u:state", "pr_state", "a#1: PR o/r#7 OPEN -> CLOSED", False, key="a#1")]
    assert "- 0 PR(s) merged" in reports.week(log, latest(), NOW)


def test_reports_are_deterministic_whatever_the_log_order():
    a, b = week_log(), list(reversed(week_log()))
    assert reports.week(a, latest(), NOW) == reports.week(b, latest(), NOW)
    assert reports.standup(a, latest(), NOW) == reports.standup(b, latest(), NOW)


# --- end of day -----------------------------------------------------------------------------------------------------


def test_eod_drafts_only_for_in_progress_items_not_updated_today():
    pr = {"state": "OPEN", "url": "https://example.test/o/r/pull/1", "review_state": "review_required", "ci_fail": 1}
    stale = it("a#1", prs=[pr])
    plan = [{"rank": 1, "ref": "a#1", "title": "t", "next": "Fix the lint failure."}]
    items = [stale, it("a#2", updated=NOW - timedelta(hours=2)), it("a#3", "blocked", "blocked"), it("a#4", "in_review")]
    text, drafts = reports.eod([], latest(plan, items), NOW)
    [d] = drafts
    assert (d.id, d.kind, d.transition, d.status_at_draft) == ("a#1:eod_update", "eod_update", None, "in_progress")
    assert d.comment == ("End-of-day update: still in progress. PR https://example.test/o/r/pull/1 is open "
                         "(awaiting review, CI failing). Next: Fix the lint failure.")
    assert "`work-briefing apply a#1:eod_update`" in text and "a#2:eod_update" not in text


def test_eod_comment_falls_back_to_the_action_and_ignores_closed_prs():
    t = it("a#1", prs=[{"state": "MERGED", "url": "u", "review_state": "none", "ci_fail": 0}])
    [d] = reports.eod([], latest([], [t]), NOW)[1]
    assert d.comment == "End-of-day update: still in progress. Next: Continue."


def test_eod_with_nothing_to_post_lists_the_days_progress():
    log = [row(NOW - timedelta(hours=1), "a#1:status", "status", "a#1 status todo -> in_progress", True, key="a#1")]
    text, drafts = reports.eod(log, latest([], [it("a#1", updated=NOW)]), NOW)
    assert drafts == [] and "None: every in-progress item was updated today." in text and "11:00: a#1 status todo -> in_progress" in text
    assert "Nothing logged today." in reports.eod([], latest(), NOW)[0]


def test_eod_ignores_items_with_unreadable_update_times():
    bad = it("a#1")
    bad["updated"] = "not a date"
    assert reports.eod([], latest([], [bad]), NOW)[1] == []


def test_build_report_rejects_unknown_kinds():
    with pytest.raises(ValueError, match="unknown report"):
        reports.build_report("monthly", [], None, NOW)
    assert reports.build_report("week", [], None, NOW)[1] == []
