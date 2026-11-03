"""GitHub as a source, through the `gh` CLI (so there is no token handling here): my assigned issues, my open PRs,
review requests, and a tracker that can comment and change status. Everything goes through an injectable runner."""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta

from .models import Comment, Incoming, Item, PullRequest
from .util import Runner, parse_ts

FAIL_STATES = ("FAILURE", "ERROR", "TIMED_OUT", "CANCELLED", "STARTUP_FAILURE", "ACTION_REQUIRED")
PR_FIELDS = ("state,reviewDecision,mergedAt,createdAt,closedAt,updatedAt,statusCheckRollup,isDraft,title,"
             "latestReviews,reviewRequests,commits")
ISSUE_FIELDS = "number,title,url,state,labels,updatedAt,comments,milestone"
LINKED_QUERY = ("query($o:String!,$r:String!,$n:Int!){repository(owner:$o,name:$r){issue(number:$n)"
                "{closedByPullRequestsReferences(first:20){nodes{url}}}}}")
SEARCH_FIELDS = "number,title,url,repository,updatedAt,labels,isPullRequest"
BLOCKED_LABELS = {"blocked", "status: blocked", "status/blocked"}
IN_PROGRESS_LABELS = {"in progress", "in-progress", "status: in progress", "status/in-progress"}
URL_RE = re.compile(r"https://github\.com/([\w.-]+/[\w.-]+)/(?:issues|pull)/(\d+)")


class GhError(Exception):
    pass


def gh_json(runner: Runner, args: list[str], timeout: float = 60):
    p = runner(["gh", *args], timeout=timeout)
    if p.returncode != 0:
        raise GhError((p.stderr or p.stdout or f"gh exited {p.returncode}").strip()[:200])
    try:
        return json.loads(p.stdout or "null")
    except json.JSONDecodeError as e:
        raise GhError(f"gh returned something that is not JSON: {e}") from None


def current_user(runner: Runner) -> str:
    p = runner(["gh", "api", "user", "-q", ".login"], timeout=30)
    if p.returncode != 0 or not p.stdout.strip():
        raise GhError((p.stderr or "could not determine the GitHub user (are you logged in? run `gh auth login`)").strip()[:200])
    return p.stdout.strip()


def label_names(raw: list | None) -> list[str]:
    return [(x.get("name") if isinstance(x, dict) else str(x)) for x in raw or []]


def priority_of(labels: list[str]) -> str:
    """'priority: high', 'priority/high' or 'P1'-style labels; the first one wins."""
    for name in labels:
        m = re.match(r"(?i)priority\s*[:/ -]\s*(\w+)$", name)
        if m:
            return m.group(1).lower()
        if re.fullmatch(r"(?i)p[0-3]", name):
            return {"p0": "critical", "p1": "high", "p2": "medium", "p3": "low"}[name.lower()]
    return ""


def derive_status(labels: list[str], prs: list[PullRequest]) -> str:
    """Map GitHub's open/closed plus labels and linked PRs onto the shared status vocabulary."""
    lower = {n.lower() for n in labels}
    live = [p for p in prs if not p.error and p.state == "OPEN"]
    if lower & BLOCKED_LABELS:
        return "blocked"
    if any(not p.draft for p in live):
        return "in_review"
    if live or lower & IN_PROGRESS_LABELS:
        return "in_progress"
    return "todo"


def assess_pr(d: dict, url: str) -> PullRequest:
    """Reduce `gh pr view --json` output to what the briefing needs.

    GitHub keeps CHANGES_REQUESTED until the reviewer reviews again, so it is downgraded to `changes_addressed` when the
    author has pushed since and every reviewer who asked for changes has been asked to look again."""
    fail, pending, failing = 0, 0, []
    for c in d.get("statusCheckRollup") or []:
        outcome = (c.get("conclusion") or c.get("state") or "").upper()
        if outcome in FAIL_STATES:
            fail += 1
            failing.append(c.get("name") or c.get("context") or "?")
        elif outcome in ("", "PENDING", "EXPECTED"):
            pending += 1
    decision = d.get("reviewDecision") or ""
    review_state = {"APPROVED": "approved", "REVIEW_REQUIRED": "review_required"}.get(decision, "none")
    if decision == "CHANGES_REQUESTED":
        review_state = "changes_requested"
        blockers = [r for r in d.get("latestReviews") or [] if r.get("state") == "CHANGES_REQUESTED"]
        requested = {r.get("login") for r in d.get("reviewRequests") or []}
        pushed = [parse_ts(c["committedDate"]) for c in d.get("commits") or [] if c.get("committedDate")]
        if blockers and pushed and max(pushed) > max(parse_ts(r["submittedAt"]) for r in blockers) and all(
                (r.get("author") or {}).get("login") in requested for r in blockers):
            review_state = "changes_addressed"

    def ts(key: str) -> datetime | None:
        return parse_ts(d[key]) if d.get(key) else None

    return PullRequest(url=url, title=d.get("title", ""), state=d.get("state", ""), draft=bool(d.get("isDraft")),
                       review_state=review_state, ci_fail=fail, ci_pending=pending, failing_checks=failing,
                       created_at=ts("createdAt"), merged_at=ts("mergedAt"), closed_at=ts("closedAt"))


