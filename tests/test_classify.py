from datetime import date, timedelta

from conftest import NOW, make_comment, make_item, make_pr
from work_briefing import classify as rules


def run(item, stale_days=7):
    rules.classify(item, NOW, stale_days)
    rules.score(item, NOW)
    return item


# --- buckets -------------------------------------------------------------------------------------------------


def test_in_progress_with_recent_activity_is_ready_to_continue():
    i = run(make_item())
    assert (i.bucket, i.action, i.quick) == ("ready", "Continue", False)


def test_in_progress_goes_stale_unless_someone_commented():
    assert run(make_item(updated_days=20)).bucket == "stale"
    i = run(make_item(updated_days=20, comments=[make_comment("bob", 2)]))
    assert i.bucket == "ready" and i.action == "Respond to new comments, then continue"


def test_my_own_comment_clears_older_comments_from_others():
    i = run(make_item(comments=[make_comment("me", 1, mine=True), make_comment("bob", 3)]))
    assert i.others_since == [] and i.action == "Continue"


def test_staleness_starts_exactly_at_stale_days():
    assert run(make_item(updated_days=7)).bucket == "stale" and run(make_item(updated_days=6.9)).bucket == "ready"


def test_unanswered_comment_urgency_is_capped_at_a_week():
    year_old = run(make_item(comments=[make_comment("bob", 365)]))
    week_old = run(make_item(comments=[make_comment("bob", 7)]))
    assert year_old.score == week_old.score and "unanswered comments from others (365d) (+22)" in year_old.score_parts


def test_stale_days_is_configurable():
    assert run(make_item(updated_days=5), stale_days=3).bucket == "stale"
    assert run(make_item(updated_days=5), stale_days=10).bucket == "ready"


def test_todo_is_start_when_fresh_or_prioritised_and_backlog_when_old():
    assert run(make_item(status="todo", updated_days=3)).bucket == "start"
    assert run(make_item(status="todo", updated_days=60, priority="high")).bucket == "start"
    assert run(make_item(status="todo", updated_days=60)).bucket == "backlog"


def test_open_blockers_make_a_todo_or_blocked_item_blocked_unless_someone_wrote():
    assert run(make_item(status="todo", blockers=["acme-labs/widget-api#9"])).bucket == "blocked"
    b = run(make_item(status="blocked", blockers=["acme-labs/widget-api#9"]))
    assert b.bucket == "blocked" and "acme-labs/widget-api#9" in b.action
    assert run(make_item(status="blocked", blockers=["x#1"], comments=[make_comment("bob", 1)])).bucket == "ready"
    c = run(make_item(status="blocked"))  # a blocked label with no linked blocker stays blocked until someone writes
    assert (c.bucket, c.action) == ("blocked", "Still blocked")
    assert run(make_item(status="blocked", comments=[make_comment("bob", 1)])).action == "Respond, then unblock"


def test_in_review_waits_unless_there_is_something_to_do():
    w = run(make_item(status="in_review", updated_days=5, prs=[make_pr()]))
    assert (w.bucket, w.action) == ("waiting", "Waiting on reviewer (5d since update)")
    addressed = run(make_item(status="in_review", prs=[make_pr(review_state="changes_addressed")]))
    assert addressed.action == "Waiting on reviewer (changes addressed)"
    assert run(make_item(status="in_review", comments=[make_comment("bob", 1)])).action == "Address review feedback"


def test_approved_pr_is_a_quick_merge_unless_ci_fails_or_it_is_a_draft():
    ok = run(make_item(status="in_review", prs=[make_pr(review_state="approved")]))
    assert (ok.bucket, ok.action, ok.quick) == ("ready", "Approved: merge", True)
    red = run(make_item(status="in_review", prs=[make_pr(review_state="approved", ci_fail=1)]))
    assert red.action == "Fix failing CI on PR" and not red.quick  # approved but red: only the CI is left to fix
    assert not run(make_item(status="in_review", prs=[make_pr(review_state="approved", draft=True)])).quick
    assert run(make_item(prs=[make_pr(review_state="approved")])).quick  # also while still in progress


def test_in_review_with_everything_merged_is_a_quick_close_out():
    i = run(make_item(status="in_review", prs=[make_pr("MERGED")]))
    assert (i.action, i.quick) == ("Merged: close out", True)


def test_in_progress_with_failing_ci_or_changes_requested():
    ci = run(make_item(prs=[make_pr(ci_fail=1, failing_checks=["lint"])]))
    assert ci.action == "Fix failing CI on PR" and any("CI failing (lint)" in r for r in ci.reasons)
    cr = run(make_item(prs=[make_pr(review_state="changes_requested", ci_fail=1)]))
    assert cr.action == "Address review feedback"


