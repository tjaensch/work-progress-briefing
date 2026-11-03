"""The example agent skill must only mention commands and JSON keys that really exist."""
import re
from pathlib import Path

from conftest import NOW
from work_briefing import cli
from work_briefing.models import Briefing
from work_briefing.render import to_json

SKILL = Path(__file__).resolve().parent.parent / "examples" / "copilot-skill" / "SKILL.md"
TEXT = SKILL.read_text()


def test_frontmatter_names_the_skill_after_its_folder_slash_command():
    m = re.match(r"---\nname: ([\w-]+)\ndescription: (.+)\n---\n", TEXT)
    assert m and m.group(1) == "work-progress-briefing" and "/work-progress-briefing" in m.group(2)


def test_every_command_it_mentions_exists():
    parser_commands = {"run", "report", "snooze", "dismiss", "unsnooze", "snoozes", "apply", "demo"}
    used = set(re.findall(r"work-briefing (?:report |)(\w+)", TEXT))
    assert used - {"standup", "eod", "week"} <= parser_commands
    for line in re.findall(r"`(work-briefing [^`]+)`", TEXT):
        args = [a for a in line.split()[1:] if not a.startswith(("[", "<")) and a.isupper() is False]
        cmd = args[0] if args else "run"
        if cmd in {"report", "snooze", "dismiss", "unsnooze", "snoozes", "apply"}:
            assert cmd in parser_commands


def test_every_json_key_it_names_exists_in_the_output():
    keys = to_json(Briefing(now=NOW))
    for name in ("delta", "progress_today", "plan", "incoming", "health", "drafts", "snooze_fingerprints", "items"):
        assert name in TEXT and name in keys
    assert {"events"} <= set(keys["delta"])


def test_it_never_allows_posting_without_a_yes_and_the_apply_flag_exists():
    assert "Never post" in TEXT and "explicit yes" in TEXT and "apply <draft id> --yes" in TEXT
    assert cli.parse_args(["apply", "x#1:nudge", "--yes"]).yes is True


def test_it_is_included_in_the_readme():
    assert "examples/copilot-skill/SKILL.md" in (SKILL.parents[2] / "README.md").read_text()
