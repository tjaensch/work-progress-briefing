"""Drafts: comments (and optionally a status change) the briefing proposes. Nothing is posted by building a draft.

Posting happens only through `apply_draft`, which re-checks the item's status, shows exactly what will happen, and asks."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Callable, Protocol

from .models import Draft, Item
from .util import atomic_write


class Tracker(Protocol):
    """What a source must offer to accept drafts. Statuses use the vocabulary in `models.STATUSES`."""

    def get_status(self, item_id: str) -> str: ...
    def available_transitions(self, item_id: str) -> list[str]: ...
    def comment(self, item_id: str, text: str) -> None: ...
    def transition(self, item_id: str, to_status: str) -> bool: ...


# kind -> (PR state it is about, PR timestamp that marks when it happened)
MISMATCH_EVENTS = {"merged_open": ("MERGED", "merged_at"), "open_todo": ("OPEN", "created_at"),
                   "closed_unmerged": ("CLOSED", "closed_at")}


def _prs_phrase(urls: list[str], singular: str, plural: str) -> str:
    return f"PR {urls[0]} {singular}" if len(urls) == 1 else f"PRs {', '.join(urls)} {plural}"


def mismatch_comment(kind: str, status: str, urls: list[str], transition: str | None) -> str:
    """Fixed wording, no model: predictable, and the same every time."""
    pretty = status.replace("_", " ")
    if kind == "merged_open":
        text = f"Status update: {_prs_phrase(urls, 'has', 'have')} been merged. "
        return text + (f"Moving this from {pretty} to {transition.replace('_', ' ')} to match." if transition
                       else f"This is still marked {pretty}; reviewing whether it can move forward.")
    if kind == "open_todo":
        text = f"Status update: {_prs_phrase(urls, 'is', 'are')} open for review, but this is still marked {pretty}."
        return text + (f" Moving it to {transition.replace('_', ' ')}." if transition else "")
    return (f"Status update: {_prs_phrase(urls, 'was', 'were')} closed without merging. This is still marked {pretty}; "
            "next step is to decide whether to open a new PR, take a different approach, or close it.")


def _status_update(item: Item, kind: str, now: datetime, nudge_after_days: int, last_mine: datetime | None,
                   transition: str | None, stamp: str) -> Draft | None:
    pr_state, field = MISMATCH_EVENTS[kind]
    prs = [p for p in item.prs if not p.error and p.state == pr_state]
    times = [getattr(p, field) for p in prs if getattr(p, field)]
    if times:
        # Only worth saying while nothing has touched the item since the event. Once anything (including this very
        # comment) updates it, the news is out and the draft is not offered again.
        if item.updated > max(times):
            return None
    elif last_mine and (now - last_mine).days < nudge_after_days:  # event time unknown: fall back to a cool-down
        return None
    if transition == item.status:
        transition = None
    return Draft(id=f"{item.id}:status_update", key=item.id, kind="status_update",
                 title=f"Update status of {item.id}: {item.mismatches[0]}",
                 comment=mismatch_comment(kind, item.status, [p.url for p in prs], transition),
                 transition=transition, status_at_draft=item.status, created=stamp)


def nudge_text(item: Item, now: datetime) -> str:
    waiting = (now - item.updated).days
    last = next((c.author for c in item.comments if not c.mine), "")
    greeting = f"Hi @{last}, " if last else "Hi, "
    return (f"{greeting}checking in: this has been waiting for {waiting} days. Is there anything blocking it, "
            "or anything I can do to help move it along?")


def build_drafts(items: list[Item], now: datetime, nudge_after_days: int = 3, mismatch_drafts: bool = True,
                 mismatch_transitions: dict | None = None) -> list[Draft]:
    drafts: list[Draft] = []
    mismatch_transitions = mismatch_transitions or {}
    stamp = now.isoformat()
    for i in items:
        mine = [c.created for c in i.comments if c.mine]
        last_mine = max(mine) if mine else None
        prs = [p for p in i.prs if not p.error]
        if i.status == "in_review" and prs and all(p.state == "MERGED" for p in prs):
            drafts.append(Draft(
                id=f"{i.id}:close_out", key=i.id, kind="close_out", title=f"Close out {i.id}",
                comment="Closing out: " + ", ".join(p.url for p in prs) + " merged.", transition="done",
                status_at_draft=i.status, created=stamp))
        if mismatch_drafts:
            for kind in i.mismatch_kinds[:1]:  # the kinds are mutually exclusive, so at most one
                d = _status_update(i, kind, now, nudge_after_days, last_mine, mismatch_transitions.get(kind), stamp)
                if d:
                    drafts.append(d)
        quiet_since = max([i.updated, *mine]) if mine else i.updated
        if i.bucket == "waiting" and (now - quiet_since).days >= nudge_after_days and not i.others_since:
            drafts.append(Draft(id=f"{i.id}:nudge", key=i.id, kind="nudge", title=f"Nudge on {i.id}",
                                comment=nudge_text(i, now), transition=None, status_at_draft=i.status, created=stamp))
    return drafts


# --- persistence ----------------------------------------------------------------------------------------------------


def save_drafts(out_dir: Path, drafts: list[Draft], now: datetime, prefix: str = "drafts") -> Path:
    """`prefix="drafts-eod"` keeps end-of-day drafts apart, so a refresh (which rewrites drafts-DATE.json) never drops them."""
    p = out_dir / f"{prefix}-{now:%Y-%m-%d}.json"
    atomic_write(p, json.dumps([d.__dict__ for d in drafts], indent=1))
    return p


def load_draft(out_dir: Path, draft_id: str) -> Draft | None:
    files = [*out_dir.glob("drafts-????-??-??.json"), *out_dir.glob("drafts-eod-????-??-??.json")]
    for p in sorted(files, key=lambda p: p.stem.split("-", 1)[1].removeprefix("eod-"), reverse=True):
        try:
            for d in json.loads(p.read_text()):
                if d["id"] == draft_id:
                    return Draft(**d)
        except (OSError, ValueError, TypeError):
            continue
    return None


# --- applying -------------------------------------------------------------------------------------------------------


def apply_draft(draft: Draft, tracker: Tracker, confirm: Callable[[str], bool], say: Callable[[str], None] = print,
                assume_yes: bool = False) -> bool:
    """Post a draft after re-checking the item's status and getting confirmation. A status change is only offered if
    the tracker says it is available right now; otherwise only the comment is posted, and the preview says so."""
    current = tracker.get_status(draft.key)
    if current != draft.status_at_draft:
        say(f"Refusing: {draft.key} is now '{current}' but the draft was made for '{draft.status_at_draft}'. "
            "Run the briefing again.")
        return False
    say(f"--- {draft.title}\nComment on {draft.key}:\n  {draft.comment}")
    transition = None
    if draft.transition:
        try:
            available = tracker.available_transitions(draft.key)
        except Exception as e:  # noqa: BLE001 - a failed lookup must not block posting the comment
            say(f"Could not check which status changes are available ({type(e).__name__}: {e}); only the comment will be posted.")
        else:
            if draft.transition in available:
                transition = draft.transition
                say(f"Then move {draft.key} from '{current}' to '{draft.transition}'.")
            else:
                say(f"'{draft.transition}' is not available from '{current}' (offered: {', '.join(available) or 'nothing'}); "
                    "only the comment will be posted.")
    if not assume_yes and not confirm("Post this? [y/N] "):
        say("Cancelled; nothing was written.")
        return False
    tracker.comment(draft.key, draft.comment)
    if not transition:
        say("Comment posted.")
        return True
    try:
        done = tracker.transition(draft.key, transition)
    except Exception as e:  # noqa: BLE001 - the comment is already posted; say so rather than crash
        say(f"Comment posted, but the status change failed: {type(e).__name__}: {e}"[:300])
        return True
    say(f"Moved to '{transition}'." if done else f"Comment posted, but '{transition}' was no longer available.")
    return True