def fetch_pr(runner: Runner, url: str) -> PullRequest:
    return assess_pr(gh_json(runner, ["pr", "view", url, "--json", PR_FIELDS]), url)


def fetch_prs(runner: Runner, urls: list[str], workers: int = 6) -> dict[str, PullRequest]:
    """Each unique PR once; a failure becomes a PullRequest with `error` set instead of aborting the run."""
    def one(url: str) -> tuple[str, PullRequest]:
        try:
            return url, fetch_pr(runner, url)
        except Exception as e:  # noqa: BLE001
            return url, PullRequest(url=url, error=str(e)[:120])

    with ThreadPoolExecutor(max_workers=workers) as ex:
        return dict(ex.map(one, sorted(set(urls))))


def _comments(raw: list[dict], me: str) -> list[Comment]:
    out = [Comment(str(c.get("id", "")), (c.get("author") or {}).get("login", ""), (c.get("author") or {}).get("login") == me,
                   parse_ts(c["createdAt"]), c.get("body") or "") for c in raw or []]
    return sorted(out, key=lambda c: c.created, reverse=True)


def repo_of(search_row: dict) -> str:
    return (search_row.get("repository") or {}).get("nameWithOwner", "")


def item_id(repo: str, number: int) -> str:
    return f"{repo}#{number}"


def split_id(item: str) -> tuple[str, int]:
    repo, _, num = item.rpartition("#")
    return repo, int(num)


def closing_prs(runner: Runner, repo: str, number: int) -> list[str]:
    """URLs of the PRs linked to close this issue (GraphQL: `gh issue view` does not expose them)."""
    owner, _, name = repo.partition("/")
    res = gh_json(runner, ["api", "graphql", "-f", f"query={LINKED_QUERY}", "-f", f"o={owner}", "-f", f"r={name}",
                           "-F", f"n={number}"])
    try:
        return [n["url"] for n in res["data"]["repository"]["issue"]["closedByPullRequestsReferences"]["nodes"] if n.get("url")]
    except (KeyError, TypeError):
        return []


def fetch_items(runner: Runner, me: str, repos: list[str]) -> tuple[list[Item], list[str]]:
    """My open assigned issues (with the PRs that close them) plus my open PRs that no assigned issue covers.
    Returns the items and a list of problems; one unreadable issue never hides the others."""
    rows = gh_json(runner, ["search", "issues", "--assignee=@me", "--state=open", "--limit", "100", "--json", SEARCH_FIELDS]) or []
    issues = [r for r in rows if not r.get("isPullRequest") and (not repos or repo_of(r) in repos)]
    my_prs = gh_json(runner, ["search", "prs", "--author=@me", "--state=open", "--limit", "100", "--json",
                              SEARCH_FIELDS + ",isDraft"]) or []
    my_prs = [r for r in my_prs if not repos or repo_of(r) in repos]

    problems: list[str] = []

    def issue_detail(row: dict) -> tuple[dict, list[str]]:
        try:
            detail = gh_json(runner, ["issue", "view", row["url"], "--json", ISSUE_FIELDS])
        except GhError as e:
            problems.append(f"{item_id(repo_of(row), row['number'])}: could not read details ({e})")
            return {}, []
        try:
            linked = closing_prs(runner, repo_of(row), row["number"])
        except GhError:
            linked = []  # linked PRs are an extra; the issue itself is still worth showing
        return detail, linked

    with ThreadPoolExecutor(max_workers=6) as ex:
        found = list(ex.map(issue_detail, issues))
    pr_urls = {u for _, urls in found for u in urls} | {r["url"] for r in my_prs}
    prs = fetch_prs(runner, sorted(pr_urls))

    items: list[Item] = []
    covered: set[str] = set()
    for row, (d, urls) in zip(issues, found):
        linked = [prs[u] for u in urls if u in prs]
        covered |= {p.url for p in linked}
        labels = label_names(d.get("labels"))
        due = (d.get("milestone") or {}).get("dueOn")
        items.append(Item(
            id=item_id(repo_of(row), row["number"]), title=row["title"], status=derive_status(labels, linked),
            updated=parse_ts(d.get("updatedAt") or row["updatedAt"]), url=row["url"], kind="issue", labels=labels,
            priority=priority_of(labels), due=parse_ts(due).date() if due else None,
            comments=_comments(d.get("comments"), me), prs=linked))
    for row in my_prs:
        if row["url"] in covered:
            continue
        pr = prs[row["url"]]
        labels = label_names(row.get("labels"))
        items.append(Item(
            id=item_id(repo_of(row), row["number"]), title=row["title"], status=derive_status(labels, [pr]),
            updated=parse_ts(row["updatedAt"]), url=row["url"], kind="pr", labels=labels, priority=priority_of(labels), prs=[pr]))
    return sorted(items, key=lambda i: i.id), sorted(problems)


