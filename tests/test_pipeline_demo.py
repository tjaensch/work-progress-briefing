"""End-to-end: the real pipeline and GitHub adapter against the demo story (a fake `gh`, fictional data)."""
import json
from datetime import timedelta

import pytest

from conftest import FakeRunner, cp
from work_briefing import actions, github_source as gh, pipeline, snooze, state
from work_briefing.config import Config
from work_briefing.demo import API, DEMO_NOW, DOCS, ME, STAGES, DemoRunner, Scenario
from work_briefing.render import render_compact, render_markdown, to_json

I = lambda n: f"{API}#{n}"  # noqa: E731


def deps(tmp_path, stage=3, config=None, runner=None):
    return pipeline.Deps(runner=runner or DemoRunner(stage), now=STAGES[stage], state_dir=tmp_path / "state",
                         config=config or Config(me=ME), me=ME)


def play(tmp_path, upto=3, **kw):
    b = None
    for s in range(upto + 1):
        b = pipeline.run(pipeline.Options(**kw), deps(tmp_path, s))
    return b


def by_id(b):
    return {i.id: i for i in b.items}


def test_first_run_has_nothing_to_compare_with(tmp_path):
    b = pipeline.run(pipeline.Options(), deps(tmp_path, 0))
    assert b.last_check is None and b.changes == [] and b.progress == []
    assert len(state.load_history(tmp_path / "state")) == 1


def test_the_final_stage_ranks_the_plan_and_buckets_every_item(tmp_path):
    b = play(tmp_path)
    items = by_id(b)
    assert {k: v.bucket for k, v in items.items()} == {
        I(12): "ready", I(15): "ready", I(21): "ready", I(30): "ready", I(5): "stale", I(9): "blocked", I(18): "waiting",
        DOCS + "#7": "start", DOCS + "#3": "backlog"}
    assert items[I(12)].quick and items[I(12)].action == "Approved: merge" and b.plan[0]["ref"] == I(12)
    assert items[I(15)].action == "Fix failing CI on PR" and items[I(30)].action == "Respond to new comments, then continue"
    assert [p["ref"] for p in b.plan] == [I(12), I(30), I(15), I(21), "https://github.com/acme-labs/docs-site/pull/51"]
    assert [p["score"] for p in b.plan] == [60, 40, 28, 28, 12]  # ties broken by id, so the order is stable


def test_review_request_is_waiting_on_me_and_competes_for_the_plan(tmp_path):
    b = play(tmp_path)
    [x] = b.incoming
    assert x.kind == "review_request" and x.title == f"{DOCS}: Fix broken links in the API guide" and x.detail == "by kim-docs"
    assert x.score == 12 and [p["kind"] for p in b.plan][-1] == "review_request" and b.plan[-1]["next"] == "Review it"


def test_changes_since_the_last_check_are_tagged_and_ordered(tmp_path):
    b = play(tmp_path)
    fresh = {c.text: c for c in b.changes if c.is_new}
    assert fresh[f"{I(12)}: PR widget-api#40 review changes_requested -> approved"].attention
    assert fresh[f"{I(30)} has 1 new comment(s), latest by kim-docs"].attention
    assert fresh[f"{I(15)}: PR widget-api#44 CI started failing"].attention
    assert fresh[f"New waiting on you: {DOCS}: Fix broken links in the API guide"].attention
    assert fresh[f"{I(21)}: PR widget-api#38 OPEN -> MERGED"].progress
    assert not any("review review_required -> none" in t for t in fresh)  # a merged PR's review state is not news


def test_progress_today_and_event_log_feed_the_week(tmp_path):
    play(tmp_path)
    log = state.load_log(tmp_path / "state")
    kinds = {(e["key"], e["kind"]) for e in log if e.get("progress")}
    assert (I(2), "left") in kinds and (I(12), "my_comments") in kinds and (I(21), "pr_state") in kinds
    assert all("key" in e for e in log)


def test_drafts_are_a_nudge_and_a_status_update_but_no_close_out(tmp_path):
    b = play(tmp_path)
    got = {d.id: d for d in b.drafts}
    assert set(got) == {f"{I(18)}:nudge", f"{I(21)}:status_update"}
    assert got[f"{I(18)}:nudge"].comment.startswith("Hi @kim-docs, checking in")
    assert got[f"{I(21)}:status_update"].transition is None and "has been merged" in got[f"{I(21)}:status_update"].comment


