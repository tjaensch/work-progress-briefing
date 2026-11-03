"""`work-briefing demo`: the whole pipeline on fictional data, no accounts needed.

A fake `gh` replays a small invented story (an org called acme-labs, one developer, a handful of issues and PRs) over four
"days", so the first run has nothing to compare with and the later ones show what changed. The adapter code that runs is
the real one; only the command runner is fake. Every name, title and date here is made up."""
from __future__ import annotations

import argparse
import io
import json
import shutil
import subprocess
import sys
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import github_source as gh, reports, state
from .cli import run_command
from .config import Config
from .pipeline import Deps
from .util import Runner

ME = "dana-dev"
ORG = "acme-labs"
API, DOCS = f"{ORG}/widget-api", f"{ORG}/docs-site"
MARKER = ".demo"  # a home folder holding this file is a demo home: `apply` there never touches GitHub

# Four "days" of the story, oldest first. The last one is "now".
STAGES = [datetime(2026, 3, 6, 16, 0, tzinfo=timezone.utc), datetime(2026, 3, 9, 9, 0, tzinfo=timezone.utc),
          datetime(2026, 3, 10, 16, 30, tzinfo=timezone.utc), datetime(2026, 3, 11, 10, 30, tzinfo=timezone.utc)]
DEMO_NOW = STAGES[-1]


def iso(t: datetime) -> str:
    return t.strftime("%Y-%m-%dT%H:%M:%SZ")


