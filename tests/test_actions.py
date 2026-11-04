import json
from datetime import timedelta

import pytest

from conftest import NOW, make_comment, make_item, make_pr
from work_briefing import actions
from work_briefing.models import Draft

PR1 = "https://example.test/acme-labs/widget-api/pull/1"
EARLIER = (NOW - timedelta(days=2))


class FakeTracker:
    def __init__(self, status="in_review", available=("done",), list_error=None, transition_error=None, transition_ok=True):
        self.status, self.available, self.list_error = status, list(available), list_error
        self.transition_error, self.transition_ok = transition_error, transition_ok
        self.comments, self.transitions, self.listed = [], [], []

    def get_status(self, item_id):
        return self.status

    def available_transitions(self, item_id):
        self.listed.append(item_id)
        if self.list_error:
            raise self.list_error
        return self.available

    def comment(self, item_id, text):
        self.comments.append((item_id, text))

    def transition(self, item_id, to_status):
        if self.transition_error:
            raise self.transition_error
        self.transitions.append((item_id, to_status))
        return self.transition_ok


def classified(item, bucket="", **kw):
    item.bucket = bucket
    for k, v in kw.items():
        setattr(item, k, v)
    return item


def mismatch_item(status="blocked", kind="merged_open", pr=None, updated_days=5.0, **kw):
    pr = pr or make_pr("MERGED", merged_at=EARLIER)
    msg = {"merged_open": f"PR merged but item is still {status}", "open_todo": "PR is open but item is still todo",
           "closed_unmerged": "PR closed without merging but item is still in review"}[kind]
    return make_item("a#9", status=status, updated_days=updated_days, prs=[pr], mismatches=[msg], mismatch_kinds=[kind], **kw)


# --- close-out ------------------------------------------------------------------------------------------------------


def test_close_out_only_for_in_review_with_only_merged_prs():
    ok = make_item("a#1", status="in_review", prs=[make_pr("MERGED")])
    still_open = make_item("a#2", status="in_review", prs=[make_pr("MERGED"), make_pr(url=PR1 + "0")])
    blocked = make_item("a#3", status="blocked", prs=[make_pr("MERGED")])
    none = make_item("a#4", status="in_review")
    errored = make_item("a#5", status="in_review", prs=[make_pr("MERGED", error="x")])
    drafts = actions.build_drafts([ok, still_open, blocked, none, errored], NOW)
    assert [d.id for d in drafts] == ["a#1:close_out"]
    assert drafts[0].transition == "done" and PR1 in drafts[0].comment and drafts[0].status_at_draft == "in_review"


# --- nudge ----------------------------------------------------------------------------------------------------------


def test_nudge_needs_a_waiting_item_and_a_quiet_period():
    waiting = classified(make_item("a#1", status="in_review", updated_days=5, prs=[make_pr()],
                                   comments=[make_comment("me", 5, mine=True), make_comment("bob", 6)]), "waiting")
    [d] = actions.build_drafts([waiting], NOW)
    assert d.id == "a#1:nudge" and d.transition is None and d.comment.startswith("Hi @bob, checking in: this has been waiting for 5 days")
    fresh = classified(make_item("a#2", status="in_review", updated_days=1), "waiting")
    assert actions.build_drafts([fresh], NOW) == []
    assert actions.build_drafts([classified(make_item("a#3", status="in_review", updated_days=9), "ready")], NOW) == []


def test_nudge_quiet_period_counts_from_my_last_comment_and_skips_when_others_replied():
    mine = classified(make_item("a#1", status="in_review", updated_days=9,
                                comments=[make_comment("me", 1, mine=True)]), "waiting")
    assert actions.build_drafts([mine], NOW) == []
    replied = classified(make_item("a#2", status="in_review", updated_days=9, comments=[make_comment("bob", 1)]), "waiting")
    assert actions.build_drafts([replied], NOW) == []
    plain = classified(make_item("a#3", status="in_review", updated_days=9), "waiting")
    assert actions.build_drafts([plain], NOW)[0].comment.startswith("Hi, checking in")


