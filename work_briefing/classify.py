"""Rules that turn an item and its PRs into a bucket, a next action and reasons; scoring and the daily plan."""
from __future__ import annotations

from datetime import datetime

from .models import Item
from .util import ago

BUCKET_BASE = {"ready": 20, "start": 8, "stale": 6, "backlog": 0, "waiting": -10, "blocked": -15}
PRIORITY_POINTS = {"critical": 12, "high": 8, "medium": 3, "low": 0}
SECTIONS = [("ready", "Ready to continue"), ("start", "Ready to start"), ("waiting", "Waiting on others"),
            ("blocked", "Blocked"), ("stale", "Stale"), ("backlog", "Backlog")]


def classify(item: Item, now: datetime, stale_days: int = 7) -> None:
    """Fill bucket, action, reasons, mismatches, quick. Pure: depends only on the item and `now`."""
    item.reasons, item.mismatches, item.mismatch_kinds, item.quick = [], [], [], False
    prs = [p for p in item.prs if not p.error]
    merged = [p for p in prs if p.state == "MERGED"]
    open_prs = [p for p in prs if p.state == "OPEN"]
    closed = [p for p in prs if p.state == "CLOSED"]
    others = item.others_since
    age = (now - item.updated).days
    needs_work = [p for p in open_prs if p.review_state == "changes_requested" or p.ci_fail]
    approved = [p for p in open_prs if p.review_state == "approved" and not p.ci_fail and not p.draft]
    open_blockers = item.blockers

    if others:
        item.reasons.append(f"{len(others)} new comment(s) from others, latest {others[0].author} {ago(others[0].created, now)}")
    for p in open_prs:
        if p.review_state == "changes_requested":
            item.reasons.append(f"PR changes requested: {p.url}")
        elif p.review_state == "changes_addressed":
            item.reasons.append(f"PR changes addressed, awaiting re-review: {p.url}")
        elif p.review_state == "approved":
            item.reasons.append(f"PR approved: {p.url}")
        if p.ci_fail:
            names = f" ({', '.join(p.failing_checks[:3])})" if p.failing_checks else ""
            item.reasons.append(f"PR CI failing{names}: {p.url}")
    item.reasons += [f"PR merged: {p.url}" for p in merged]

    if merged and not open_prs and item.status in ("todo", "blocked", "in_progress"):
        _mismatch(item, "merged_open", f"PR merged but item is still {item.status}")
    if closed and not open_prs and not merged and item.status == "in_review":
        _mismatch(item, "closed_unmerged", "PR closed without merging but item is still in review")
    if open_prs and item.status == "todo":
        _mismatch(item, "open_todo", "PR is open but item is still todo")

    if item.status == "blocked":
        if others or not open_blockers:
            item.bucket, item.action = "ready", "Respond, then unblock" if others else "Check whether it can move again"
        else:
            item.bucket, item.action = "blocked", f"Still blocked by {', '.join(open_blockers)}"
    elif item.status == "todo":
        if open_blockers:
            item.bucket, item.action = "blocked", f"Blocked by {', '.join(open_blockers)}"
        elif item.priority or age <= 14:
            item.bucket, item.action = "start", "Start"
        else:
            item.bucket, item.action = "backlog", "Backlog: start, park or close"
    elif item.status == "in_review":
        if merged and not open_prs:
            item.bucket, item.action, item.quick = "ready", "Merged: close out", True
        elif others or needs_work:
            item.bucket, item.action = "ready", "Address review feedback"
        elif approved:
            item.bucket, item.action, item.quick = "ready", "Approved: merge", True
        else:
            note = " (changes addressed)" if any(p.review_state == "changes_addressed" for p in open_prs) else ""
            since = f" ({age}d since update)" if age >= 3 else ""
            item.bucket, item.action = "waiting", "Waiting on reviewer" + note + since
    else:  # in_progress
        if approved:
            item.bucket, item.action, item.quick = "ready", "Approved: merge", True
        elif needs_work:
            only_ci = not any(p.review_state == "changes_requested" for p in needs_work)
            item.bucket, item.action = "ready", "Fix failing CI on PR" if only_ci else "Address review feedback"
        elif age >= stale_days and not others:
            item.reasons.append(f"no activity for {age}d")
            item.bucket, item.action = "stale", "Stale: continue, park, or re-scope"
        else:
            item.bucket = "ready"
            item.action = "Respond to new comments, then continue" if others else "Continue"
    item.reasons += [f"Check: {m}" for m in item.mismatches]
    if not item.reasons:
        item.reasons.append(f"last updated {ago(item.updated, now)}")


def _mismatch(item: Item, kind: str, text: str) -> None:
    item.mismatch_kinds.append(kind)
    item.mismatches.append(text)


def score(item: Item, now: datetime) -> None:
    parts: list[tuple[float, str]] = [(BUCKET_BASE.get(item.bucket, 0), f"bucket: {item.bucket}")]
    others = item.others_since
    if others:
        waited = (now - min(c.created for c in others)).days
        parts.append((15 + min(waited, 7), f"unanswered comments from others ({waited}d)"))
    if item.status == "blocked" and not item.blockers and item.bucket == "ready":
        parts.append((10, "blockers cleared"))
    for p in item.prs:
        if p.error or p.state != "OPEN":
            continue
        if p.review_state == "changes_requested":
            parts.append((10, "review changes requested"))
        if p.ci_fail:
            parts.append((8, "PR CI failing"))
    if item.quick:
        parts.append((12, "quick win"))
    if item.status == "in_progress":
        parts.append((5, "already in progress"))
    if item.due:
        days = (item.due - now.date()).days
        if days < 0:
            parts.append((30, f"overdue by {-days}d"))
        elif days <= 3:
            parts.append((20, f"due in {days}d"))
        elif days <= 7:
            parts.append((8, f"due in {days}d"))
    if (pts := PRIORITY_POINTS.get(item.priority.lower(), 0)):
        parts.append((pts, f"priority {item.priority}"))
    if item.mismatches:
        parts.append((3, "status mismatch"))
    item.score = sum(n for n, _ in parts)
    item.score_parts = [f"{why} ({n:+g})" for n, why in sorted(parts, key=lambda x: -abs(x[0])) if n]


def build_plan(items: list[Item], size: int) -> list[dict]:
    """The top `size` items that are mine to act on now (not waiting, blocked or backlog), best first."""
    actionable = [i for i in items if i.bucket in ("ready", "start", "stale")]
    ranked = sorted(actionable, key=lambda i: (-i.score, i.id))[:size]
    return [{"rank": n, "ref": i.id, "title": i.title, "next": i.action, "why": "; ".join(i.score_parts[:3]),
             "score": i.score, "quick": i.quick} for n, i in enumerate(ranked, 1)]


def quick_wins(items: list[Item]) -> list[str]:
    return [i.id for i in sorted(items, key=lambda i: (-i.score, i.id)) if i.quick]
