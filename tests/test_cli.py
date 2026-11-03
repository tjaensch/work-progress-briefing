import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from conftest import NOW, FakeRunner, cp
from test_actions import FakeTracker
from work_briefing import actions, cli, snooze, state, status
from work_briefing.demo import API, DEMO_NOW, DOCS, is_demo_home
from work_briefing.models import Draft
from work_briefing.outputs import prune_old, write_outputs

I = lambda n: f"{API}#{n}"  # noqa: E731


@pytest.fixture
def home(tmp_path, capsys):
    """A demo home that has been through the whole story (all output captured and discarded)."""
    assert cli.main(["--home", str(tmp_path), "demo"]) == 0
    capsys.readouterr()
    return tmp_path / "demo"


def run(*args, capsys):
    rc = cli.main(list(args))
    out = capsys.readouterr()
    return rc, out.out, out.err


# --- arguments ------------------------------------------------------------------------------------------------------


def test_plain_invocation_means_run_and_keeps_global_options():
    a = cli.parse_args(["--home", "/x"])
    assert (a.command, a.home, a.full, a.keep_days) == ("run", "/x", False, 14)
    assert cli.parse_args([]).command == "run" and cli.parse_args(["run", "--full"]).full is True


def test_snooze_options_are_mutually_exclusive():
    with pytest.raises(SystemExit):
        cli.parse_args(["snooze", "x", "--days", "2", "--until", "tomorrow"])
    with pytest.raises(SystemExit):
        cli.parse_args(["report", "monthly"])


def test_bad_config_is_exit_code_2(tmp_path, capsys):
    (tmp_path / "config.json").write_text(json.dumps({"plan_size": 0}))
    rc, _, err = run("--home", str(tmp_path), capsys=capsys)
    assert rc == 2 and "plan_size" in err


# --- run ------------------------------------------------------------------------------------------------------------


def test_demo_writes_briefing_files_drafts_and_status(home):
    out = home / "out"
    names = {p.name for p in out.iterdir()}
    assert {"latest.md", "latest.json", "briefing-2026-03-11.md", "briefing-2026-03-11.json", "drafts-2026-03-11.json", "status.json"} <= names
    assert json.loads((out / "status.json").read_text())["consecutive_failures"] == 0
    assert is_demo_home(home) and (home / "state" / "events.jsonl").exists()
    assert {d["id"] for d in json.loads((out / "drafts-2026-03-11.json").read_text())} == {f"{I(18)}:nudge", f"{I(21)}:status_update"}


def test_demo_output_is_deterministic_and_repeatable(tmp_path, capsys):
    cli.main(["--home", str(tmp_path / "a"), "demo"])
    first = capsys.readouterr().out.replace(str(tmp_path / "a"), "<home>")
    cli.main(["--home", str(tmp_path / "b"), "demo"])
    second = capsys.readouterr().out.replace(str(tmp_path / "b"), "<home>")
    assert first == second and "# Progress briefing 2026-03-11 (Wed) 10:30" in first and "# Week Thu 2026-03-05" in first
    cli.main(["--home", str(tmp_path / "a"), "demo"])  # running it again starts from scratch
    assert capsys.readouterr().out.replace(str(tmp_path / "a"), "<home>") == first


def test_run_in_a_demo_home_uses_demo_data_and_never_real_gh(home, capsys, monkeypatch):
    monkeypatch.setattr(cli, "run_cmd", lambda *a, **k: pytest.fail("must not run a real command"))
    rc, out, _ = run("--home", str(home), capsys=capsys)
    assert rc == 0 and "=== Progress briefing 2026-03-11" in out and "Full briefing:" in out


def test_a_failed_refresh_is_recorded_with_a_banner_and_exit_codes(tmp_path, capsys, monkeypatch):
    out = tmp_path / "out"
    out.mkdir()
    (out / "latest.md").write_text("# Old briefing\n")
    runner = FakeRunner({"gh api user": cp("me\n"), "gh search issues": cp("", 1, "HTTP 502")})
    monkeypatch.setattr(cli, "run_cmd", runner)
    rc, _, err = run("--home", str(tmp_path), capsys=capsys)
    assert rc == 1 and "Refresh failed: GhError: HTTP 502" in err
    st = status.load(out)
    assert st["consecutive_failures"] == 1 and "HTTP 502" in st["last_error"]
    assert "last refresh failed" in (out / "latest.md").read_text() and (out / "latest.md").read_text().endswith("# Old briefing\n")
    monkeypatch.setattr(cli, "run_cmd", FakeRunner({"gh api user": cp("", 1, "You are not logged in")}))
    assert run("--home", str(tmp_path), capsys=capsys)[0] == 2  # retrying cannot help


def test_full_prints_the_markdown(tmp_path, capsys):
    cli.main(["--home", str(tmp_path), "demo"])
    capsys.readouterr()
    rc, out, _ = run("--home", str(tmp_path / "demo"), "run", "--full", capsys=capsys)
    assert rc == 0 and out.startswith("# Progress briefing 2026-03-11")