def test_the_mismatch_draft_needs_the_issue_to_be_older_than_the_merge(tmp_path):
    play(tmp_path, upto=2)
    stage3 = Scenario(3)
    assert stage3.issues[(API, 21)]["updatedAt"] < stage3.prs[stage3.url(API, 38, "pull")]["mergedAt"]
    stage3.issues[(API, 21)]["updatedAt"] = stage3.ago(0.1)  # someone touched the issue after the merge
    runner = DemoRunner(3)
    runner.scenario = stage3
    b = pipeline.run(pipeline.Options(), deps(tmp_path, 3, runner=runner))
    assert f"{I(21)}:status_update" not in {d.id for d in b.drafts} and by_id(b)[I(21)].mismatches


def test_configured_transition_flows_into_the_draft(tmp_path):
    for s in range(3):
        pipeline.run(pipeline.Options(), deps(tmp_path, s))
    cfg = Config(me=ME, mismatch_transitions={"merged_open": "done"})
    b = pipeline.run(pipeline.Options(), deps(tmp_path, 3, config=cfg))
    d = next(d for d in b.drafts if d.kind == "status_update")
    assert d.transition == "done" and d.comment.endswith("Moving this from in progress to done to match.")


def test_snoozing_hides_an_item_and_quiets_its_changes(tmp_path):
    for s in range(3):
        pipeline.run(pipeline.Options(), deps(tmp_path, s))
    sn = snooze.Snoozes({I(30): snooze.make_entry("until_date", (DEMO_NOW + timedelta(days=2)).date(), "later", None, DEMO_NOW)})
    sn.save(tmp_path / "state")
    b = pipeline.run(pipeline.Options(), deps(tmp_path, 3))
    assert I(30) not in by_id(b) and [s["id"] for s in b.snoozed] == [I(30)] and I(30) in b.fingerprints
    muted = next(c for c in b.changes if c.id == f"{I(30)}:comments")
    assert muted.attention is False
    assert I(30) in state.load_history(tmp_path / "state")[-1][1]["items"]  # hidden is not gone
    shown = pipeline.run(pipeline.Options(no_snooze=True), deps(tmp_path, 3))
    assert I(30) in by_id(shown) and shown.snoozed == []


def test_a_snoozed_mismatch_keeps_the_item_but_drops_the_warning_and_the_draft(tmp_path):
    for s in range(3):
        pipeline.run(pipeline.Options(), deps(tmp_path, s))
    snooze.Snoozes({f"{I(21)}:mismatch": snooze.make_entry("until_date", (DEMO_NOW + timedelta(days=3)).date(), "", None, DEMO_NOW)}).save(tmp_path / "state")
    b = pipeline.run(pipeline.Options(), deps(tmp_path, 3))
    item = by_id(b)[I(21)]
    assert item.mismatches == [] and not any(r.startswith("Check:") for r in item.reasons)
    assert not any(d.kind == "status_update" for d in b.drafts)


def test_snoozed_draft_and_incoming_item_are_hidden(tmp_path):
    for s in range(3):
        pipeline.run(pipeline.Options(), deps(tmp_path, s))
    url = "https://github.com/acme-labs/docs-site/pull/51"
    until = (DEMO_NOW + timedelta(days=3)).date()
    snooze.Snoozes({f"{I(18)}:nudge": snooze.make_entry("until_date", until, "", None, DEMO_NOW),
                    snooze.short_id(url): snooze.make_entry("until_date", until, "", None, DEMO_NOW)}).save(tmp_path / "state")
    b = pipeline.run(pipeline.Options(), deps(tmp_path, 3))
    assert f"{I(18)}:nudge" not in {d.id for d in b.drafts} and b.incoming == []
    assert {s["kind"] for s in b.snoozed} == {"draft", "incoming"}
    assert next(c for c in b.changes if c.id == f"incoming:{url}").attention is False


def test_a_changed_item_wakes_its_until_change_snooze_and_the_entry_is_removed(tmp_path):
    for s in range(3):
        pipeline.run(pipeline.Options(), deps(tmp_path, s))
    snooze.Snoozes({I(30): snooze.make_entry("until_change", None, "", "fingerprint-from-before", DEMO_NOW)}).save(tmp_path / "state")
    b = pipeline.run(pipeline.Options(), deps(tmp_path, 3))
    assert I(30) in by_id(b) and snooze.Snoozes.load(tmp_path / "state").entries == {}


def test_no_state_leaves_no_trace(tmp_path):
    pipeline.run(pipeline.Options(no_state=True), deps(tmp_path, 0))
    assert state.load_history(tmp_path / "state") == [] and state.load_log(tmp_path / "state") == []