def test_nudge_after_days_is_configurable():
    item = classified(make_item("a#1", status="in_review", updated_days=5), "waiting")
    assert actions.build_drafts([item], NOW, nudge_after_days=7) == [] and len(actions.build_drafts([item], NOW, nudge_after_days=5)) == 1


# --- status updates -------------------------------------------------------------------------------------------------


def test_merged_pr_on_a_blocked_item_gets_a_comment_only_draft_by_default():
    [d] = actions.build_drafts([mismatch_item()], NOW)
    assert (d.id, d.kind, d.transition, d.status_at_draft) == ("a#9:status_update", "status_update", None, "blocked")
    assert d.comment == f"Status update: PR {PR1} has been merged. This is still marked blocked; reviewing whether it can move forward."


def test_configured_transition_is_offered_and_worded_into_the_comment():
    [d] = actions.build_drafts([mismatch_item()], NOW, mismatch_transitions={"merged_open": "in_review"})
    assert d.transition == "in_review" and d.comment.endswith("Moving this from blocked to in review to match.")
    [same] = actions.build_drafts([mismatch_item(status="in_progress")], NOW, mismatch_transitions={"merged_open": "in_progress"})
    assert same.transition is None


def test_open_pr_on_a_todo_item_and_closed_unmerged_pr_texts():
    t = mismatch_item("todo", "open_todo", make_pr(created_at=EARLIER))
    [d] = actions.build_drafts([t], NOW, mismatch_transitions={"open_todo": "in_progress"})
    assert d.comment == f"Status update: PR {PR1} is open for review, but this is still marked todo. Moving it to in progress."
    t = mismatch_item("in_review", "closed_unmerged", make_pr("CLOSED", closed_at=EARLIER))
    [d] = actions.build_drafts([t], NOW)
    assert "was closed without merging" in d.comment and "still marked in review" in d.comment


def test_several_prs_use_plural_wording():
    t = mismatch_item()
    t.prs = [make_pr("MERGED", merged_at=EARLIER), make_pr("MERGED", url=PR1 + "0", merged_at=EARLIER)]
    [d] = actions.build_drafts([t], NOW)
    assert d.comment.startswith(f"Status update: PRs {PR1}, {PR1}0 have been merged.")


def test_no_draft_when_the_item_was_updated_after_the_event():
    assert actions.build_drafts([mismatch_item(updated_days=1.0)], NOW) == []  # touched after the merge two days ago
    assert len(actions.build_drafts([mismatch_item(updated_days=3.0)], NOW)) == 1


def test_without_pr_timestamps_my_recent_comment_suppresses_the_draft():
    recent = mismatch_item(pr=make_pr("MERGED"), comments=[make_comment("me", 1, mine=True)])
    old = mismatch_item(pr=make_pr("MERGED"), comments=[make_comment("me", 10, mine=True)])
    assert actions.build_drafts([recent], NOW) == [] and len(actions.build_drafts([old], NOW)) == 1
    assert len(actions.build_drafts([mismatch_item(pr=make_pr("MERGED"))], NOW)) == 1


def test_mismatch_drafts_can_be_switched_off_and_need_a_mismatch():
    assert actions.build_drafts([mismatch_item()], NOW, mismatch_drafts=False) == []
    quiet = make_item("a#9", status="blocked", prs=[make_pr("MERGED", merged_at=EARLIER)])
    assert actions.build_drafts([quiet], NOW) == []


# --- apply ----------------------------------------------------------------------------------------------------------

CLOSE = Draft("a#1:close_out", "a#1", "close_out", "Close out a#1", "Closing out: x merged.", "done", "in_review", "x")
COMMENT_ONLY = Draft("a#1:nudge", "a#1", "nudge", "Nudge", "Hi", None, "in_review", "x")


def test_apply_posts_comment_then_transition_when_confirmed():
    t, out = FakeTracker(), []
    assert actions.apply_draft(CLOSE, t, lambda p: True, out.append)
    assert t.comments == [("a#1", "Closing out: x merged.")] and t.transitions == [("a#1", "done")]
    assert any("Then move a#1 from 'in_review' to 'done'." in line for line in out) and out[-1] == "Moved to 'done'."


