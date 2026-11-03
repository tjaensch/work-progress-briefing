import json
from datetime import timedelta

from conftest import NOW, make_comment, make_item, make_pr
from work_briefing import state
from work_briefing.models import Briefing, Change


def snap_of(*items, now=NOW):
    return state.snapshot(Briefing(now=now, items=list(items)))


def kinds(changes):
    return {c.kind: c for c in changes}


def test_new_item_needs_attention_and_departed_item_counts_as_progress():
    old = snap_of(make_item("a#1"))
    new = snap_of(make_item("a#2", title="Fresh"))
    got = kinds(state.diff_events(old, new))
    assert got["new"].attention and got["new"].key == "a#2" and "Fresh" in got["new"].text
    assert got["left"].progress and got["left"].key == "a#1"


def test_status_moves_forward_are_progress_and_moves_into_blocked_are_not():
    base = snap_of(make_item("a#1", status="in_progress"))
    fwd = kinds(state.diff_events(base, snap_of(make_item("a#1", status="in_review"))))["status"]
    back = kinds(state.diff_events(base, snap_of(make_item("a#1", status="todo"))))["status"]
    blocked = kinds(state.diff_events(base, snap_of(make_item("a#1", status="blocked"))))["status"]
    assert (fwd.progress, back.progress, blocked.progress) == (True, False, False)
    assert fwd.text == "a#1 status in_progress -> in_review"


def test_bucket_change_is_reported_only_when_the_status_did_not_change():
    old = snap_of(make_item("a#1", updated_days=1))
    new_item = make_item("a#1", updated_days=1)
    new = snap_of(new_item)
    new["items"]["a#1"]["bucket"] = "stale"
    assert kinds(state.diff_events(old, new))["bucket"].text == "a#1 moved from  to stale"


def test_comments_from_others_need_attention_and_mine_are_progress():
    old = snap_of(make_item("a#1"))
    new = snap_of(make_item("a#1", comments=[make_comment("bob", 0.1), make_comment("me", 0.2, mine=True, cid="2")]))
    got = kinds(state.diff_events(old, new))
    assert got["comments"].attention and "latest by bob" in got["comments"].text
    assert got["my_comments"].progress and "posted 1 comment(s)" in got["my_comments"].text


def test_pr_changes_state_review_and_ci():
    url = "https://example.test/acme-labs/widget-api/pull/7"
    old = snap_of(make_item("a#1", prs=[make_pr(url=url)]))
    new = snap_of(make_item("a#1", prs=[make_pr("MERGED", "approved", url=url)]))
    got = kinds(state.diff_events(old, new))
    assert got["pr_state"].progress and "widget-api#7 OPEN -> MERGED" in got["pr_state"].text
    assert got["pr_review"].attention
    red = kinds(state.diff_events(old, snap_of(make_item("a#1", prs=[make_pr(url=url, ci_fail=1)]))))["ci_fail"]
    green = kinds(state.diff_events(snap_of(make_item("a#1", prs=[make_pr(url=url, ci_fail=1)])), old))["ci_ok"]
    assert red.attention and not green.attention
    appeared = kinds(state.diff_events(snap_of(make_item("a#1")), old))["pr_new"]
    assert not appeared.progress and "(OPEN)" in appeared.text


def test_cleared_blocker_is_progress_and_needs_attention():
    old = snap_of(make_item("a#1", status="blocked", blockers=["a#9"]))
    new = snap_of(make_item("a#1", status="blocked"))
    c = kinds(state.diff_events(old, new))["blocker"]
    assert c.progress and c.attention and c.id == "a#1:blocker:a#9"


def test_prs_with_errors_are_not_part_of_the_snapshot():
    s = snap_of(make_item("a#1", prs=[make_pr(error="boom")]))
    assert s["items"]["a#1"]["prs"] == {}


def test_snapshot_includes_hidden_items_so_snoozing_is_not_leaving():
    b = Briefing(now=NOW, items=[make_item("a#1")])
    assert set(state.snapshot(b, hidden=[make_item("a#2")])["items"]) == {"a#1", "a#2"}


def test_incoming_items_arrive_needing_attention_and_clear_as_progress():
    from work_briefing.models import Incoming

    def with_incoming(*xs, now=NOW):
        return state.snapshot(Briefing(now=now, incoming=list(xs)))

    x = Incoming("review_request", "https://example.test/x/pull/3", "x/y: Add thing", when=NOW - timedelta(days=1))
    arrived = kinds(state.diff_events(with_incoming(), with_incoming(x)))["incoming"]
    assert arrived.attention and arrived.id == f"incoming:{x.id}" and arrived.key == ""
    cleared = kinds(state.diff_events(with_incoming(x), with_incoming()))["cleared"]
    assert cleared.progress and "No longer waiting on you: x/y: Add thing" in cleared.text


def test_an_incoming_item_that_merely_aged_out_is_not_progress():
    from work_briefing.models import Incoming
    old = Incoming("review_request", "u", "old one", when=NOW - timedelta(days=30))
    before = state.snapshot(Briefing(now=NOW, incoming=[old]))
    assert state.diff_events(before, state.snapshot(Briefing(now=NOW))) == []


