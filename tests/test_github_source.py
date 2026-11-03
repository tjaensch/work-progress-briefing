import json

import pytest

from conftest import NOW, FakeRunner, cp
from work_briefing import github_source as gh
from work_briefing.models import PullRequest

REPO = "acme-labs/widget-api"
OLD, NEWER = "2026-10-20T15:10:44Z", "2026-10-28T12:27:39Z"


def pr_json(**kw):
    base = {"state": "OPEN", "reviewDecision": "", "isDraft": False, "title": "T", "statusCheckRollup": [], "latestReviews": [],
            "reviewRequests": [], "commits": [], "mergedAt": None, "updatedAt": NEWER}
    base.update(kw)
    return base


def changes_requested(login="rita", at=OLD):
    return {"author": {"login": login}, "state": "CHANGES_REQUESTED", "submittedAt": at}


# --- reducing a PR --------------------------------------------------------------------------------------------------


def test_review_states_map_from_the_decision():
    f = lambda **kw: gh.assess_pr(pr_json(**kw), "u").review_state  # noqa: E731
    assert (f(reviewDecision="APPROVED"), f(reviewDecision="REVIEW_REQUIRED"), f()) == ("approved", "review_required", "none")
    assert f(reviewDecision="CHANGES_REQUESTED") == "changes_requested"


def test_changes_requested_becomes_addressed_only_after_a_push_and_a_re_request():
    base = dict(reviewDecision="CHANGES_REQUESTED", latestReviews=[changes_requested()],
                commits=[{"committedDate": NEWER}])
    assert gh.assess_pr(pr_json(**base), "u").review_state == "changes_requested"  # pushed, but reviewer not asked again
    asked = dict(base, reviewRequests=[{"login": "rita"}])
    assert gh.assess_pr(pr_json(**asked), "u").review_state == "changes_addressed"
    no_push = dict(asked, commits=[{"committedDate": "2026-10-01T00:00:00Z"}])
    assert gh.assess_pr(pr_json(**no_push), "u").review_state == "changes_requested"  # re-requested, but nothing new pushed
    other = dict(asked, reviewRequests=[{"login": "someone-else"}])
    assert gh.assess_pr(pr_json(**other), "u").review_state == "changes_requested"


def test_ci_counts_and_failing_check_names():
    rollup = [{"name": "lint", "conclusion": "FAILURE"}, {"context": "legacy", "state": "ERROR"},
              {"name": "tests", "conclusion": "SUCCESS"}, {"name": "slow", "status": "IN_PROGRESS", "conclusion": ""},
              {"name": "skipped", "conclusion": "SKIPPED"}]
    pr = gh.assess_pr(pr_json(statusCheckRollup=rollup), "u")
    assert (pr.ci_fail, pr.ci_pending, pr.failing_checks) == (2, 1, ["lint", "legacy"])


def test_times_state_and_draft_are_kept():
    pr = gh.assess_pr(pr_json(state="MERGED", isDraft=True, createdAt=OLD, mergedAt=NEWER, closedAt=NEWER, title="Fix"), "u")
    assert (pr.state, pr.draft, pr.title, pr.created_at.day, pr.merged_at.day, pr.closed_at.day) == ("MERGED", True, "Fix", 20, 28, 28)
    assert gh.assess_pr(pr_json(), "u").closed_at is None


def test_fetch_prs_dedupes_and_isolates_errors():
    runner = FakeRunner({"gh pr view https://example.test/a/b/pull/1": pr_json(),
                         "gh pr view https://example.test/a/b/pull/2": cp("", 1, "not found")})
    out = gh.fetch_prs(runner, ["https://example.test/a/b/pull/1"] * 3 + ["https://example.test/a/b/pull/2"])
    assert runner.count("gh pr view") == 2 and out["https://example.test/a/b/pull/1"].state == "OPEN"
    assert "not found" in out["https://example.test/a/b/pull/2"].error


# --- labels and statuses --------------------------------------------------------------------------------------------


def test_priority_labels():
    assert gh.priority_of(["bug", "priority: High"]) == "high" and gh.priority_of(["Priority/low"]) == "low"
    assert gh.priority_of(["P0"]) == "critical" and gh.priority_of(["p2"]) == "medium" and gh.priority_of(["bug"]) == ""


@pytest.mark.parametrize("labels,prs,expected", [
    ([], [], "todo"), (["blocked"], [], "blocked"), (["In Progress"], [], "in_progress"),
    ([], [PullRequest("u", draft=True)], "in_progress"), ([], [PullRequest("u")], "in_review"),
    (["blocked"], [PullRequest("u")], "blocked"), ([], [PullRequest("u", state="MERGED")], "in_progress"), ([], [PullRequest("u", state="CLOSED")], "todo"),
    ([], [PullRequest("u", error="x")], "todo"),
])
def test_derive_status(labels, prs, expected):
    assert gh.derive_status(labels, prs) == expected


