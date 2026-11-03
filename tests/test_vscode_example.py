import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TASKS = json.loads((ROOT / "examples" / "vscode" / "tasks.json").read_text())


def test_tasks_are_valid_run_the_real_command_and_one_runs_on_folder_open():
    tasks = {t["label"]: t for t in TASKS["tasks"]}
    assert set(tasks) == {"Work briefing", "Work briefing (refresh now)"}
    assert all(t["command"].startswith("work-briefing && code -r ") and "latest.md" in t["command"] for t in tasks.values())
    assert tasks["Work briefing"]["runOptions"] == {"runOn": "folderOpen"} and "runOptions" not in tasks["Work briefing (refresh now)"]


def test_the_opened_file_is_where_the_tool_writes_it():
    from work_briefing.config import home_dir
    assert home_dir().parts[-3:] == (".local", "share", "work-progress-briefing")  # the default the task falls back to
    assert ".local/share/work-progress-briefing" in TASKS["tasks"][0]["command"]
    assert "WPB_HOME" in TASKS["tasks"][0]["command"]


def test_readme_and_article_point_at_the_example():
    assert "examples/vscode/tasks.json" in (ROOT / "README.md").read_text()
    assert "examples/vscode/tasks.json" in (ROOT / "docs" / "article" / "article.md").read_text()
