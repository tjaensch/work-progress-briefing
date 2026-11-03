"""Run-to-run state: snapshots for "what changed", an event log for "progress today"."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from pathlib import Path

from .models import Briefing, Change
from .util import atomic_write, parse_ts

KEEP_SNAPSHOTS = 40
LOG_DAYS = 30
STATUS_RANK = {"todo": 0, "blocked": 0, "in_progress": 1, "in_review": 2, "done": 3}


def item_of(change_id: str) -> str:
    """The item key at the front of a change id ("<item>:<what>"); empty for ids that are not about one item."""
    head = change_id.split(":", 1)[0]
    return "" if head in ("incoming", "cleared") else head


def snapshot(b: Briefing, hidden: list | None = None, hidden_incoming: list | None = None) -> dict:
    """What matters about every item (visible and snoozed alike: hiding an item is not it leaving the list)."""
    items = {}
    for i in [*b.items, *(hidden or [])]:
        others = [c for c in i.comments if not c.mine]
        items[i.id] = {
            "title": i.title, "status": i.status, "bucket": i.bucket,
            "n_others": len(others), "n_mine": len(i.comments) - len(others),
            "last_other_by": others[0].author if others else "",
            "prs": {p.url: {"state": p.state, "review_state": p.review_state, "ci_fail": p.ci_fail}
                    for p in i.prs if not p.error},
            "blockers": sorted(i.blockers),
        }
    waiting = [*b.incoming, *(hidden_incoming or [])]
    return {"ts": b.now.isoformat(), "items": items, "incoming": {x.id: x.title for x in waiting},
            "incoming_when": {x.id: x.when.isoformat() if x.when else "" for x in waiting}}


def status_progress(old: str, new: str) -> bool:
    """Moving forward along todo -> in_progress -> in_review -> done. Moving into blocked is never progress."""
    o, n = STATUS_RANK.get(old), STATUS_RANK.get(new)
    return new != "blocked" and o is not None and n is not None and n > o


def diff_events(old: dict, new: dict, max_age_days: int = 6) -> list[Change]:
    """Differences between two snapshots, each tagged as progress and/or needing attention.

    An incoming item that disappeared counts as "cleared" only if it was recent: one that merely aged out of the
    lookback window was not dealt with."""
    ev: list[Change] = []
    oi, ni = old.get("items", {}), new.get("items", {})
    for key, n in ni.items():
        o = oi.get(key)
        if o is None:
            ev.append(Change(f"{key}:new", "new", f"{key} is new on your list: {n['title']}", attention=True))
            continue
        if o["status"] != n["status"]:
            ev.append(Change(f"{key}:status", "status", f"{key} status {o['status']} -> {n['status']}",
                             progress=status_progress(o["status"], n["status"])))
        elif o["bucket"] != n["bucket"]:
            ev.append(Change(f"{key}:bucket", "bucket", f"{key} moved from {o['bucket']} to {n['bucket']}"))
        added = n["n_others"] - o["n_others"]
        if added > 0:
            ev.append(Change(f"{key}:comments", "comments",
                             f"{key} has {added} new comment(s), latest by {n['last_other_by']}", attention=True))
        mine = n["n_mine"] - o["n_mine"]
        if mine > 0:
            ev.append(Change(f"{key}:my_comments", "my_comments", f"{key}: posted {mine} comment(s)", progress=True))
        for url, p in n["prs"].items():
            op = o["prs"].get(url)
            label = f"PR {url.rsplit('/', 3)[-3]}#{url.rsplit('/', 1)[-1]}"
            pid = f"{key}:{url}"
            if op is None:
                ev.append(Change(f"{pid}:new", "pr_new", f"{key}: new {label} ({p['state']})"))
                continue
            if op["state"] != p["state"]:
                ev.append(Change(f"{pid}:state", "pr_state", f"{key}: {label} {op['state']} -> {p['state']}",
                                 progress=p["state"] == "MERGED"))
            if p["state"] != "OPEN":
                continue  # review and CI of a merged or closed PR are not news
            if op["review_state"] != p["review_state"]:
                ev.append(Change(f"{pid}:review", "pr_review", f"{key}: {label} review {op['review_state']} -> {p['review_state']}",
                                 attention=p["review_state"] in ("approved", "changes_requested")))
            if op["ci_fail"] == 0 < p["ci_fail"]:
                ev.append(Change(f"{pid}:ci", "ci_fail", f"{key}: {label} CI started failing", attention=True))
            elif op["ci_fail"] > 0 == p["ci_fail"]:
                ev.append(Change(f"{pid}:ci", "ci_ok", f"{key}: {label} CI is passing again"))
        for blocker in sorted(set(o["blockers"]) - set(n["blockers"])):
            ev.append(Change(f"{key}:blocker:{blocker}", "blocker", f"{key}: blocker {blocker} cleared",
                             progress=True, attention=True))
    for key, o in oi.items():
        if key not in ni:
            ev.append(Change(f"{key}:left", "left", f"{key} left your list (done or reassigned): {o['title']}", progress=True))
    new_ts = parse_ts(new["ts"]) if new.get("ts") else None
    for iid, title in new.get("incoming", {}).items():
        if iid not in old.get("incoming", {}):
            ev.append(Change(f"incoming:{iid}", "incoming", f"New waiting on you: {title}", attention=True))
    for iid, title in old.get("incoming", {}).items():
        if iid in new.get("incoming", {}):
            continue
        when = (old.get("incoming_when") or {}).get(iid)
        if when and new_ts and parse_ts(when) < new_ts - timedelta(days=max_age_days):
            continue
        ev.append(Change(f"cleared:{iid}", "cleared", f"No longer waiting on you: {title}", progress=True))
    return [replace(c, key=item_of(c.id)) for c in ev]


# --- snapshots on disk ------------------------------------------------------------------------------------------


def load_history(state_dir: Path) -> list[tuple[datetime, dict]]:
    out = []
    for p in sorted((state_dir / "snapshots").glob("snap-*.json")):
        try:
            snap = json.loads(p.read_text())
            out.append((parse_ts(snap["ts"]), snap))
        except (OSError, ValueError, KeyError):
            continue  # a corrupt snapshot must not break the briefing
    return sorted(out, key=lambda x: x[0])


def save_snapshot(state_dir: Path, snap: dict, now: datetime) -> None:
    d = state_dir / "snapshots"
    atomic_write(d / f"snap-{now:%Y%m%d-%H%M%S}.json", json.dumps(snap))
    for old in sorted(d.glob("snap-*.json"))[:-KEEP_SNAPSHOTS]:
        old.unlink()


@dataclass
class Delta:
    last_check: datetime | None
    baseline: datetime | None
    changes: list[Change]  # since the baseline: the last snapshot from before today; is_new = also since the last check
    recent: list[Change]  # exactly what happened since the last check; this is what the event log stores


def compute_changes(history: list[tuple[datetime, dict]], new: dict, now: datetime, since_last: bool = False) -> Delta:
    if not history:
        return Delta(None, None, [], [])
    last_ts, last = history[-1]
    start_of_today = now.replace(hour=0, minute=0, second=0, microsecond=0)
    earlier = [h for h in history if h[0] < start_of_today]
    base_ts, base = history[-1] if since_last else (earlier[-1] if earlier else history[0])
    recent = [replace(c, is_new=True) for c in diff_events(last, new)]
    new_ids = {c.id for c in recent}
    changes = [replace(c, is_new=c.id in new_ids) for c in diff_events(base, new)]
    return Delta(last_ts, base_ts, changes, recent)


# --- event log (progress today, reports) -------------------------------------------------------------------------


def _log_path(state_dir: Path) -> Path:
    return state_dir / "events.jsonl"


def load_log(state_dir: Path) -> list[dict]:
    out = []
    try:
        lines = _log_path(state_dir).read_text().splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            e = json.loads(line)
            e["_ts"] = parse_ts(e["ts"])
            out.append(e)
        except (ValueError, KeyError):
            continue  # a damaged line must not break the briefing
    return out


def write_log(state_dir: Path, rows: list[dict]) -> None:
    atomic_write(_log_path(state_dir), "".join(json.dumps(r) + "\n" for r in rows))


def append_log(state_dir: Path, changes: list[Change], now: datetime) -> None:
    """Append this run's changes and drop entries older than LOG_DAYS."""
    keep = [e for e in load_log(state_dir) if e["_ts"] >= now - timedelta(days=LOG_DAYS)]
    rows = [{k: v for k, v in e.items() if k != "_ts"} for e in keep]
    rows += [{"ts": now.isoformat(), "id": c.id, "kind": c.kind, "key": c.key, "text": c.text,
              "progress": c.progress, "attention": c.attention} for c in changes]
    write_log(state_dir, rows)


def progress_today(logged: list[dict], recent: list[Change], now: datetime) -> list[dict]:
    """Progress made today: earlier runs' logged events plus this run's, newest first."""
    today = now.date()
    items = [{"ts": e["_ts"], "text": e["text"]} for e in logged
             if e.get("progress") and e["_ts"].astimezone(now.tzinfo).date() == today]
    items += [{"ts": now, "text": c.text} for c in recent if c.progress]
    return sorted(items, key=lambda i: i["ts"], reverse=True)