def test_label_names_accepts_dicts_and_strings():
    assert gh.label_names([{"name": "a"}, "b"]) == ["a", "b"] and gh.label_names(None) == []


# --- fetching items -------------------------------------------------------------------------------------------------

ISSUE_URL = f"https://github.com/{REPO}/issues/12"
PR_URL = f"https://github.com/{REPO}/pull/40"
LONE_PR = f"https://github.com/{REPO}/pull/41"


def search_row(number, url, title="T", pr=False, labels=(), updated=NEWER):
    return {"number": number, "url": url, "title": title, "repository": {"nameWithOwner": REPO}, "updatedAt": updated,
            "labels": [{"name": n} for n in labels], "isPullRequest": pr, "isDraft": False}


def routes(**override):
    r = {
        "gh search issues": [search_row(12, ISSUE_URL, "Fix the widget", labels=["priority: high"])],
        "gh search prs --author": [search_row(40, PR_URL, "Fix it", pr=True), search_row(41, LONE_PR, "Side change", pr=True)],
        "gh issue view": {"number": 12, "updatedAt": NEWER, "labels": [{"name": "priority: high"}], "milestone": {"dueOn": "2026-11-05T00:00:00Z"},
                          "comments": [{"id": "c1", "author": {"login": "rita"}, "createdAt": OLD, "body": "ping"},
                                       {"id": "c2", "author": {"login": "me"}, "createdAt": NEWER, "body": "on it"}]},
        "gh api graphql": {"data": {"repository": {"issue": {"closedByPullRequestsReferences": {"nodes": [{"url": PR_URL}]}}}}},
        f"gh pr view {PR_URL}": pr_json(reviewDecision="APPROVED"),
        f"gh pr view {LONE_PR}": pr_json(isDraft=True),
    }
    r.update(override)
    return r


def test_issues_are_enriched_with_comments_linked_prs_priority_and_due_date():
    items, problems = gh.fetch_items(FakeRunner(routes()), "me", [])
    assert problems == [] and [i.id for i in items] == [f"{REPO}#12", f"{REPO}#41"]
    issue = items[0]
    assert (issue.kind, issue.status, issue.priority, str(issue.due)) == ("issue", "in_review", "high", "2026-11-05")
    assert [c.author for c in issue.comments] == ["me", "rita"] and issue.comments[0].mine and not issue.comments[1].mine
    assert [p.review_state for p in issue.prs] == ["approved"]


def test_a_pr_without_an_assigned_issue_becomes_its_own_item_and_a_covered_one_does_not():
    items, _ = gh.fetch_items(FakeRunner(routes()), "me", [])
    lone = items[1]
    assert (lone.kind, lone.status, lone.title) == ("pr", "in_progress", "Side change") and lone.prs[0].draft
    assert all(i.id != f"{REPO}#40" for i in items)


def test_repo_filter_applies_to_issues_and_prs():
    items, _ = gh.fetch_items(FakeRunner(routes()), "me", ["other/repo"])
    assert items == []
    assert len(gh.fetch_items(FakeRunner(routes()), "me", [REPO])[0]) == 2


def test_an_unreadable_issue_is_reported_but_does_not_hide_the_rest():
    runner = FakeRunner(routes(**{"gh issue view": cp("", 1, "HTTP 502")}))
    items, problems = gh.fetch_items(runner, "me", [])
    assert any(i.id == f"{REPO}#12" for i in items) and "could not read details (HTTP 502)" in problems[0]


def test_failed_linked_pr_lookup_still_shows_the_issue():
    items, problems = gh.fetch_items(FakeRunner(routes(**{"gh api graphql": cp("", 1, "rate limited")})), "me", [])
    issue = items[0]
    assert issue.prs == [] and issue.status == "todo" and problems == []
    assert any(i.id == f"{REPO}#40" for i in items)  # the PR is still covered as its own item


def test_search_failure_raises_with_the_message():
    with pytest.raises(gh.GhError, match="bad credentials"):
        gh.fetch_items(FakeRunner({"gh search issues": cp("", 1, "bad credentials")}), "me", [])
    with pytest.raises(gh.GhError, match="not JSON"):
        gh.fetch_items(FakeRunner({"gh search issues": cp("<html>")}), "me", [])


def test_pr_that_cannot_be_read_still_gives_an_item_with_an_error_flag():
    runner = FakeRunner(routes(**{f"gh pr view {LONE_PR}": cp("", 1, "gone")}))
    lone = next(i for i in gh.fetch_items(runner, "me", [])[0] if i.id.endswith("#41"))
    assert lone.prs[0].error == "gone" and lone.status == "todo"


def test_issue_and_item_ids():
    assert gh.item_id("a/b", 5) == "a/b#5" and gh.split_id("a/b#5") == ("a/b", 5)


# --- incoming -------------------------------------------------------------------------------------------------------


