"""Plain data classes shared by every module. Sources fill them in; nothing here knows where data came from."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime

# The one status vocabulary. Each source maps its own states onto these.
STATUSES = ("todo", "in_progress", "in_review", "blocked", "done")
OPEN_STATUSES = STATUSES[:-1]


@dataclass
class Comment:
    id: str
    author: str
    mine: bool
    created: datetime
    body: str = ""


@dataclass
class PullRequest:
    url: str
    title: str = ""
    state: str = "OPEN"  # OPEN | MERGED | CLOSED
    draft: bool = False
    review_state: str = "none"  # none | review_required | changes_requested | changes_addressed | approved
    ci_fail: int = 0
    ci_pending: int = 0
    failing_checks: list[str] = field(default_factory=list)
    created_at: datetime | None = None
    merged_at: datetime | None = None
    closed_at: datetime | None = None
    error: str = ""  # set when the PR could not be read; the other fields are then unreliable


@dataclass
class Item:
    """One thing on my plate: an issue, a ticket or a PR I authored."""

    id: str  # stable, human-readable, no colon, e.g. "acme-labs/widget-api#12" or "DEMO-4"
    title: str
    status: str  # one of STATUSES
    updated: datetime
    url: str = ""
    kind: str = "issue"
    labels: list[str] = field(default_factory=list)
    priority: str = ""
    due: date | None = None
    comments: list[Comment] = field(default_factory=list)  # newest first
    prs: list[PullRequest] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)  # ids of open items that block this one
    # filled in by classify / score
    bucket: str = ""
    action: str = ""
    reasons: list[str] = field(default_factory=list)
    mismatches: list[str] = field(default_factory=list)
    mismatch_kinds: list[str] = field(default_factory=list)  # parallel to `mismatches`
    quick: bool = False
    score: float = 0.0
    score_parts: list[str] = field(default_factory=list)

    @property
    def others_since(self) -> list[Comment]:
        """Comments from others since my last comment (or all of theirs if I never commented)."""
        mine = [c.created for c in self.comments if c.mine]
        since = max(mine) if mine else None
        return [c for c in self.comments if not c.mine and (since is None or c.created > since)]


@dataclass
class Incoming:
    """Something waiting on me that is not one of my items, for example a PR I was asked to review."""

    kind: str  # review_request | mention
    id: str  # a URL, unique
    title: str
    url: str = ""
    when: datetime | None = None
    detail: str = ""
    score: float = 0.0


@dataclass(frozen=True)
class Change:
    """One difference between two runs."""

    id: str  # names what changed, so it can be matched across runs even if the wording differs
    kind: str
    text: str
    key: str = ""  # the item it belongs to, if any
    is_new: bool = False  # since the previous run (otherwise: earlier today)
    progress: bool = False  # something moved forward
    attention: bool = False  # something that may need me


@dataclass
class Draft:
    id: str  # "<item id>:<kind>"
    key: str
    kind: str  # close_out | nudge | status_update | eod_update
    title: str
    comment: str
    transition: str | None  # a status from STATUSES, or None for comment only
    status_at_draft: str
    created: str


@dataclass
class Briefing:
    now: datetime
    items: list[Item] = field(default_factory=list)  # visible items (snoozed ones are in `snoozed`)
    incoming: list[Incoming] = field(default_factory=list)
    plan: list[dict] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)
    progress: list[dict] = field(default_factory=list)  # [{"ts", "text"}] newest first, today
    drafts: list[Draft] = field(default_factory=list)
    snoozed: list[dict] = field(default_factory=list)
    fingerprints: dict[str, str] = field(default_factory=dict)
    health: list[tuple[str, str, str]] = field(default_factory=list)  # (source, ok|failed|skipped, detail)
    last_check: datetime | None = None
