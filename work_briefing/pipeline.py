"""One refresh: fetch -> classify -> snooze -> score -> plan -> drafts -> what changed -> progress."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import actions, classify, github_source as gh, snooze, state
from .config import Config
from .models import Briefing
from .util import Runner


@dataclass
class Deps:
    runner: Runner
    now: datetime
    state_dir: Path
    config: Config
    me: str = ""


@dataclass
class Options:
    no_state: bool = False  # do not save a snapshot or log entries
    no_snooze: bool = False  # show everything; snoozes are kept
    since_last: bool = False  # diff against the last run instead of the last day before today


def run(opts: Options, deps: Deps) -> Briefing:
    cfg, now = deps.config, deps.now
    me = deps.me or cfg.me or gh.current_user(deps.runner)
    b = Briefing(now=now)

    items, problems = gh.fetch_items(deps.runner, me, cfg.repos)  # a failure here is fatal: there is nothing to show
    b.health.append(("github items", "failed" if problems and not items else "ok", "; ".join(problems)))
    try:
        incoming = gh.review_requests(deps.runner, cfg.repos, now, cfg.lookback_days)
        b.health.append(("review requests", "ok", ""))
    except gh.GhError as e:  # an extra source: say so and carry on
        incoming = []
        b.health.append(("review requests", "failed", str(e)))

    for i in items:
        classify.classify(i, now, cfg.stale_days)
    snoozes = snooze.Snoozes.load(deps.state_dir)
    ctx = snooze.Context(snoozes, now.date(), enabled=not opts.no_snooze)
    visible, hidden = snooze.split_items(ctx, items)
    snooze.apply_mismatches(ctx, visible)
    for i in visible:
        classify.score(i, now)
    shown_incoming, hidden_incoming = snooze.split_incoming(ctx, incoming)
    for x in shown_incoming:
        classify.score_incoming(x, now)

    b.items, b.incoming = sorted(visible, key=lambda i: (-i.score, i.id)), shown_incoming
    b.plan = classify.build_plan(visible, cfg.plan_size, shown_incoming)
    b.drafts = snooze.filter_drafts(
        ctx, actions.build_drafts(visible, now, cfg.nudge_after_days, cfg.mismatch_drafts, cfg.mismatch_transitions),
        {i.id: i for i in visible})
    b.snoozed, b.fingerprints = ctx.shown, ctx.fps

    snap = state.snapshot(b, hidden, hidden_incoming)
    delta = state.compute_changes(state.load_history(deps.state_dir), snap, now, opts.since_last)
    hidden_ids, hidden_in = {i.id for i in hidden}, {snooze.short_id(x.id) for x in hidden_incoming}
    delta.changes = snooze.quiet_changes(delta.changes, hidden_ids, hidden_in)
    delta.recent = snooze.quiet_changes(delta.recent, hidden_ids, hidden_in)
    b.changes, b.last_check = delta.changes, delta.last_check
    b.progress = state.progress_today(state.load_log(deps.state_dir), delta.recent, now)

    # Partial data would create phantom changes next run, so only complete runs are remembered.
    complete = not problems and not any(p.error for i in items for p in i.prs)
    if complete and not opts.no_state:
        state.save_snapshot(deps.state_dir, snap, now)
        state.append_log(deps.state_dir, delta.recent, now)
        if not opts.no_snooze and ctx.prune({i.id for i in items}):
            snoozes.save(deps.state_dir)
    return b