# --- reports --------------------------------------------------------------------------------------------------------


def test_report_command_prints_saves_and_needs_no_network(home, capsys, monkeypatch):
    monkeypatch.setattr(cli, "run_cmd", lambda *a, **k: pytest.fail("reports never run commands"))
    rc, out, _ = run("--home", str(home), "report", "standup", capsys=capsys)
    assert rc == 0 and out == (home / "out" / "report-standup-2026-03-11.md").read_text() and "# Stand-up 2026-03-11 (Wed)" in out
    rc, out, _ = run("--home", str(home), "report", "week", capsys=capsys)
    assert "## By item" in out and "No hours" in out


def test_eod_report_saves_drafts_apart_and_apply_finds_them(home, capsys):
    rc, out, _ = run("--home", str(home), "report", "eod", capsys=capsys)
    assert rc == 0
    eod = json.loads((home / "out" / "drafts-eod-2026-03-11.json").read_text())
    assert {d["id"] for d in eod} == {f"{I(5)}:eod_update", f"{I(21)}:eod_update"}
    assert all(f"`work-briefing apply {d['id']}`" in out for d in eod)
    run("--home", str(home), capsys=capsys)  # a refresh rewrites drafts-DATE.json but must not lose the end-of-day drafts
    rc, out, _ = run("--home", str(home), "apply", f"{I(21)}:eod_update", "--yes", capsys=capsys)
    assert rc == 0 and "End-of-day update: still in progress." in out and f"(demo) would post to {I(21)}" in out


def test_report_with_nothing_on_disk(tmp_path, capsys):
    rc, out, _ = run("--home", str(tmp_path), "report", "standup", capsys=capsys)
    assert rc == 0 and "No briefing found yet" in out


# --- snooze ---------------------------------------------------------------------------------------------------------


def test_snooze_by_days_then_list_refresh_and_unsnooze(home, capsys):
    rc, out, _ = run("--home", str(home), "snooze", I(5), "--days", "3", "--reason", "parked", capsys=capsys)
    assert rc == 0 and f"Snoozed {I(5)} until Sat 2026-03-14" in out
    _, listing, _ = run("--home", str(home), "snoozes", capsys=capsys)
    assert f"{I(5)}  until Sat 2026-03-14  [active]  reason: parked" in listing
    run("--home", str(home), capsys=capsys)  # refresh
    latest = json.loads((home / "out" / "latest.json").read_text())
    assert I(5) not in {i["id"] for i in latest["items"]} and latest["snoozed"][0]["id"] == I(5)
    rc, out, _ = run("--home", str(home), "unsnooze", I(5), capsys=capsys)
    assert rc == 0 and f"Unsnoozed {I(5)}" in out
    assert run("--home", str(home), "snoozes", capsys=capsys)[1] == "Nothing is snoozed.\n"


def test_snooze_defaults_to_one_day_and_accepts_dates_and_weekdays(home, capsys):
    assert "until Thu 2026-03-12" in run("--home", str(home), "snooze", I(5), capsys=capsys)[1]
    assert "until Fri 2026-03-13" in run("--home", str(home), "snooze", I(9), "--until", "friday", capsys=capsys)[1]
    assert "until Thu 2026-03-12" in run("--home", str(home), "snooze", I(18), "--until", "tomorrow", capsys=capsys)[1]


def test_dismiss_is_until_change_and_stores_the_fingerprint_from_the_briefing(home, capsys):
    rc, out, _ = run("--home", str(home), "dismiss", I(30), capsys=capsys)
    assert rc == 0 and "until it changes" in out
    e = snooze.Snoozes.load(home / "state").entries[I(30)]
    assert e.mode == "until_change" and e.until is None
    assert e.fingerprint == json.loads((home / "out" / "latest.json").read_text())["snooze_fingerprints"][I(30)]


def test_snooze_until_change_with_a_date_cap(home, capsys):
    run("--home", str(home), "snooze", I(30), "--until-change", capsys=capsys)
    assert snooze.Snoozes.load(home / "state").entries[I(30)].mode == "until_change"


def test_snoozing_rejects_unknown_ids_bad_dates_and_a_missing_briefing(home, tmp_path, capsys):
    rc, _, err = run("--home", str(home), "snooze", "nope#1", capsys=capsys)
    assert rc == 1 and "Not snoozable right now: nope#1" in err
    rc, _, err = run("--home", str(home), "snooze", I(5), "--until", "someday", capsys=capsys)
    assert rc == 1 and "cannot read the date" in err
    assert snooze.Snoozes.load(home / "state").entries == {}  # nothing was half-saved
    empty = tmp_path / "empty"
    rc, _, err = run("--home", str(empty), "snooze", I(5), capsys=capsys)
    assert rc == 1 and "run `work-briefing` first" in err


def test_unsnooze_reports_what_was_not_snoozed(home, capsys):
    run("--home", str(home), "snooze", I(5), capsys=capsys)
    rc, out, err = run("--home", str(home), "unsnooze", I(5), I(9), capsys=capsys)
    assert rc == 1 and f"Unsnoozed {I(5)}" in out and f"Not snoozed: {I(9)}" in err


