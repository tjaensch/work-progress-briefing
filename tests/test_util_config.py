import json
from datetime import timedelta, timezone

import pytest

from conftest import NOW, FakeRunner, cp
from work_briefing import cli
from work_briefing.config import Config, home_dir, load_config
from work_briefing.util import ago, atomic_write, clip, parse_ts, redact, run_cmd, since_text


def test_parse_ts_handles_z_offsets_and_naive():
    assert parse_ts("2026-11-02T12:00:00Z") == NOW
    assert parse_ts("2026-11-02T13:00:00+0100") == NOW
    assert parse_ts("2026-11-02T13:00:00+01:00") == NOW
    assert parse_ts("2026-11-02T12:00:00").tzinfo == timezone.utc


def test_ago_and_since_text():
    assert ago(NOW, NOW) == "today" and ago(NOW - timedelta(days=3, hours=2), NOW) == "3d ago"
    assert since_text(NOW - timedelta(seconds=20), NOW) == "just now"
    assert since_text(NOW - timedelta(minutes=5), NOW) == "5 min ago"
    assert since_text(NOW - timedelta(hours=3), NOW) == "3h ago"
    assert since_text(NOW - timedelta(days=2), NOW) == "2d ago"


def test_clip_breaks_at_words_and_adds_an_ellipsis():
    assert clip("short", 20) == "short"
    out = clip("alpha beta gamma delta", 14)
    assert out == "alpha beta…" and len(out) <= 14
    assert clip("x" * 50, 10) == "x" * 9 + "…"


def test_atomic_write_replaces_and_leaves_no_temp_files(tmp_path):
    p = tmp_path / "sub" / "f.txt"
    atomic_write(p, "one")
    atomic_write(p, "two")
    assert p.read_text() == "two" and [x.name for x in p.parent.iterdir()] == ["f.txt"]


def test_redact_removes_common_secret_shapes():
    text = "ghp_" + "a" * 30 + " and password=hunter2 and Bearer " + "b" * 20 + " ok"
    out = redact(text)
    assert "ghp_" not in out and "hunter2" not in out and "bbbb" not in out and out.endswith(" ok")


def test_run_cmd_turns_missing_programs_and_timeouts_into_results():
    assert run_cmd(["definitely-not-a-program-xyz"]).returncode == 127
    assert run_cmd(["sleep", "5"], timeout=0.2).returncode == 124
    assert run_cmd(["echo", "hi"]).stdout.strip() == "hi"


def test_fake_runner_longest_prefix_wins_and_unknown_commands_fail():
    r = FakeRunner({"gh": cp("short"), "gh pr view": cp("long")})
    assert r(["gh", "pr", "view", "1"]).stdout == "long" and r(["gh", "x"]).stdout == "short"
    assert r(["other"]).returncode == 1


def test_config_defaults_when_missing(tmp_path):
    assert load_config(tmp_path / "none.json") == Config()


def test_config_loads_values_and_rejects_typos_and_bad_values(tmp_path):
    p = tmp_path / "c.json"
    p.write_text(json.dumps({"plan_size": 3, "repos": ["acme-labs/widget-api"]}))
    c = load_config(p)
    assert c.plan_size == 3 and c.repos == ["acme-labs/widget-api"]
    for bad, msg in (({"plan_sise": 3}, "unknown key"), ({"plan_size": 0}, "plan_size"), ({"plan_size": True}, "plan_size"),
                     ({"repos": ["noslash"]}, "repos"), ({"mismatch_drafts": "yes"}, "mismatch_drafts"),
                     ({"mismatch_transitions": {"bogus": "done"}}, "mismatch_transitions"),
                     ({"mismatch_transitions": {"open_todo": "nonsense"}}, "mismatch_transitions")):
        p.write_text(json.dumps(bad))
        with pytest.raises(ValueError, match=msg):
            load_config(p)
    p.write_text(json.dumps({"mismatch_transitions": {"open_todo": "in_progress"}}))
    assert load_config(p).mismatch_transitions == {"open_todo": "in_progress"}


def test_home_dir_prefers_the_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("WPB_HOME", str(tmp_path))
    assert home_dir() == tmp_path
    monkeypatch.delenv("WPB_HOME")
    assert home_dir().name == "work-progress-briefing"


def test_cli_reports_a_bad_config_with_exit_code_2(tmp_path, capsys):
    (tmp_path / "config.json").write_text("{bad json")
    assert cli.main(["--home", str(tmp_path)]) == 2 and "error" in capsys.readouterr().err
    (tmp_path / "config.json").write_text(json.dumps({"plan_size": 0}))
    assert cli.main(["--home", str(tmp_path)]) == 2