class Scenario:
    """What GitHub would say at one stage (0 = oldest, 3 = now)."""

    def __init__(self, stage: int):
        self.stage, self.t = stage, STAGES[stage]
        self.issues: dict[tuple[str, int], dict] = {}
        self.prs: dict[str, dict] = {}
        self.authored: list[str] = []  # urls of my open PRs
        self.requests: list[dict] = []
        self._build()

    def ago(self, days: float = 0, hours: float = 0) -> str:
        return iso(self.t - timedelta(days=days, hours=hours))

    def url(self, repo: str, number: int, kind: str = "issues") -> str:
        return f"https://github.com/{repo}/{kind}/{number}"

    def comment(self, cid: str, author: str, days: float, body: str) -> dict:
        return {"id": cid, "author": {"login": author}, "createdAt": self.ago(days), "body": body}

    def issue(self, repo, number, title, *, labels=(), updated=1.0, comments=(), prs=(), due=None):
        self.issues[(repo, number)] = {
            "title": title, "labels": [{"name": n} for n in labels], "updatedAt": self.ago(updated), "comments": list(comments),
            "prs": list(prs), "milestone": {"dueOn": iso(due)} if due else None}

    def pr(self, repo, number, *, state="OPEN", decision="", draft=False, checks=(), created=3.0, merged=None, title="PR", mine=True):
        url = self.url(repo, number, "pull")
        self.prs[url] = {
            "state": state, "reviewDecision": decision, "isDraft": draft, "title": title, "createdAt": self.ago(created),
            "mergedAt": self.ago(*merged) if merged else None, "closedAt": self.ago(*merged) if merged else None,
            "updatedAt": self.ago(0.2), "latestReviews": [], "reviewRequests": [], "commits": [],
            "statusCheckRollup": [{"name": n, "conclusion": c} for n, c in checks]}
        if mine and state == "OPEN":
            self.authored.append(url)
        return url

    def _build(self) -> None:
        s = self.stage
        # 12: a rate-limiter fix, due Friday: PR opened, changes requested, then approved
        pr12 = None
        if s >= 1:
            pr12 = self.pr(API, 40, decision="REVIEW_REQUIRED" if s == 1 else ("CHANGES_REQUESTED" if s == 2 else "APPROVED"),
                           checks=[("tests", "SUCCESS")], created=0.5, title="Fix rate limiter window")
        mine12 = [self.comment("m1", ME, 0.4, "Opened a PR with the fix.")] if s >= 1 else []
        self.issue(API, 12, "Rate limiter drops valid requests", labels=["priority: high"], updated=0.1, comments=mine12,
                   prs=[pr12] if pr12 else [], due=datetime(2026, 3, 13, tzinfo=timezone.utc))
        # 15: pagination: todo, then in progress, then a PR whose lint check fails
        labels15 = [] if s == 0 else ["in progress"]
        pr15 = []
        if s == 2:
            pr15 = [self.pr(API, 44, draft=True, checks=[("lint", "SUCCESS")], created=0.3, title="Add pagination")]
        if s == 3:
            pr15 = [self.pr(API, 44, decision="REVIEW_REQUIRED", checks=[("lint", "FAILURE"), ("tests", "SUCCESS")], created=1.3,
                            title="Add pagination")]
        self.issue(API, 15, "Add pagination to /v2/orders", labels=labels15, updated=0.2, prs=pr15)
        # 21: a PR that merged yesterday while the issue was left "in progress"
        pr21 = []
        if s <= 2:
            pr21 = [self.pr(API, 38, decision="REVIEW_REQUIRED", created=2, title="Invalidate cache on user delete")] if s >= 1 else []
        else:
            pr21 = [self.pr(API, 38, state="MERGED", created=3, merged=(0, 20), title="Invalidate cache on user delete")]
        self.issue(API, 21, "Cache invalidation on user delete", labels=["in progress"], updated=2.0 if s == 3 else 0.5, prs=pr21)
        # 18: waiting on a reviewer for five days (nudge material)
        old_reviewer = self.comment("r1", "kim-docs", 7.0, "Looks plausible, will test on staging.")
        self.issue(API, 18, "Intermittent timeout in checkout flow", updated=5.0, prs=[
            self.pr(API, 42, decision="REVIEW_REQUIRED", created=6, title="Raise checkout timeout")],
            comments=[self.comment("m2", ME, 5.0, "Ready for another look when you have time."), old_reviewer])
        # 9: blocked on infrastructure
        self.issue(API, 9, "Upgrade to Python 3.13", labels=["blocked"], updated=5.5,
                   comments=[self.comment("m9", ME, 5.5, "Thanks, I will wait for the image."),
                             self.comment("c9", "sam-infra", 6.0, "Base image is not published yet.")])
        # 30: a reviewer asked a question today
        ask = [self.comment("c30", "kim-docs", 0.1, "Should the error message include the header name?")] if s == 3 else []
        self.issue(API, 30, "Clearer webhook signature error", labels=["in progress"], updated=0.1 if s == 3 else 1.5, comments=ask)
        # 5: a spike nobody has touched for twelve days
        self.issue(API, 5, "Spike: evaluate a queue library", labels=["in progress"], updated=12.0)
        # 2: a tiny README fix: PR merged on Monday, issue gone from my list by Tuesday
        if s == 0:
            self.issue(API, 2, "Fix typo in README", updated=1.0, prs=[self.pr(API, 36, decision="APPROVED", created=1, title="Fix typo")])
        elif s == 1:
            self.issue(API, 2, "Fix typo in README", updated=0.1, prs=[self.pr(API, 36, state="MERGED", created=2, merged=(0, 1), title="Fix typo")])
        # docs: a new todo appears on Tuesday; an old one sits in the backlog
        if s >= 2:
            self.issue(DOCS, 7, "Document the webhook retry policy", updated=0.5)
        self.issue(DOCS, 3, "Refresh the onboarding guide", updated=60.0)
        if s == 3:
            self.requests.append({"url": self.url(DOCS, 51, "pull"), "title": "Fix broken links in the API guide",
                                  "repository": {"nameWithOwner": DOCS}, "updatedAt": self.ago(2.0), "author": {"login": "kim-docs"},
                                  "isDraft": False})

    # --- what `gh` would print -----------------------------------------------------------------------------------

    def search_issues(self) -> list[dict]:
        return [{"number": n, "url": self.url(repo, n), "title": i["title"], "repository": {"nameWithOwner": repo},
                 "updatedAt": i["updatedAt"], "labels": i["labels"], "isPullRequest": False} for (repo, n), i in sorted(self.issues.items())]

    def search_my_prs(self) -> list[dict]:
        rows = []
        for url in self.authored:
            repo, _, n = url.removeprefix("https://github.com/").partition("/pull/")
            p = self.prs[url]
            rows.append({"number": int(n), "url": url, "title": p["title"], "repository": {"nameWithOwner": repo},
                         "updatedAt": p["updatedAt"], "labels": [], "isPullRequest": True, "isDraft": p["isDraft"]})
        return rows

    def issue_view(self, repo: str, number: int) -> dict:
        i = self.issues[(repo, number)]
        return {"number": number, "updatedAt": i["updatedAt"], "labels": i["labels"], "comments": i["comments"],
                "milestone": i["milestone"], "state": "OPEN"}

    def linked(self, repo: str, number: int) -> dict:
        nodes = [{"url": u} for u in self.issues[(repo, number)]["prs"]]
        return {"data": {"repository": {"issue": {"closedByPullRequestsReferences": {"nodes": nodes}}}}}