def test_prs_that_could_not_be_read_are_ignored_by_the_rules():
    i = run(make_item(prs=[make_pr(error="not found", state="MERGED")]))
    assert i.mismatches == [] and i.action == "Continue"


# --- mismatches ----------------------------------------------------------------------------------------------


def test_mismatch_kinds_run_parallel_to_the_messages():
    for status, pr, kind in (("blocked", make_pr("MERGED"), "merged_open"), ("in_review", make_pr("CLOSED"), "closed_unmerged"),
                             ("todo", make_pr(), "open_todo")):
        i = run(make_item(status=status, prs=[pr]))
        assert i.mismatch_kinds == [kind] and len(i.mismatches) == 1 and f"Check: {i.mismatches[0]}" in i.reasons
        assert "status mismatch (+3)" in i.score_parts
    assert run(make_item(status="in_review", prs=[make_pr("MERGED")])).mismatches == []  # handled as a close-out
    assert run(make_item(status="done", prs=[make_pr("MERGED")])).mismatches == []


def test_a_merged_pr_next_to_an_open_one_is_not_a_mismatch():
    assert run(make_item(prs=[make_pr("MERGED"), make_pr(url="https://example.test/x/y/pull/2")])).mismatches == []


# --- scoring and the plan ------------------------------------------------------------------------------------


def test_score_parts_explain_the_number():
    i = run(make_item(priority="High", due=NOW.date() + timedelta(days=2), comments=[make_comment("bob", 3)]))
    assert i.score == sum(float(p.rsplit("(", 1)[1].rstrip(")")) for p in i.score_parts)
    assert any("due in 2d" in p for p in i.score_parts) and any("priority High" in p for p in i.score_parts)
    assert any("unanswered comments from others (3d)" in p for p in i.score_parts)


def test_overdue_beats_due_soon_and_due_far_away_scores_nothing():
    over = run(make_item(due=date(2026, 10, 30))).score
    soon = run(make_item(due=date(2026, 11, 4))).score
    later = run(make_item(due=date(2026, 12, 25))).score
    assert over > soon > later == run(make_item()).score


def test_waiting_and_blocked_rank_below_ready():
    items = [run(make_item("a#1", status="in_review", prs=[make_pr()], updated_days=5)),
             run(make_item("a#2", status="blocked", blockers=["a#9"])), run(make_item("a#3"))]
    assert [i.id for i in sorted(items, key=lambda i: -i.score)] == ["a#3", "a#1", "a#2"]


def test_plan_lists_only_actionable_items_best_first_and_respects_size():
    items = [run(make_item("a#1")), run(make_item("a#2", priority="critical")),
             run(make_item("a#3", status="in_review", prs=[make_pr()], updated_days=5)),
             run(make_item("a#4", status="todo", updated_days=90))]
    plan = rules.build_plan(items, 5)
    assert [p["ref"] for p in plan] == ["a#2", "a#1"] and plan[0]["rank"] == 1 and "priority critical" in plan[0]["why"]
    assert [p["ref"] for p in rules.build_plan(items, 1)] == ["a#2"]


def test_plan_order_is_deterministic_for_ties():
    items = [run(make_item(f"a#{n}")) for n in (3, 1, 2)]
    assert [p["ref"] for p in rules.build_plan(items, 5)] == ["a#1", "a#2", "a#3"]


def test_quick_wins_are_listed_best_first():
    a = run(make_item("a#1", prs=[make_pr(review_state="approved")]))
    b = run(make_item("a#2", status="in_review", prs=[make_pr("MERGED")], priority="high"))
    assert rules.quick_wins([a, b, run(make_item("a#3"))]) == ["a#2", "a#1"]


def test_incoming_requests_join_the_plan_by_score():
    from work_briefing.models import Incoming
    waiting = Incoming("review_request", "https://example.test/x/pull/3", "x/y: Add thing", detail="by bob", when=NOW - timedelta(days=4))
    rules.score_incoming(waiting, NOW)
    assert waiting.score == 14
    plan = rules.build_plan([run(make_item("a#1"))], 5, [waiting])
    assert [(p["kind"], p["ref"]) for p in plan] == [("item", "a#1"), ("review_request", waiting.id)]  # ready items score 25+
    assert plan[1]["next"] == "Review it" and plan[1]["why"] == "by bob"
    assert [p["ref"] for p in rules.build_plan([], 1, [waiting])] == [waiting.id]


def test_incoming_score_grows_with_waiting_time_up_to_a_cap():
    from work_briefing.models import Incoming
    def score(days):
        x = Incoming("review_request", "u", "t", when=NOW - timedelta(days=days))
        rules.score_incoming(x, NOW)
        return x.score
    assert [score(0), score(2), score(5), score(30)] == [10, 12, 15, 15]
    x = Incoming("review_request", "u", "t")
    rules.score_incoming(x, NOW)
    assert x.score == 10
