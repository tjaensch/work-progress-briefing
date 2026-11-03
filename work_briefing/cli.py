"""Command line entry point: `work-briefing [run|report|snooze|dismiss|unsnooze|snoozes|apply|demo]`."""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

from . import __version__, actions, github_source as gh, reports, snooze, state, status
from .config import Config, home_dir, load_config
from .outputs import prune_old, write_outputs
from .pipeline import Deps, Options, run
from .render import render_compact, render_markdown
from .util import atomic_write, run_cmd


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="work-briefing", description="Where your work stands right now.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--home", default=str(home_dir()), help="state and output folder (default: $WPB_HOME or ~/.local/share/...)")
    ap.add_argument("--config", default=None, help="config file (default: <home>/config.json)")
    sub = ap.add_subparsers(dest="command")

    r = sub.add_parser("run", help="refresh the briefing and print a summary (the default)")
    r.add_argument("--full", action="store_true", help="print the full markdown instead of the summary")
    r.add_argument("--no-state", action="store_true", help="do not remember this run for the next diff")
    r.add_argument("--no-snooze", action="store_true", help="show everything, ignoring snoozes (they are kept)")
    r.add_argument("--since-last", action="store_true", help="diff against the last run instead of the last day")
    r.add_argument("--keep-days", type=int, default=14, help="delete briefings older than this many days")

    rep = sub.add_parser("report", help="print a report from the event log and the latest briefing (no network)")
    rep.add_argument("kind", choices=reports.KINDS)

    for name, help_ in (("snooze", "hide something for a while"), ("dismiss", "hide something until it changes")):
        s = sub.add_parser(name, help=help_)
        s.add_argument("ids", nargs="+", metavar="ID")
        s.add_argument("--reason", default="")
        if name == "snooze":
            g = s.add_mutually_exclusive_group()
            g.add_argument("--days", type=int, metavar="N", help="it comes back in N days (default 1)")
            g.add_argument("--until", metavar="DATE", help="YYYY-MM-DD, 'tomorrow' or a weekday")
            g.add_argument("--until-change", action="store_true", help="it comes back when it changes")
    u = sub.add_parser("unsnooze", help="bring snoozed things back")
    u.add_argument("ids", nargs="+", metavar="ID")
    sub.add_parser("snoozes", help="list everything that is snoozed")

    ap_ = sub.add_parser("apply", help="post a draft (asks first)")
    ap_.add_argument("draft_id")
    ap_.add_argument("--yes", action="store_true", help="skip the confirmation prompt (use after confirming elsewhere)")

    sub.add_parser("demo", help="run on built-in fictional data, no accounts needed")
    raw = list(argv) if argv is not None else sys.argv[1:]
    a = ap.parse_args(raw)
    return a if a.command else ap.parse_args([*raw, "run"])  # plain `work-briefing` means `run`


def dirs(a: argparse.Namespace) -> tuple[Path, Path]:
    home = Path(a.home)
    return home / "state", home / "out"


def latest_fingerprints(out: Path) -> dict[str, str] | None:
    try:
        return dict(json.loads((out / "latest.json").read_text()).get("snooze_fingerprints") or {})
    except (OSError, ValueError, AttributeError, TypeError):
        return None


def clock(a: argparse.Namespace) -> datetime:
    """The current time; a demo folder lives in the demo story's own "now"."""
    from .demo import DEMO_NOW, is_demo_home

    return DEMO_NOW if is_demo_home(Path(a.home)) else datetime.now().astimezone()


def build_deps(a: argparse.Namespace) -> Deps:
    from .demo import ME, STAGES, DemoRunner, is_demo_home

    if is_demo_home(Path(a.home)):  # a demo folder always refreshes from the demo data, never from GitHub
        return Deps(runner=DemoRunner(len(STAGES) - 1), now=clock(a), state_dir=dirs(a)[0], config=Config(me=ME), me=ME)
    config = load_config(Path(a.config) if a.config else Path(a.home) / "config.json")
    return Deps(runner=run_cmd, now=clock(a), state_dir=dirs(a)[0], config=config)


def run_command(a: argparse.Namespace, deps: Deps) -> int:
    out = dirs(a)[1]
    try:
        b = run(Options(no_state=a.no_state, no_snooze=a.no_snooze, since_last=a.since_last), deps)
    except Exception as e:  # noqa: BLE001 - any failure must leave a visible trace, not just a traceback
        message = status.describe_error(e)
        print(f"Refresh failed: {message}", file=sys.stderr)
        status.record_failure(out, message, deps.now)
        return 2 if status.is_permanent(message) else 1
    md_path, _ = write_outputs(b, out)
    actions.save_drafts(out, b.drafts, b.now)
    prune_old(out, a.keep_days, b.now.replace(tzinfo=None))
    status.record_success(out, b.now)
    print(render_markdown(b) if a.full else render_compact(b, md_path))
    return 0


