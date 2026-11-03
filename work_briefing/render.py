"""Markdown, terminal and JSON views of a briefing."""
from __future__ import annotations

from .classify import SECTIONS
from .models import Briefing, Item
from .snooze import short_id
from .util import ago, clip, since_text


def _pr_line(p) -> str:
    if p.error:
        return f"PR status unavailable ({p.error}): {p.url}"
    review = "" if p.review_state == "none" else f", {p.review_state.replace('_', ' ')}"
    failing = f" [failing: {', '.join(p.failing_checks[:3])}]" if p.failing_checks else ""
    return f"PR {p.state}{' (draft)' if p.draft else ''}{review}, CI fail {p.ci_fail}/pending {p.ci_pending}{failing}: {p.url}"


def _item_md(i: Item, now) -> list[str]:
    title = f"[{i.id}]({i.url})" if i.url else i.id
    L = [f"### {title} {i.title}", f"- **{i.status.replace('_', ' ')}**, updated {ago(i.updated, now)}. **Next: {i.action}**"]
    L += [f"- {r}" + (f"  (snooze: `{i.id}:mismatch`)" if r.startswith("Check:") else "") for r in i.reasons]
    L += [f"- {_pr_line(p)}" for p in i.prs]
    if i.others_since:
        c = i.others_since[0]
        L.append(f"- Latest from {c.author} ({ago(c.created, now)}): \"{clip(c.body, 200)}\"")
    if i.score_parts:
        L.append(f"- Ranking: {i.score:g} = " + ", ".join(i.score_parts[:4]))
    L.append("")
    return L


def render_markdown(b: Briefing) -> str:
    L = [f"# Progress briefing {b.now:%Y-%m-%d (%a) %H:%M}",
         f"{len(b.items)} open item(s) on my plate. {len(b.incoming)} waiting on me elsewhere.", ""]
    L += ["## Today's plan"]
    if not b.plan:
        L.append("Nothing to do right now.")
    for p in b.plan:
        label = p["ref"] if p["kind"] == "item" else p["kind"].replace("_", " ")
        L.append(f"{p['rank']}. **{label}** {p['title']}  \n   Next: {p['next']}" + (f"  \n   Why: {p['why']}" if p["why"] else ""))
    L.append("")
    if b.last_check:
        L += [f"## Since your last check ({since_text(b.last_check, b.now)})"]
        fresh = [c for c in b.changes if c.is_new]
        L += [f"- {'**needs you:** ' if c.attention else ''}{c.text}" for c in sorted(fresh, key=lambda c: not c.attention)] or ["- Nothing new."]
        earlier = [c for c in b.changes if not c.is_new]
        if earlier:
            L += ["", "### Earlier since the last day"] + [f"- {c.text}" for c in earlier]
        L.append("")
    if b.progress:
        L += ["## Progress today"] + [f"- {p['ts']:%H:%M} {p['text']}" for p in b.progress] + [""]
    if b.incoming:
        L += ["## Waiting on you"] + [f"- {x.title} ({x.detail}): {x.url}" for x in b.incoming] + [""]
    quick = [i for i in b.items if i.quick]
    if quick:
        L += ["## Quick wins"] + [f"- **{i.id}** {i.title}: {i.action}" for i in quick] + [""]
    for bucket, heading in SECTIONS:
        group = [i for i in b.items if i.bucket == bucket]
        if group:
            L += [f"## {heading} ({len(group)})", ""]
            for i in group:
                L += _item_md(i, b.now)
    if b.drafts:
        L += ["## Drafts for your approval (nothing is posted until you confirm)", ""]
        for d in b.drafts:
            L += [f"- `{d.id}` {d.title}", f"  > {d.comment}"]
            if d.transition:
                L.append(f"  - Then moves it from {d.status_at_draft} to **{d.transition}** (only if the source offers it when you apply)")
            L.append(f"  - Apply: `work-briefing apply {d.id}`")
        L.append("")
    if b.snoozed:
        L += ["## Snoozed (hidden for now)"] + [f"- `{s['id']}` {s['label']} ({s['until']}" + (f": {s['reason']}" if s["reason"] else "") + ")"
                                               for s in b.snoozed] + [""]
    bad = [h for h in b.health if h[1] != "ok"]
    L += ["## Sources"] + [f"- {n}: {st}" + (f" ({d})" if d else "") for n, st, d in (bad or b.health)]
    return "\n".join(L) + "\n"


def render_compact(b: Briefing, path) -> str:
    L = [f"=== Progress briefing {b.now:%Y-%m-%d (%a) %H:%M}: {len(b.items)} items, {len(b.incoming)} waiting on you ==="]
    if b.last_check:
        fresh = [c for c in b.changes if c.is_new]
        L.append(f"SINCE YOUR LAST CHECK ({since_text(b.last_check, b.now)}): {len(fresh)} change(s)")
        L += [f"  {'! ' if c.attention else '  '}{c.text}" for c in sorted(fresh, key=lambda c: not c.attention)[:8]]
    L.append("TODAY'S PLAN")
    L += [f"  {p['rank']}. {p['ref']}: {clip(p['title'], 60)} -> {p['next']}" for p in b.plan] or ["  (nothing)"]
    quick = [i.id for i in b.items if i.quick]
    if quick:
        L.append("QUICK WINS: " + ", ".join(quick))
    if b.drafts:
        L.append(f"DRAFTS AWAITING APPROVAL ({len(b.drafts)}): " + ", ".join(d.id for d in b.drafts))
    L += [f"FAILED SOURCE: {n} ({d})" for n, st, d in b.health if st == "failed"]
    L.append(f"Full briefing: {path}")
    return "\n".join(L)


def _item_json(i: Item) -> dict:
    return {"id": i.id, "title": i.title, "url": i.url, "kind": i.kind, "status": i.status, "bucket": i.bucket, "action": i.action,
            "score": i.score, "score_parts": i.score_parts, "quick": i.quick, "reasons": i.reasons, "priority": i.priority,
            "mismatches": i.mismatches, "mismatch_kinds": i.mismatch_kinds, "updated": i.updated.isoformat(),
            "due": i.due.isoformat() if i.due else None,
            "prs": [{"url": p.url, "state": p.state, "draft": p.draft, "review_state": p.review_state, "ci_fail": p.ci_fail,
                     "failing_checks": p.failing_checks, "error": p.error} for p in i.prs]}


def to_json(b: Briefing) -> dict:
    return {
        "generated": b.now.isoformat(), "plan": b.plan, "items": [_item_json(i) for i in b.items],
        "incoming": [{"kind": x.kind, "id": x.id, "title": x.title, "url": x.url, "detail": x.detail,
                      "when": x.when.isoformat() if x.when else None, "snooze_id": short_id(x.id)}
                     for x in b.incoming],
        "delta": {"last_check": b.last_check.isoformat() if b.last_check else None,
                  "events": [{"id": c.id, "kind": c.kind, "text": c.text, "key": c.key, "new_since_last_run": c.is_new,
                              "progress": c.progress, "attention": c.attention} for c in b.changes]},
        "progress_today": [{"ts": p["ts"].isoformat(), "text": p["text"]} for p in b.progress],
        "drafts": [d.__dict__ for d in b.drafts], "snoozed": b.snoozed, "snooze_fingerprints": b.fingerprints,
        "health": [list(h) for h in b.health],
    }