def test_apply_writes_nothing_when_declined_and_refuses_when_status_changed():
    t, out = FakeTracker(), []
    assert not actions.apply_draft(CLOSE, t, lambda p: False, out.append)
    assert t.comments == [] and t.transitions == [] and "nothing was written" in out[-1]
    t2, out2 = FakeTracker(status="done"), []
    assert not actions.apply_draft(CLOSE, t2, lambda p: True, out2.append)
    assert t2.comments == [] and "Refusing" in out2[0] and "'done'" in out2[0]


def test_assume_yes_skips_the_prompt():
    t = FakeTracker()
    assert actions.apply_draft(CLOSE, t, lambda p: pytest.fail("must not ask"), lambda s: None, assume_yes=True)
    assert len(t.comments) == 1


def test_unavailable_transition_posts_the_comment_only_and_says_why():
    t, out = FakeTracker(available=["blocked"]), []
    assert actions.apply_draft(CLOSE, t, lambda p: True, out.append)
    assert len(t.comments) == 1 and t.transitions == [] and out[-1] == "Comment posted."
    assert any("'done' is not available from 'in_review' (offered: blocked)" in line for line in out)


def test_failing_transition_lookup_still_posts_the_comment():
    t, out = FakeTracker(list_error=RuntimeError("503")), []
    assert actions.apply_draft(CLOSE, t, lambda p: True, out.append)
    assert len(t.comments) == 1 and any("Could not check" in line and "503" in line for line in out)


def test_transition_failure_after_the_comment_is_reported_not_raised():
    t, out = FakeTracker(transition_error=RuntimeError("boom")), []
    assert actions.apply_draft(CLOSE, t, lambda p: True, out.append)
    assert len(t.comments) == 1 and "Comment posted, but the status change failed: RuntimeError: boom" in out[-1]
    t2, out2 = FakeTracker(transition_ok=False), []
    assert actions.apply_draft(CLOSE, t2, lambda p: True, out2.append) and "no longer available" in out2[-1]


def test_comment_only_drafts_do_not_look_up_transitions():
    t, out = FakeTracker(), []
    assert actions.apply_draft(COMMENT_ONLY, t, lambda p: True, out.append)
    assert t.listed == [] and out[-1] == "Comment posted."


# --- persistence ----------------------------------------------------------------------------------------------------


def test_save_and_load_roundtrip_latest_file_wins_and_corrupt_files_are_skipped(tmp_path):
    actions.save_drafts(tmp_path, [CLOSE], NOW - timedelta(days=1))
    newer = Draft(**{**CLOSE.__dict__, "comment": "newer"})
    actions.save_drafts(tmp_path, [newer], NOW)
    (tmp_path / "drafts-2099-01-01.json").write_text("{broken")
    assert actions.load_draft(tmp_path, "a#1:close_out").comment == "newer"
    assert actions.load_draft(tmp_path, "nope") is None
    assert json.loads((tmp_path / f"drafts-{NOW:%Y-%m-%d}.json").read_text())[0]["id"] == "a#1:close_out"


def test_eod_drafts_live_in_their_own_file_and_are_found_after_a_refresh_rewrites_the_others(tmp_path):
    eod = Draft("a#1:eod_update", "a#1", "eod_update", "t", "c", None, "in_progress", "x")
    actions.save_drafts(tmp_path, [eod], NOW, prefix="drafts-eod")
    actions.save_drafts(tmp_path, [], NOW)
    assert actions.load_draft(tmp_path, "a#1:eod_update").comment == "c"


def test_saving_drafts_leaves_no_temp_files_and_replaces_in_one_step(tmp_path):
    actions.save_drafts(tmp_path, [CLOSE], NOW)
    actions.save_drafts(tmp_path, [], NOW)
    assert [p.name for p in tmp_path.iterdir()] == [f"drafts-{NOW:%Y-%m-%d}.json"]
    assert json.loads((tmp_path / f"drafts-{NOW:%Y-%m-%d}.json").read_text()) == []
