"""Snooze and dismiss, keyed to a fingerprint of "what changed" so an item comes back when it matters.

Ids: an item id hides the whole item; `<item>:mismatch` only its status-mismatch warning; `<item>:<kind>` one draft
(`nudge`, `close_out`, `status_update`, `eod_update`); `in-xxxxxxxx` one incoming item. A snooze lasts until a date,
until the fingerprint changes, or both (whichever comes first)."""
from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from datetime import date, datetime, timedelta
from pathlib import Path

from .models import Change, Draft, Incoming, Item
from .util import atomic_write

OPEN_ENDED_DAYS = 90  # an until-change snooze with no date is dropped after this long, so nothing hides forever
WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


def _hash(*parts) -> str:
    return hashlib.sha1(json.dumps(parts, default=str, sort_keys=True).encode()).hexdigest()[:12]


def short_id(url: str) -> str:
    return "in-" + hashlib.sha1(url.encode()).hexdigest()[:8]


def base_id(snooze_id: str) -> str:
    """The item an id belongs to ("a#1:nudge" -> "a#1"); incoming ids are their own base."""
    return snooze_id.split(":", 1)[0]


# --- fingerprints ---------------------------------------------------------------------------------------------------


def item_fp(i: Item) -> str:
    """Changes when someone else comments, the status moves, a PR changes state/review/CI, or a blocker moves.
    My own comments do not count, so posting a nudge does not undo the snooze."""
    others = [c.id for c in i.comments if not c.mine]
    prs = sorted((p.url, p.state, p.review_state, p.ci_fail) for p in i.prs if not p.error)
    return _hash(i.status, len(others), others[:1], prs, sorted(i.blockers))


def mismatch_fp(i: Item) -> str:
    return _hash(i.status, sorted(i.mismatches), sorted((p.url, p.state) for p in i.prs if not p.error))


def incoming_fp(x: Incoming) -> str:
    return _hash(x.title, x.when.isoformat() if x.when else "")


# --- stored snoozes -------------------------------------------------------------------------------------------------


@dataclass
class Entry:
    mode: str  # until_date | until_change
    until: str | None  # ISO date it comes back (for until_change: an optional cap)
    reason: str
    created: str
    fingerprint: str | None = None  # what the item looked like when snoozed (until_change)


class Snoozes:
    def __init__(self, entries: dict[str, Entry] | None = None):
        self.entries: dict[str, Entry] = entries or {}

    @staticmethod
    def path(state_dir: Path) -> Path:
        return state_dir / "snoozes.json"

    @classmethod
    def load(cls, state_dir: Path) -> "Snoozes":
        try:
            raw = json.loads(cls.path(state_dir).read_text())
            return cls({k: Entry(**v) for k, v in raw.items()})
        except (OSError, ValueError, TypeError):
            return cls()  # a damaged file must not break the briefing; nothing is hidden

    def save(self, state_dir: Path) -> None:
        atomic_write(self.path(state_dir), json.dumps({k: asdict(v) for k, v in sorted(self.entries.items())}, indent=1))

    def active(self, sid: str, fp: str | None, today: date) -> bool:
        e = self.entries.get(sid)
        if e is None:
            return False
        if e.until and today >= date.fromisoformat(e.until):
            return False
        if e.mode == "until_change":
            return fp is not None and fp == e.fingerprint
        return True


# --- applying snoozes during a run ----------------------------------------------------------------------------------


