"""Stand-up, end-of-day and weekly reports from the event log and the latest briefing.

Pure functions: no network, no model, and the same input gives the same text. They say what the briefing saw change,
never hours and never detail it did not observe. `eod` also builds comment drafts (posted only through `--apply`)."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path

from .models import Draft
from .state import item_of
from .util import ago, parse_ts

KINDS = ("standup", "eod", "week")
# Log kinds that mean "something happened on an item", plus anything flagged as progress. New comments from others, CI
# and bucket moves are not activity of mine.
ACTIVITY_KINDS = {"status", "pr_new", "pr_state", "my_comments", "left", "blocker", "cleared"}
WEEK_DAYS = 5
REVIEW_TEXT = {"review_required": "awaiting review", "changes_requested": "changes requested",
               "changes_addressed": "changes addressed, awaiting re-review", "approved": "approved", "none": "no review yet"}


@dataclass(frozen=True)
class Event:
    when: datetime  # in the caller's time zone
    key: str  # "" when the event is not about one item
    kind: str
    text: str
    progress: bool


# --- calendar -------------------------------------------------------------------------------------------------------


def previous_working_day(today: date) -> date:
    d = today - timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def last_working_days(today: date, n: int = WEEK_DAYS) -> list[date]:
    """The last n weekdays up to and including today (up to Friday on a weekend), oldest first."""
    d, days = today, []
    while len(days) < n:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
    return days[::-1]


def _start_of(d: date, now: datetime) -> datetime:
    return datetime.combine(d, time.min, tzinfo=now.tzinfo)


def _until(now: datetime) -> datetime:
    return now + timedelta(seconds=1)  # "so far", including events logged this very second


# --- inputs ---------------------------------------------------------------------------------------------------------


def activity(log: list[dict], start: datetime, end: datetime) -> list[Event]:
    """Activity rows with start <= time < end, in a stable order."""
    events = []
    for e in log:
        when = e["_ts"].astimezone(start.tzinfo)
        kind, progress = e.get("kind", ""), bool(e.get("progress"))
        if start <= when < end and (progress or kind in ACTIVITY_KINDS):
            events.append(Event(when, e.get("key") or item_of(e.get("id", "")), kind, e.get("text", ""), progress))
    return sorted(events, key=lambda x: (x.when, x.key, x.kind, x.text))


def load_latest(out_dir: Path) -> dict | None:
    try:
        data = json.loads((out_dir / "latest.json").read_text())
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _items(latest: dict | None) -> dict[str, dict]:
    return {i["id"]: i for i in (latest or {}).get("items") or [] if "id" in i}


def _freshness_note(latest: dict | None, now: datetime) -> list[str]:
    if latest is None:
        return ["> No briefing found yet, so the plan and item details are missing. Run a refresh first.", ""]
    try:
        made = parse_ts(latest["generated"]).astimezone(now.tzinfo)
    except (KeyError, ValueError, AttributeError):
        return []
    if made.date() != now.date():
        return [f"> The briefing is from {made:%Y-%m-%d %H:%M}, not today. Refresh it for current status.", ""]
    return []


# --- rendering helpers ----------------------------------------------------------------------------------------------


def _grouped(events: list[Event], items: dict[str, dict], stamp: str) -> list[str]:
    """Bullets grouped by item (in order of first activity), then events that belong to no item."""
    by_key: dict[str, list[Event]] = {}
    for e in events:
        by_key.setdefault(e.key, []).append(e)
    lines = []
    for key, rows in by_key.items():
        if not key:
            continue
        i = items.get(key)
        lines.append(f"- **{key}**" + (f" {i['title']} ({i['status']})" if i else ""))
        lines += [f"  - {r.when:{stamp}}: {r.text}" for r in rows]
    if "" in by_key:
        lines.append("- Not tied to an item")
        lines += [f"  - {r.when:{stamp}}: {r.text}" for r in by_key[""]]
    return lines


def _plan_lines(latest: dict | None, limit: int = 5) -> list[str]:
    plan = (latest or {}).get("plan") or []
    if not plan:
        return ["- (no plan in the latest briefing)"]
    return [f"{p['rank']}. **{p['ref']}** {p['title']}" + (f"  \n   Next: {p['next']}" if p.get("next") else "") for p in plan[:limit]]


def _waiting_lines(items: dict[str, dict]) -> list[str]:
    out = []
    for i in items.values():
        if i.get("bucket") in ("blocked", "waiting"):
            why = (i.get("reasons") or [""])[0]
            out.append(f"- **{i['id']}** {i['title']} ({i['status']}): {i.get('action', '')}" + (f". {why}" if why else ""))
    return out or ["- Nothing blocked or waiting."]


# --- stand-up -------------------------------------------------------------------------------------------------------


def standup(log: list[dict], latest: dict | None, now: datetime) -> str:
    today = now.date()
    prev = previous_working_day(today)
    label = "Yesterday" if prev == today - timedelta(days=1) else f"Last working day ({prev:%A})"
    items = _items(latest)
    before = activity(log, _start_of(prev, now), _start_of(today, now))
    so_far = activity(log, _start_of(today, now), _until(now))
    L = [f"# Stand-up {today:%Y-%m-%d (%a)}", ""] + _freshness_note(latest, now)
    L += [f"## {label} ({prev:%a %Y-%m-%d})"]
    L += _grouped(before, items, "%a %H:%M") if before else ["- Nothing logged."]
    if so_far:
        L += ["", "## Already today"] + _grouped(so_far, items, "%H:%M")
    L += ["", "## Today (plan)"] + _plan_lines(latest)
    L += ["", "## Blocked or waiting"] + _waiting_lines(items)
    return "\n".join(L) + "\n"


# --- week -----------------------------------------------------------------------------------------------------------


def week(log: list[dict], latest: dict | None, now: datetime) -> str:
    days = last_working_days(now.date())
    start = _start_of(days[0], now)
    events = activity(log, start, _until(now))
    items = _items(latest)
    touched = {e.key for e in events if e.key}
    merged = {e.text for e in events if e.kind == "pr_state" and e.progress}
    advanced = {e.key for e in events if e.kind == "status" and e.progress and e.key}
    closed = {e.key for e in events if e.key and (e.kind == "left" or (
        e.kind == "status" and e.text.rsplit(" -> ", 1)[-1] == "done"))}
    comments = sum(int(m.group(1)) if (m := re.search(r"posted (\d+) comment", e.text)) else 1
                   for e in events if e.kind == "my_comments")
    L = [f"# Week {days[0]:%a %Y-%m-%d} to {days[-1]:%a %Y-%m-%d}", ""] + _freshness_note(latest, now)
    L += [f"- {len(touched)} item(s) with activity, {len(advanced)} advanced, {len(closed)} closed out (done, or left my list)",
          f"- {len(merged)} PR(s) merged, {comments} comment(s) posted by me",
          "- No hours: this lists what the briefing saw change, as a prompt for the timesheet.", ""]
    first = min((e["_ts"].astimezone(now.tzinfo) for e in log), default=None)
    if first is None:
        L += ["> The event log is empty, so nothing can be listed yet.", ""]
    elif first > start:
        L += [f"> The event log starts {first:%a %Y-%m-%d}; earlier days in this window are not included.", ""]
    L += ["## By item"]
    L += _grouped(events, items, "%a %H:%M") if events else ["- Nothing logged in this period."]
    return "\n".join(L) + "\n"


# --- end of day -----------------------------------------------------------------------------------------------------


def _next_step(item: dict, latest: dict) -> str:
    plan = {p["ref"]: p.get("next") for p in latest.get("plan") or []}
    return (plan.get(item["id"]) or item.get("action") or "").strip()


def eod_draft(item: dict, latest: dict, now: datetime) -> Draft:
    status = item["status"].replace("_", " ")
    parts = [f"End-of-day update: still {status}."]
    for p in item.get("prs") or []:
        if p.get("state") == "OPEN" and p.get("url"):
            ci = ", CI failing" if p.get("ci_fail") else ""
            parts.append(f"PR {p['url']} is open ({REVIEW_TEXT.get(p.get('review_state'), 'no review yet')}{ci}).")
    if (step := _next_step(item, latest)):
        parts.append(f"Next: {step.rstrip('.')}.")
    return Draft(id=f"{item['id']}:eod_update", key=item["id"], kind="eod_update", title=f"End-of-day update on {item['id']}",
                 comment=" ".join(parts), transition=None, status_at_draft=item["status"], created=now.isoformat())


def eod_candidates(latest: dict | None, now: datetime) -> list[dict]:
    """In-progress items whose record was not touched today."""
    out = []
    for i in (latest or {}).get("items") or []:
        try:
            touched_today = parse_ts(i["updated"]).astimezone(now.tzinfo).date() >= now.date()
        except (KeyError, ValueError, AttributeError):
            continue
        if i.get("status") == "in_progress" and not touched_today:
            out.append(i)
    return out


def eod(log: list[dict], latest: dict | None, now: datetime) -> tuple[str, list[Draft]]:
    items = _items(latest)
    done = activity(log, _start_of(now.date(), now), _until(now))
    drafts = [eod_draft(i, latest, now) for i in eod_candidates(latest, now)]
    L = [f"# End of day {now:%Y-%m-%d (%a)}", ""] + _freshness_note(latest, now)
    L += ["## Progress today"] + (_grouped(done, items, "%H:%M") if done else ["- Nothing logged today."])
    L += ["", "## In progress with no update today"]
    if not drafts:
        L += ["- None: every in-progress item was updated today."]
    for d in drafts:
        i = items[d.key]
        L += [f"- **{d.key}** {i['title']}: last updated {ago(parse_ts(i['updated']), now)}",
              f"  - Suggested comment: {d.comment}",
              f"  - Post it (asks first): `work-briefing --apply {d.id}`"]
    return "\n".join(L) + "\n", drafts


def build_report(kind: str, log: list[dict], latest: dict | None, now: datetime) -> tuple[str, list[Draft]]:
    if kind == "standup":
        return standup(log, latest, now), []
    if kind == "week":
        return week(log, latest, now), []
    if kind == "eod":
        return eod(log, latest, now)
    raise ValueError(f"unknown report {kind!r}; choose from {', '.join(KINDS)}")