def review_requests(runner: Runner, repos: list[str], now: datetime, lookback_days: int) -> list[Incoming]:
    rows = gh_json(runner, ["search", "prs", "--review-requested=@me", "--state=open", "--limit", "30", "--json",
                            "url,title,repository,updatedAt,author,isDraft"]) or []
    cutoff = now - timedelta(days=lookback_days)
    return [Incoming(kind="review_request", id=r["url"], url=r["url"], title=f"{repo_of(r)}: {r['title']}",
                     when=parse_ts(r["updatedAt"]), detail=f"by {(r.get('author') or {}).get('login', '?')}")
            for r in rows if not r.get("isDraft") and parse_ts(r["updatedAt"]) >= cutoff and (not repos or repo_of(r) in repos)]


class GitHubTracker:
    """Comments and status changes through the REST API (`gh api`), which works the same for issues and PRs.

    Status changes: `done` closes the issue or PR; `blocked` and `in_progress` add the label "blocked" / "in progress"."""

    TRANSITIONS = ("done", "blocked", "in_progress")

    def __init__(self, runner: Runner, me: str):
        self.runner, self.me = runner, me

    def get_status(self, item_id_: str) -> str:
        repo, n = split_id(item_id_)
        try:
            d = gh_json(self.runner, ["issue", "view", str(n), "-R", repo, "--json", "state,labels"])
        except GhError:  # a PR number: `gh issue view` refuses some of them
            d = gh_json(self.runner, ["pr", "view", str(n), "-R", repo, "--json", "state,labels,isDraft"])
            if d.get("state") != "OPEN":
                return "done"
            return derive_status(label_names(d.get("labels")), [PullRequest(url="", state="OPEN", draft=bool(d.get("isDraft")))])
        if d.get("state") == "CLOSED":
            return "done"
        urls = closing_prs(self.runner, repo, n)
        return derive_status(label_names(d.get("labels")), list(fetch_prs(self.runner, urls).values()))

    def available_transitions(self, item_id_: str) -> list[str]:
        return list(self.TRANSITIONS)

    def _api(self, args: list[str]) -> None:
        p = self.runner(["gh", "api", *args], timeout=60)
        if p.returncode != 0:
            raise GhError((p.stderr or p.stdout or "gh api failed").strip()[:200])

    def comment(self, item_id_: str, text: str) -> None:
        repo, n = split_id(item_id_)
        self._api([f"repos/{repo}/issues/{n}/comments", "-f", f"body={text}"])

    def transition(self, item_id_: str, to_status: str) -> bool:
        repo, n = split_id(item_id_)
        if to_status == "done":
            self._api(["-X", "PATCH", f"repos/{repo}/issues/{n}", "-f", "state=closed"])
        elif to_status in ("blocked", "in_progress"):
            self._api(["-X", "POST", f"repos/{repo}/issues/{n}/labels", "-f", f"labels[]={to_status.replace('_', ' ')}"])
        else:
            return False
        return True
