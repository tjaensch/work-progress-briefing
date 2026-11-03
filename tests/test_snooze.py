from datetime import date, datetime, timedelta, timezone

import pytest

from conftest import NOW, make_comment, make_item, make_pr
from work_briefing import snooze
from work_briefing.models import Change, Draft, Incoming

TODAY = date(2026, 11, 2)  # a Monday


def ctx(entries=None, today=TODAY, enabled=True):
    return snooze.Context(snooze.Snoozes(entries or {}), today, enabled)


def entry(mode="until_date", until="2026-11-10", fp=None, reason="", created="2026-11-01T09:00:00+00:00"):
    return snooze.Entry(mode, until, reason, created, fp)


# --- fingerprints ---------------------------------------------------------------------------------------------------


def test_item_fingerprint_changes_for_others_comments_status_prs_and_blockers_but_not_mine():
    base = snooze.item_fp(make_item())
    assert snooze.item_fp(make_item(comments=[make_comment("bob", 1)])) != base
    assert snooze.item_fp(make_item(status="in_review")) != base
    assert snooze.item_fp(make_item(prs=[make_pr()])) != base
    assert snooze.item_fp(make_item(blockers=["a#9"])) != base
    assert snooze.item_fp(make_item(comments=[make_comment("me", 1, mine=True)])) == base
    assert snooze.item_fp(make_item(prs=[make_pr(error="boom")])) == base
    assert snooze.item_fp(make_item(prs=[make_pr()])) != snooze.item_fp(make_item(prs=[make_pr(ci_fail=1)]))


def test_mismatch_and_incoming_fingerprints():
    a = make_item(status="blocked", prs=[make_pr("MERGED")], mismatches=["m"])
    assert snooze.mismatch_fp(a) == snooze.mismatch_fp(make_item(status="blocked", prs=[make_pr("MERGED")], mismatches=["m"]))
    assert snooze.mismatch_fp(a) != snooze.mismatch_fp(make_item(status="todo", prs=[make_pr("MERGED")], mismatches=["m"]))
    x = Incoming("review_request", "u", "T", when=NOW)
    assert snooze.incoming_fp(x) != snooze.incoming_fp(Incoming("review_request", "u", "T", when=NOW + timedelta(hours=1)))


def test_ids():
    assert snooze.base_id("a/b#1:nudge") == "a/b#1" and snooze.base_id("a/b#1") == "a/b#1"
    assert snooze.short_id("u") == snooze.short_id("u") != snooze.short_id("v") and snooze.short_id("u").startswith("in-")


# --- when is a snooze active ----------------------------------------------------------------------------------------


def test_until_date_hides_until_the_day_it_comes_back():
    s = snooze.Snoozes({"a#1": entry(until="2026-11-04")})
    assert s.active("a#1", "x", date(2026, 11, 3)) and not s.active("a#1", "x", date(2026, 11, 4))
    assert not s.active("a#2", "x", TODAY)


def test_until_change_hides_while_the_fingerprint_matches_and_a_date_caps_it():
    s = snooze.Snoozes({"a#1": entry("until_change", None, "fp1")})
    assert s.active("a#1", "fp1", TODAY) and not s.active("a#1", "fp2", TODAY) and not s.active("a#1", None, TODAY)
    capped = snooze.Snoozes({"a#1": entry("until_change", "2026-11-03", "fp1")})
    assert capped.active("a#1", "fp1", TODAY) and not capped.active("a#1", "fp1", date(2026, 11, 3))


# --- applying during a run ------------------------------------------------------------------------------------------


def test_split_items_hides_snoozed_and_records_fingerprints_for_every_id():
    a, b = make_item("a#1"), make_item("a#2")
    c = ctx({"a#1": entry()})
    visible, hidden = snooze.split_items(c, [a, b])
    assert visible == [b] and hidden == [a]
    assert set(c.fps) == {"a#1", "a#2"} and c.shown[0]["id"] == "a#1" and c.shown[0]["until"] == "until 2026-11-10"


def test_disabled_context_hides_nothing_but_still_records_fingerprints():
    c = ctx({"a#1": entry()}, enabled=False)
    assert snooze.split_items(c, [make_item("a#1")])[1] == [] and "a#1" in c.fps and c.shown == []


def test_a_changed_item_comes_back_and_its_snooze_is_pruned():
    item = make_item("a#1")
    c = ctx({"a#1": entry("until_change", None, "stale-fingerprint")})
    assert snooze.split_items(c, [item])[0] == [item]
    assert c.prune({"a#1"}) is True and c.snoozes.entries == {}