class Context:
    """Decides what is hidden in one run and remembers what to tidy up afterwards."""

    def __init__(self, snoozes: Snoozes, today: date, enabled: bool = True):
        self.snoozes, self.today, self.enabled = snoozes, today, enabled
        self.fps: dict[str, str] = {}
        self.shown: list[dict] = []
        self._lifted: set[str] = set()

    def check(self, sid: str, kind: str, label: str, fp: str) -> bool:
        self.fps[sid] = fp
        e = self.snoozes.entries.get(sid)
        if not self.enabled or e is None:
            return False
        if self.snoozes.active(sid, fp, self.today):
            until = f"until {e.until}" if e.until else "until it changes"
            self.shown.append({"id": sid, "kind": kind, "label": label, "until": until, "reason": e.reason})
            return True
        if e.mode == "until_change" and not (e.until and self.today >= date.fromisoformat(e.until)):
            self._lifted.add(sid)  # it changed: the snooze has done its job
        return False

    def prune(self, live_ids: set[str]) -> bool:
        """Drop snoozes that expired, were lifted by a change, or belong to items that are gone."""
        gone = []
        for sid, e in self.snoozes.entries.items():
            expired = bool(e.until and self.today >= date.fromisoformat(e.until))
            stale = e.until is None and (self.today - datetime.fromisoformat(e.created).date()).days > OPEN_ENDED_DAYS
            orphan = not sid.startswith("in-") and base_id(sid) not in live_ids
            if expired or stale or orphan or sid in self._lifted:
                gone.append(sid)
        for sid in gone:
            del self.snoozes.entries[sid]
        return bool(gone)


def split_items(ctx: Context, items: list[Item]) -> tuple[list[Item], list[Item]]:
    visible, hidden = [], []
    for i in items:
        (hidden if ctx.check(i.id, "item", i.title, item_fp(i)) else visible).append(i)
    return visible, hidden


def split_incoming(ctx: Context, items: list[Incoming]) -> tuple[list[Incoming], list[Incoming]]:
    visible, hidden = [], []
    for x in items:
        (hidden if ctx.check(short_id(x.id), "incoming", x.title, incoming_fp(x)) else visible).append(x)
    return visible, hidden


def apply_mismatches(ctx: Context, items: list[Item]) -> None:
    """A snoozed mismatch loses its warning but the item stays."""
    for i in items:
        if i.mismatches and ctx.check(f"{i.id}:mismatch", "mismatch", f"{i.id}: {i.mismatches[0]}", mismatch_fp(i)):
            i.reasons = [r for r in i.reasons if not r.startswith("Check:")]
            i.mismatches, i.mismatch_kinds = [], []


def filter_drafts(ctx: Context, drafts: list[Draft], items: dict[str, Item]) -> list[Draft]:
    keep = []
    for d in drafts:
        item = items.get(d.key)
        if not ctx.check(d.id, "draft", d.title, item_fp(item) if item else d.created):
            keep.append(d)
    return keep


def quiet_changes(changes: list[Change], hidden_items: set[str], hidden_incoming: set[str]) -> list[Change]:
    """Changes on hidden things stay in the lists but never ask for attention."""
    def muted(c: Change) -> bool:
        if c.key and c.key in hidden_items:
            return True
        prefix, _, rest = c.id.partition(":")
        return prefix in ("incoming", "cleared") and short_id(rest) in hidden_incoming

    return [replace(c, attention=False) if c.attention and muted(c) else c for c in changes]


# --- command helpers ------------------------------------------------------------------------------------------------


def parse_until(text: str, today: date) -> date:
    """YYYY-MM-DD, 'tomorrow', or a weekday name (the next one, never today)."""
    t = text.strip().lower()
    if t == "tomorrow":
        return today + timedelta(days=1)
    if t in WEEKDAYS:
        ahead = (WEEKDAYS.index(t) - today.weekday()) % 7 or 7
        return today + timedelta(days=ahead)
    try:
        d = date.fromisoformat(t)
    except ValueError:
        raise ValueError(f"cannot read the date {text!r}; use YYYY-MM-DD, 'tomorrow' or a weekday") from None
    if d <= today:
        raise ValueError(f"{text} is not in the future")
    return d


def make_entry(mode: str, until: date | None, reason: str, fp: str | None, now: datetime) -> Entry:
    return Entry(mode, until.isoformat() if until else None, reason, now.isoformat(), fp if mode == "until_change" else None)
