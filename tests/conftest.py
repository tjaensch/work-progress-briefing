"""Shared builders. All data is fictional."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime, timedelta, timezone

import pytest

from work_briefing.models import Comment, Item, PullRequest

NOW = datetime(2026, 11, 2, 12, 0, tzinfo=timezone.utc)  # a Monday


def days_ago(n: float) -> datetime:
    return NOW - timedelta(days=n)


def make_comment(author="alice", days=1.0, mine=False, body="hello", cid="1") -> Comment:
    return Comment(cid, author, mine, days_ago(days), body)


def make_pr(state="OPEN", review_state="review_required", url="https://example.test/acme-labs/widget-api/pull/1",
            **kw) -> PullRequest:
    return PullRequest(url=url, state=state, review_state=review_state, **kw)


def make_item(id="acme-labs/widget-api#1", status="in_progress", updated_days=1.0, **kw) -> Item:
    kw.setdefault("title", f"Title of {id}")
    return Item(id=id, status=status, updated=days_ago(updated_days), **kw)


def cp(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess([], returncode, stdout if isinstance(stdout, str) else json.dumps(stdout), stderr)


class FakeRunner:
    """Maps a command prefix to a result (CompletedProcess, JSON-able value, or a function of the command)."""

    def __init__(self, routes: dict | None = None):
        self.routes, self.calls = routes or {}, []

    def __call__(self, cmd, timeout=60, cwd=None):
        self.calls.append(list(cmd))
        text = " ".join(cmd)
        for prefix in sorted(self.routes, key=len, reverse=True):
            if text.startswith(prefix):
                r = self.routes[prefix]
                r = r(cmd) if callable(r) else r
                return r if isinstance(r, subprocess.CompletedProcess) else cp(r)
        return cp("", 1, f"no route for: {text}")

    def count(self, prefix: str) -> int:
        return sum(" ".join(c).startswith(prefix) for c in self.calls)


@pytest.fixture
def now():
    return NOW