def test_snapshot_includes_hidden_incoming_items():
    from work_briefing.models import Incoming
    s = state.snapshot(Briefing(now=NOW), hidden_incoming=[Incoming("review_request", "u", "t")])
    assert s["incoming"] == {"u": "t"}


def test_item_of_ignores_non_item_ids():
    assert state.item_of("a#1:status") == "a#1" and state.item_of("incoming:x") == "" and state.item_of("cleared:y") == ""


# --- history and deltas ----------------------------------------------------------------------------------------


def test_history_roundtrip_ignores_corrupt_files_and_prunes(tmp_path):
    for n in range(state.KEEP_SNAPSHOTS + 3):
        t = NOW - timedelta(minutes=n)
        state.save_snapshot(tmp_path, snap_of(make_item("a#1"), now=t), t)
    (tmp_path / "snapshots" / "snap-00000000-000000.json").write_text("{not json")
    hist = state.load_history(tmp_path)
    assert len(hist) <= state.KEEP_SNAPSHOTS and hist == sorted(hist, key=lambda x: x[0])


def test_compute_changes_uses_last_snapshot_from_before_today_as_baseline():
    yesterday, earlier_today = NOW - timedelta(days=1), NOW - timedelta(hours=2)
    s_old = snap_of(make_item("a#1", status="todo"), now=yesterday)
    s_mid = snap_of(make_item("a#1", status="in_progress"), now=earlier_today)
    new = snap_of(make_item("a#1", status="in_review"), now=NOW)
    d = state.compute_changes([(yesterday, s_old), (earlier_today, s_mid)], new, NOW)
    assert d.baseline == yesterday and d.last_check == earlier_today
    assert [c.text for c in d.recent] == ["a#1 status in_progress -> in_review"] and d.recent[0].is_new
    assert d.changes[0].text == "a#1 status todo -> in_review" and d.changes[0].is_new is True  # same change id as recent


def test_a_change_that_happened_before_the_last_check_is_not_new():
    yesterday, earlier_today = NOW - timedelta(days=1), NOW - timedelta(hours=2)
    s_old = snap_of(make_item("a#1", status="todo"), now=yesterday)
    s_mid = snap_of(make_item("a#1", status="in_progress"), now=earlier_today)
    new = snap_of(make_item("a#1", status="in_progress"), now=NOW)
    d = state.compute_changes([(yesterday, s_old), (earlier_today, s_mid)], new, NOW)
    assert d.recent == [] and [(c.text, c.is_new) for c in d.changes] == [("a#1 status todo -> in_progress", False)]


def test_compute_changes_marks_changes_that_are_also_new_since_last_check():
    yesterday, earlier_today = NOW - timedelta(days=1), NOW - timedelta(hours=2)
    s_old = snap_of(make_item("a#1", status="todo"), now=yesterday)
    s_mid = snap_of(make_item("a#1", status="todo"), now=earlier_today)
    new = snap_of(make_item("a#1", status="in_progress"), now=NOW)
    d = state.compute_changes([(yesterday, s_old), (earlier_today, s_mid)], new, NOW)
    assert d.changes[0].is_new is True
    assert state.compute_changes([(yesterday, s_old), (earlier_today, s_mid)], new, NOW, since_last=True).baseline == earlier_today


def test_compute_changes_without_history_is_empty():
    d = state.compute_changes([], snap_of(make_item("a#1")), NOW)
    assert (d.last_check, d.changes, d.recent) == (None, [], [])


# --- event log -------------------------------------------------------------------------------------------------


def test_log_roundtrip_drops_old_rows_and_damaged_lines(tmp_path):
    state.append_log(tmp_path, [Change("a#1:status", "status", "a#1 status todo -> done", key="a#1", progress=True)],
                     NOW - timedelta(days=40))
    state.append_log(tmp_path, [Change("a#2:comments", "comments", "c", key="a#2", attention=True)], NOW)
    with open(tmp_path / "events.jsonl", "a") as f:
        f.write("{oops\n")
    log = state.load_log(tmp_path)
    assert [e["id"] for e in log] == ["a#2:comments"] and log[0]["attention"] is True and log[0]["key"] == "a#2"


def test_progress_today_merges_log_and_this_run_newest_first_and_ignores_other_days(tmp_path):
    state.append_log(tmp_path, [Change("a#1:status", "status", "morning win", key="a#1", progress=True),
                                Change("a#1:ci", "ci_ok", "not progress")], NOW - timedelta(hours=3))
    state.append_log(tmp_path, [Change("a#0:status", "status", "yesterday", progress=True)], NOW - timedelta(days=1))
    recent = [Change("a#2:status", "status", "just now", progress=True), Change("a#2:x", "x", "nope")]
    items = state.progress_today(state.load_log(tmp_path), recent, NOW)
    assert [i["text"] for i in items] == ["just now", "morning win"]


def test_write_log_is_valid_jsonl(tmp_path):
    state.write_log(tmp_path, [{"ts": NOW.isoformat(), "id": "x", "kind": "k", "text": "t"}])
    assert json.loads((tmp_path / "events.jsonl").read_text().strip())["id"] == "x"