def test_review_requests_skip_drafts_old_items_and_other_repos():
    rows = [{"url": "u1", "title": "Add thing", "repository": {"nameWithOwner": REPO}, "updatedAt": "2026-11-01T10:00:00Z",
             "author": {"login": "bob"}, "isDraft": False},
            {"url": "u2", "title": "Draft", "repository": {"nameWithOwner": REPO}, "updatedAt": "2026-11-01T10:00:00Z", "isDraft": True},
            {"url": "u3", "title": "Ancient", "repository": {"nameWithOwner": REPO}, "updatedAt": "2026-01-01T10:00:00Z", "isDraft": False},
            {"url": "u4", "title": "Elsewhere", "repository": {"nameWithOwner": "x/y"}, "updatedAt": "2026-11-01T10:00:00Z", "isDraft": False}]
    runner = FakeRunner({"gh search prs --review-requested": rows})
    got = gh.review_requests(runner, [REPO], NOW, 14)
    assert [(x.id, x.title, x.detail, x.kind) for x in got] == [("u1", f"{REPO}: Add thing", "by bob", "review_request")]
    assert len(gh.review_requests(runner, [], NOW, 14)) == 2


def test_current_user_and_errors():
    assert gh.current_user(FakeRunner({"gh api user": cp("octocat\n")})) == "octocat"
    with pytest.raises(gh.GhError, match="gh auth login"):
        gh.current_user(FakeRunner({"gh api user": cp("", 1, "")}))
    with pytest.raises(gh.GhError, match="not logged in"):
        gh.current_user(FakeRunner({"gh api user": cp("", 1, "not logged in")}))


# --- tracker --------------------------------------------------------------------------------------------------------


def tracker_runner(**override):
    r = {"gh issue view 12": {"state": "OPEN", "labels": []}, "gh api graphql": routes()["gh api graphql"],
         f"gh pr view {PR_URL}": pr_json(), "gh api repos": cp("{}"), "gh api -X": cp("{}")}
    r.update(override)
    return FakeRunner(r)


def test_tracker_status_reads_state_labels_and_linked_prs():
    t = gh.GitHubTracker(tracker_runner(), "me")
    assert t.get_status(f"{REPO}#12") == "in_review"
    assert gh.GitHubTracker(tracker_runner(**{"gh issue view 12": {"state": "CLOSED", "labels": []}}), "me").get_status(f"{REPO}#12") == "done"
    blocked = tracker_runner(**{"gh issue view 12": {"state": "OPEN", "labels": [{"name": "blocked"}]}})
    assert gh.GitHubTracker(blocked, "me").get_status(f"{REPO}#12") == "blocked"


def test_tracker_status_falls_back_to_pr_view_for_a_pr_number():
    runner = tracker_runner(**{"gh issue view 40": cp("", 1, "not an issue"),
                               "gh pr view 40": {"state": "OPEN", "labels": [], "isDraft": True}})
    assert gh.GitHubTracker(runner, "me").get_status(f"{REPO}#40") == "in_progress"
    merged = tracker_runner(**{"gh issue view 40": cp("", 1, "x"), "gh pr view 40": {"state": "MERGED", "labels": [], "isDraft": False}})
    assert gh.GitHubTracker(merged, "me").get_status(f"{REPO}#40") == "done"


def test_tracker_comment_and_transitions_use_the_rest_api():
    runner = tracker_runner()
    t = gh.GitHubTracker(runner, "me")
    t.comment(f"{REPO}#12", "hello there")
    assert runner.calls[-1] == ["gh", "api", f"repos/{REPO}/issues/12/comments", "-f", "body=hello there"]
    assert t.transition(f"{REPO}#12", "done") and runner.calls[-1][:4] == ["gh", "api", "-X", "PATCH"] and "state=closed" in runner.calls[-1]
    assert t.transition(f"{REPO}#12", "in_progress") and "labels[]=in progress" in runner.calls[-1]
    assert t.transition(f"{REPO}#12", "blocked") and "labels[]=blocked" in runner.calls[-1]
    assert t.transition(f"{REPO}#12", "in_review") is False
    assert t.available_transitions(f"{REPO}#12") == ["done", "blocked", "in_progress"]


def test_tracker_write_failures_raise():
    t = gh.GitHubTracker(tracker_runner(**{"gh api repos": cp("", 1, "HTTP 403")}), "me")
    with pytest.raises(gh.GhError, match="403"):
        t.comment(f"{REPO}#12", "x")


def test_comment_bodies_are_passed_as_one_argument_not_through_a_shell():
    runner = tracker_runner()
    gh.GitHubTracker(runner, "me").comment(f"{REPO}#12", "a; rm -rf / $(whoami) `x`")
    assert runner.calls[-1][-1] == "body=a; rm -rf / $(whoami) `x`" and len(runner.calls[-1]) == 5