def test_prune_drops_expired_orphaned_and_ancient_open_ended_entries_and_keeps_live_ones():
    old = (datetime(2026, 11, 2) - timedelta(days=100)).isoformat()
    entries = {"a#1": entry(until="2026-11-01"), "a#2": entry(), "a#3": entry(), "in-abc": entry(),
               "a#4": entry("until_change", None, "f", created=old), "a#2:mismatch": entry()}
    c = ctx(entries)
    assert c.prune({"a#2", "a#4"}) is True
    assert set(c.snoozes.entries) == {"a#2", "in-abc", "a#2:mismatch"}
    assert ctx({"a#2": entry()}).prune({"a#2"}) is False


def test_snoozed_mismatch_loses_the_warning_but_the_item_stays():
    i = make_item(status="blocked", prs=[make_pr("MERGED")], mismatches=["PR merged but item is still blocked"],
                  mismatch_kinds=["merged_open"], reasons=["Check: PR merged but item is still blocked", "other"])
    c = ctx({"a/b#1:mismatch": entry()})
    snooze.apply_mismatches(c, [make_item("a/b#1")])  # nothing to do for an item without a mismatch
    i.id = "a/b#1"
    snooze.apply_mismatches(c, [i])
    assert (i.mismatches, i.mismatch_kinds, i.reasons) == ([], [], ["other"]) and c.shown[0]["kind"] == "mismatch"


def test_unsnoozed_mismatch_is_untouched():
    i = make_item(mismatches=["m"], mismatch_kinds=["merged_open"], reasons=["Check: m"])
    snooze.apply_mismatches(ctx(), [i])
    assert i.mismatches == ["m"] and i.reasons == ["Check: m"]


def test_draft_snoozes_use_the_items_fingerprint():
    item = make_item("a#1")
    d = Draft("a#1:nudge", "a#1", "nudge", "Nudge", "text", None, "in_progress", "x")
    assert snooze.filter_drafts(ctx({"a#1:nudge": entry("until_change", None, snooze.item_fp(item))}), [d], {"a#1": item}) == []
    assert snooze.filter_drafts(ctx({"a#1:nudge": entry("until_change", None, "other")}), [d], {"a#1": item}) == [d]
    assert snooze.filter_drafts(ctx(), [d], {}) == [d]  # a draft for an unknown item still goes through


def test_changes_on_hidden_things_lose_attention_but_stay_listed():
    url = "https://example.test/x/pull/1"
    changes = [Change("a#1:comments", "comments", "c", key="a#1", attention=True),
               Change(f"incoming:{url}", "incoming", "i", attention=True),
               Change("a#2:comments", "comments", "d", key="a#2", attention=True),
               Change(f"cleared:{url}", "cleared", "e", progress=True)]
    out = snooze.quiet_changes(changes, {"a#1"}, {snooze.short_id(url)})
    assert [c.attention for c in out] == [False, False, True, False] and [c.id for c in out] == [c.id for c in changes]
    assert out[3].progress is True


# --- storage --------------------------------------------------------------------------------------------------------


def test_save_and_load_roundtrip_and_tolerate_damage(tmp_path):
    s = snooze.Snoozes({"a#1": entry(reason="because")})
    s.save(tmp_path)
    assert snooze.Snoozes.load(tmp_path).entries["a#1"].reason == "because"
    snooze.Snoozes.path(tmp_path).write_text("{broken")
    assert snooze.Snoozes.load(tmp_path).entries == {}
    snooze.Snoozes.path(tmp_path).write_text('{"a#1": {"unexpected": 1}}')
    assert snooze.Snoozes.load(tmp_path).entries == {}


# --- command helpers ------------------------------------------------------------------------------------------------


def test_parse_until_accepts_dates_tomorrow_and_weekdays():
    assert snooze.parse_until("2026-11-10", TODAY) == date(2026, 11, 10)
    assert snooze.parse_until("Tomorrow", TODAY) == date(2026, 11, 3)
    assert snooze.parse_until("friday", TODAY) == date(2026, 11, 6)
    assert snooze.parse_until("monday", TODAY) == date(2026, 11, 9)  # never today
    for bad in ("someday", "2026-11-02", "2026-01-01"):
        with pytest.raises(ValueError):
            snooze.parse_until(bad, TODAY)


def test_make_entry_stores_the_fingerprint_only_for_until_change():
    now = datetime(2026, 11, 2, 9, 0, tzinfo=timezone.utc)
    assert snooze.make_entry("until_date", date(2026, 11, 5), "r", "fp", now).fingerprint is None
    e = snooze.make_entry("until_change", None, "r", "fp", now)
    assert e.fingerprint == "fp" and e.until is None and e.created == now.isoformat()