def test_listing_shows_back_state_for_a_changed_dismissal(home, capsys):
    snooze.Snoozes({I(30): snooze.make_entry("until_change", None, "", "old-fingerprint", DEMO_NOW)}).save(home / "state")
    assert "[back (expired, changed or gone)]" in run("--home", str(home), "snoozes", capsys=capsys)[1]


# --- apply ----------------------------------------------------------------------------------------------------------


def test_apply_in_a_demo_home_previews_and_posts_nothing_real(home, capsys, monkeypatch):
    monkeypatch.setattr(cli, "run_cmd", lambda *a, **k: pytest.fail("must not run a real command"))
    rc, out, _ = run("--home", str(home), "apply", f"{I(21)}:status_update", "--yes", capsys=capsys)
    assert rc == 0 and "Status update: PR https://github.com/acme-labs/widget-api/pull/38 has been merged." in out
    assert f"(demo) would post to {I(21)}" in out and "Comment posted." in out


def test_apply_refuses_when_the_demo_status_differs_and_asks_before_posting(home, capsys, monkeypatch):
    stale = Draft(f"{I(21)}:status_update", I(21), "status_update", "t", "text", None, "todo", "x")
    actions.save_drafts(home / "out", [stale], DEMO_NOW)
    rc, out, _ = run("--home", str(home), "apply", f"{I(21)}:status_update", capsys=capsys)
    assert rc == 1 and "Refusing" in out
    monkeypatch.setattr("builtins.input", lambda prompt="": "n")
    rc, out, _ = run("--home", str(home), "apply", f"{I(18)}:nudge", capsys=capsys)
    assert rc == 1 and "nothing was written" in out and "(demo) would post" not in out
    monkeypatch.setattr("builtins.input", lambda prompt="": (_ for _ in ()).throw(EOFError()))
    assert run("--home", str(home), "apply", f"{I(18)}:nudge", capsys=capsys)[0] == 1  # no terminal means cancel


def test_apply_unknown_draft_fails_without_touching_anything(tmp_path, capsys, monkeypatch):
    monkeypatch.setattr(cli, "run_cmd", lambda *a, **k: pytest.fail("must not run a real command"))
    rc, _, err = run("--home", str(tmp_path), "apply", "x#1:nudge", capsys=capsys)
    assert rc == 1 and "No draft 'x#1:nudge' found" in err


def test_apply_refuses_a_snoozed_draft_or_item(home, capsys):
    run("--home", str(home), "snooze", f"{I(18)}:nudge", capsys=capsys)
    rc, _, err = run("--home", str(home), "apply", f"{I(18)}:nudge", "--yes", capsys=capsys)
    assert rc == 1 and f"{I(18)}:nudge is snoozed" in err
    run("--home", str(home), "snooze", I(21), capsys=capsys)
    rc, _, err = run("--home", str(home), "apply", f"{I(21)}:status_update", "--yes", capsys=capsys)
    assert rc == 1 and f"{I(21)} is snoozed" in err


def test_apply_command_uses_the_given_tracker(home):
    tracker = FakeTracker(status="in_progress")
    a = cli.parse_args(["--home", str(home), "apply", f"{I(21)}:status_update", "--yes"])
    assert cli.apply_command(a, tracker=tracker) == 0 and tracker.comments[0][0] == I(21) and tracker.transitions == []


# --- outputs --------------------------------------------------------------------------------------------------------


def test_prune_removes_old_briefings_drafts_and_reports_only(tmp_path):
    names = ("briefing-2026-10-01.md", "briefing-2026-10-01.json", "drafts-2026-10-01.json", "drafts-eod-2026-10-01.json",
             "report-week-2026-10-01.md", "briefing-2026-11-01.md", "drafts-eod-2026-11-01.json", "latest.md", "status.json", "notes.md")
    for n in names:
        (tmp_path / n).write_text("x")
    prune_old(tmp_path, 14, datetime(2026, 11, 2))
    assert {p.name for p in tmp_path.iterdir()} == {"briefing-2026-11-01.md", "drafts-eod-2026-11-01.json", "latest.md", "status.json", "notes.md"}


def test_write_outputs_is_atomic_and_writes_four_files(tmp_path):
    from work_briefing.models import Briefing
    md, js = write_outputs(Briefing(now=NOW), tmp_path)
    assert sorted(p.name for p in tmp_path.iterdir()) == ["briefing-2026-11-02.json", "briefing-2026-11-02.md", "latest.json", "latest.md"]
    assert json.loads(js.read_text())["generated"] == NOW.isoformat() and md.read_text().startswith("# Progress briefing 2026-11-02")


def test_demo_only_ever_deletes_its_own_folder(tmp_path, capsys):
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "keep.txt").write_text("real state")
    cli.main(["--home", str(tmp_path), "demo"])
    assert (tmp_path / "state" / "keep.txt").read_text() == "real state" and (tmp_path / "demo" / ".demo").exists()