class DemoRunner:
    """A stand-in for running `gh`. Anything it does not recognise fails, so nothing real is ever reached."""

    def __init__(self, stage: int):
        self.scenario = Scenario(stage)
        self.calls: list[list[str]] = []

    def __call__(self, cmd, timeout=60, cwd=None):
        self.calls.append(list(cmd))
        try:
            answer = self._answer(cmd)
        except KeyError as e:
            return subprocess.CompletedProcess(cmd, 1, "", f"demo has no answer for: {' '.join(cmd[:4])} ({e})")
        return subprocess.CompletedProcess(cmd, 0, answer if isinstance(answer, str) else json.dumps(answer), "")

    def _answer(self, cmd: list[str]):
        sc, args = self.scenario, cmd[1:]
        if args[:2] == ["api", "user"]:
            return ME + "\n"
        if args[:2] == ["search", "issues"]:
            return sc.search_issues()
        if args[:2] == ["search", "prs"]:
            return sc.requests if any(a.startswith("--review-requested") for a in args) else sc.search_my_prs()
        if args[:2] == ["issue", "view"]:
            repo, _, number = args[2].removeprefix("https://github.com/").partition("/issues/")
            return sc.issue_view(repo, int(number))
        if args[:2] == ["pr", "view"]:
            return sc.prs[args[2]]
        if args[:2] == ["api", "graphql"]:
            values = {a.split("=", 1)[0]: a.split("=", 1)[1] for a in args if "=" in a and not a.startswith("query=")}
            return sc.linked(f"{values['o']}/{values['r']}", int(values["n"]))
        raise KeyError(" ".join(args[:3]))


class DemoTracker:
    """Pretends to post, so `apply` in a demo home shows the real preview without any network."""

    def __init__(self):
        self.posted: list[tuple[str, str]] = []

    def get_status(self, item_id: str) -> str:
        return state_of_demo(item_id)

    def available_transitions(self, item_id: str) -> list[str]:
        return ["done", "blocked", "in_progress"]

    def comment(self, item_id: str, text: str) -> None:
        print(f"  (demo) would post to {item_id}: {text}")

    def transition(self, item_id: str, to_status: str) -> bool:
        print(f"  (demo) would move {item_id} to {to_status}")
        return True


def state_of_demo(item_id: str) -> str:
    """The status each demo item has at the final stage, so the status re-check in `apply` passes."""
    sc = Scenario(len(STAGES) - 1)
    repo, _, n = item_id.rpartition("#")
    issue = sc.issues[(repo, int(n))]
    prs = [gh.assess_pr(sc.prs[u], u) for u in issue["prs"]]
    return gh.derive_status([x["name"] for x in issue["labels"]], prs)


def is_demo_home(home: Path) -> bool:
    return (home / MARKER).exists()


def demo_command(a: argparse.Namespace) -> int:
    home = Path(a.home) / "demo"
    if home.exists():
        shutil.rmtree(home)  # only ever our own `demo` folder under the chosen home
    home.mkdir(parents=True)
    (home / MARKER).write_text("fictional data; safe to delete\n")
    ns = argparse.Namespace(home=str(home), config=None, full=False, no_state=False, no_snooze=False, since_last=False, keep_days=14)
    config = Config(me=ME)
    for stage, when in enumerate(STAGES):
        ns.full = stage == len(STAGES) - 1
        deps = Deps(runner=DemoRunner(stage), now=when, state_dir=home / "state", config=config, me=ME)
        buf = io.StringIO()
        with redirect_stdout(buf if stage < len(STAGES) - 1 else sys.stdout):
            if run_command(ns, deps) != 0:
                return 1
    out = home / "out"
    print("\n" + "=" * 72 + "\nTry the reports on the same data:\n")
    for kind in ("standup", "week"):
        print(reports.build_report(kind, state.load_log(home / "state"), reports.load_latest(out), DEMO_NOW)[0])
    print(f"Everything above is fictional and lives in {home}. Commands on that folder use the demo data, never GitHub:")
    print(f"  work-briefing --home {home} snooze {API}#5 --days 3 --reason \"parked\"")
    print(f"  work-briefing --home {home} apply {API}#21:status_update")
    return 0