def test_unreadable_prs_are_shown_but_the_run_is_not_remembered(tmp_path):
    runner = FakeRunner({"gh pr view": cp("", 1, "boom")})
    inner = DemoRunner(3)

    def both(cmd, timeout=60, cwd=None):
        return runner(cmd) if cmd[1:3] == ["pr", "view"] else inner(cmd)

    b = pipeline.run(pipeline.Options(), deps(tmp_path, 3, runner=both))
    assert any(p.error for i in b.items for p in i.prs) and state.load_history(tmp_path / "state") == []
    assert "PR status unavailable (boom)" in render_markdown(b)


def test_failing_review_request_lookup_is_a_health_note_not_a_failure(tmp_path):
    inner = DemoRunner(3)

    def runner(cmd, timeout=60, cwd=None):
        return cp("", 1, "rate limited") if "--review-requested=@me" in cmd else inner(cmd)

    b = pipeline.run(pipeline.Options(), deps(tmp_path, 3, runner=runner))
    assert ("review requests", "failed", "rate limited") in b.health and b.incoming == []
    assert "FAILED SOURCE: review requests (rate limited)" in render_compact(b, "x.md")


def test_a_failing_search_is_fatal_so_the_caller_can_record_it(tmp_path):
    with pytest.raises(gh.GhError, match="bad credentials"):
        pipeline.run(pipeline.Options(), deps(tmp_path, 3, runner=FakeRunner({"gh search issues": cp("", 1, "bad credentials")})))


def test_the_user_is_asked_of_gh_when_not_configured(tmp_path):
    inner = DemoRunner(3)
    d = pipeline.Deps(runner=inner, now=STAGES[3], state_dir=tmp_path / "state", config=Config())
    pipeline.run(pipeline.Options(), d)
    assert ["gh", "api", "user", "-q", ".login"] in inner.calls


def test_repo_filter_in_config_limits_everything(tmp_path):
    b = pipeline.run(pipeline.Options(), deps(tmp_path, 3, config=Config(me=ME, repos=[DOCS])))
    assert {i.id for i in b.items} == {DOCS + "#7", DOCS + "#3"} and len(b.incoming) == 1


def test_plan_size_is_respected(tmp_path):
    assert len(pipeline.run(pipeline.Options(), deps(tmp_path, 3, config=Config(me=ME, plan_size=2))).plan) == 2


# --- views ----------------------------------------------------------------------------------------------------------


def test_markdown_has_every_section_and_names_the_apply_command(tmp_path):
    md = render_markdown(play(tmp_path))
    for heading in ("## Today's plan", "## Since your last check (18h ago)", "## Progress today", "## Waiting on you", "## Quick wins",
                    "## Ready to continue (4)", "## Ready to start (1)", "## Waiting on others (1)", "## Blocked (1)", "## Stale (1)",
                    "## Backlog (1)", "## Drafts for your approval", "## Sources"):
        assert heading in md, heading
    assert f"`work-briefing apply {I(21)}:status_update`" in md and "(snooze: `acme-labs/widget-api#21:mismatch`)" in md


def test_compact_summary_lists_attention_first_and_drafts(tmp_path):
    out = render_compact(play(tmp_path), "briefing.md")
    assert out.splitlines()[0].startswith("=== Progress briefing 2026-03-11 (Wed) 10:30: 9 items, 1 waiting on you")
    assert "QUICK WINS: " + I(12) in out and "DRAFTS AWAITING APPROVAL (2)" in out and out.endswith("Full briefing: briefing.md")
    changes = [l for l in out.splitlines() if l.startswith("  !")]
    assert len(changes) == 4


def test_json_is_serializable_and_has_what_the_reports_need(tmp_path):
    j = json.loads(json.dumps(to_json(play(tmp_path))))
    assert j["generated"].startswith("2026-03-11T10:30") and j["plan"][0]["rank"] == 1 and j["incoming"][0]["snooze_id"].startswith("in-")
    item = next(i for i in j["items"] if i["id"] == I(21))
    assert item["mismatch_kinds"] == ["merged_open"] and item["prs"][0]["state"] == "MERGED"
    assert I(12) in j["snooze_fingerprints"] and f"{I(21)}:mismatch" in j["snooze_fingerprints"]
    assert {e["new_since_last_run"] for e in j["delta"]["events"]} == {True}
    assert any(d["id"] == f"{I(18)}:nudge" for d in j["drafts"])