def report_command(a: argparse.Namespace, now: datetime | None = None) -> int:
    now = now or clock(a)
    state_dir, out = dirs(a)
    text, drafts = reports.build_report(a.kind, state.load_log(state_dir), reports.load_latest(out), now)
    atomic_write(out / f"report-{a.kind}-{now:%Y-%m-%d}.md", text)
    if a.kind == "eod":
        actions.save_drafts(out, drafts, now, prefix="drafts-eod")
    print(text, end="")
    return 0


def snooze_command(a: argparse.Namespace, now: datetime | None = None) -> int:
    state_dir, out = dirs(a)
    now = now or clock(a)
    today = now.date()
    fps = latest_fingerprints(out)
    if fps is None:
        print("No briefing yet: run `work-briefing` first so there is something to snooze.", file=sys.stderr)
        return 1
    unknown = [i for i in a.ids if i not in fps]
    if unknown:
        print(f"Not snoozable right now: {', '.join(unknown)}. Valid ids are in the briefing (see `work-briefing snoozes`).", file=sys.stderr)
        return 1
    until_change = a.command == "dismiss" or getattr(a, "until_change", False)
    try:
        if getattr(a, "until", None):
            until = snooze.parse_until(a.until, today)
        elif until_change:
            until = None
        else:
            days = getattr(a, "days", None) or 1
            until = today + timedelta(days=days)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    mode = "until_change" if until_change and not getattr(a, "until", None) else "until_date"
    snoozes = snooze.Snoozes.load(state_dir)
    for sid in a.ids:
        snoozes.entries[sid] = snooze.make_entry(mode, until, a.reason, fps[sid], now)
        print(f"Snoozed {sid} " + ("until it changes" if until is None else f"until {until:%a %Y-%m-%d}"))
    snoozes.save(state_dir)
    return 0


def unsnooze_command(a: argparse.Namespace) -> int:
    state_dir, _ = dirs(a)
    snoozes = snooze.Snoozes.load(state_dir)
    missing = [i for i in a.ids if i not in snoozes.entries]
    for sid in a.ids:
        if snoozes.entries.pop(sid, None):
            print(f"Unsnoozed {sid}")
    snoozes.save(state_dir)
    if missing:
        print(f"Not snoozed: {', '.join(missing)}", file=sys.stderr)
        return 1
    return 0


def list_snoozes_command(a: argparse.Namespace, today=None) -> int:
    state_dir, out = dirs(a)
    snoozes = snooze.Snoozes.load(state_dir)
    if not snoozes.entries:
        print("Nothing is snoozed.")
        return 0
    from datetime import date

    today = today or clock(a).date()
    fps = latest_fingerprints(out) or {}
    for sid, e in sorted(snoozes.entries.items()):
        phase = "active" if snoozes.active(sid, fps.get(sid), today) else "back (expired, changed or gone)"
        when = "until it changes" if e.until is None else f"until {date.fromisoformat(e.until):%a %Y-%m-%d}" + (
            " or it changes" if e.mode == "until_change" else "")
        print(f"{sid}  {when}  [{phase}]" + (f"  reason: {e.reason}" if e.reason else ""))
    return 0


def apply_command(a: argparse.Namespace, tracker=None, now: datetime | None = None) -> int:
    state_dir, out = dirs(a)
    draft = actions.load_draft(out, a.draft_id)
    if draft is None:
        print(f"No draft '{a.draft_id}' found in {out}. Run the briefing first.", file=sys.stderr)
        return 1
    fps = latest_fingerprints(out) or {}
    snoozes = snooze.Snoozes.load(state_dir)
    today = (now or clock(a)).date()
    for sid in {draft.id, draft.key}:
        if snoozes.active(sid, fps.get(sid), today):
            print(f"Refusing: {sid} is snoozed. Run `work-briefing unsnooze {sid}` first if you want to post this.", file=sys.stderr)
            return 1

    def confirm(prompt: str) -> bool:
        try:
            return input(prompt).strip().lower() in ("y", "yes")
        except EOFError:
            return False

    from .demo import DemoTracker, is_demo_home

    tracker = tracker or (DemoTracker() if is_demo_home(Path(a.home)) else gh.GitHubTracker(run_cmd, ""))
    return 0 if actions.apply_draft(draft, tracker, confirm, assume_yes=a.yes) else 1


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    cmd = a.command or "run"
    try:
        if cmd == "report":
            return report_command(a)
        if cmd in ("snooze", "dismiss"):
            return snooze_command(a)
        if cmd == "unsnooze":
            return unsnooze_command(a)
        if cmd == "snoozes":
            return list_snoozes_command(a)
        if cmd == "apply":
            return apply_command(a)
        if cmd == "demo":
            from .demo import demo_command

            return demo_command(a)
        return run_command(a, build_deps(a))
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
