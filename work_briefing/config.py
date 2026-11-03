"""Configuration: a small JSON file plus a home directory for state and output."""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, fields
from pathlib import Path


def home_dir() -> Path:
    """Where state and output live: $WPB_HOME, else ~/.local/share/work-progress-briefing."""
    return Path(os.environ.get("WPB_HOME") or Path.home() / ".local" / "share" / "work-progress-briefing")


@dataclass
class Config:
    me: str = ""  # your GitHub login; empty means ask `gh`
    repos: list[str] = field(default_factory=list)  # limit to these "owner/name" repos; empty means all
    plan_size: int = 5
    stale_days: int = 7  # an In Progress item with no activity for this long is "stale"
    nudge_after_days: int = 3  # no new nudge draft while I commented within this many days
    lookback_days: int = 14  # review requests and mentions older than this are ignored
    mismatch_drafts: bool = True
    mismatch_transitions: dict = field(default_factory=dict)  # kind -> status to offer with that draft


MISMATCH_KINDS = ("merged_open", "open_todo", "closed_unmerged")


def validate(cfg: Config) -> None:
    for name in ("plan_size", "stale_days", "nudge_after_days", "lookback_days"):
        v = getattr(cfg, name)
        if not isinstance(v, int) or isinstance(v, bool) or v < 1:
            raise ValueError(f"config: {name} must be a whole number of at least 1")
    if not (isinstance(cfg.repos, list) and all(isinstance(r, str) and r.count("/") == 1 for r in cfg.repos)):
        raise ValueError("config: repos must be a list of \"owner/name\" strings")
    if not isinstance(cfg.mismatch_drafts, bool):
        raise ValueError("config: mismatch_drafts must be true or false")
    from .models import OPEN_STATUSES

    mt = cfg.mismatch_transitions
    if not (isinstance(mt, dict) and all(k in MISMATCH_KINDS and v in OPEN_STATUSES + ("done",) for k, v in mt.items())):
        raise ValueError(f"config: mismatch_transitions maps {', '.join(MISMATCH_KINDS)} to a status "
                         "(todo, in_progress, in_review, blocked, done)")


def load_config(path: Path) -> Config:
    """Defaults when the file is missing; unknown keys are an error so a typo is never silently ignored."""
    cfg = Config()
    if path.exists():
        data = json.loads(path.read_text())
        known = {f.name for f in fields(Config)}
        unknown = sorted(set(data) - known)
        if unknown:
            raise ValueError(f"config: unknown key(s) {', '.join(unknown)}; known keys: {', '.join(sorted(known))}")
        for k, v in data.items():
            setattr(cfg, k, v)
    validate(cfg)
    return cfg
