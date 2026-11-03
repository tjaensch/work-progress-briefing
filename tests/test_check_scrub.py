"""Tests for scripts/check_scrub.py. They use a made-up private list, never the real one."""
import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_scrub.py"
GENERIC = SCRIPT.with_name("scrub_generic.txt")
spec = importlib.util.spec_from_file_location("check_scrub", SCRIPT)
cs = importlib.util.module_from_spec(spec)
sys.modules["check_scrub"] = cs
spec.loader.exec_module(cs)

SECRET = "zorbaxcorp"  # stands in for an organisation term
# The samples below are built from pieces so that this file does not itself match the scrub patterns.
AT = "@"
BAD_SHAPES = [
    "/home/" + "someuser/project", "C:" + "\\Users\\someone\\x", "One" + "Drive - Org", "PROJ" + "-1234",
    "see foo.atlas" + "sian.net", "99999" + ":deadbeef-0000-1111-2222-333344445555", "mail me: a.b" + AT + "corp.example.io",
    "agency" + "." + "gov", "inter" + "nal.tools.example.io",
]


@pytest.fixture
def private(tmp_path, monkeypatch):
    p = tmp_path / "private" / "denylist.txt"
    p.parent.mkdir()
    p.write_text(f"# comment\n\n{SECRET}\nfrobnicate\\s+widgets\n")
    monkeypatch.setenv("WPB_DENYLIST", str(p))
    return p


@pytest.fixture
def repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()
    for cmd in (["init", "-q", "-b", "main"], ["config", "user.name", "tester"],
                ["config", "user.email", "1+tester@users.noreply.github.com"]):
        subprocess.run(["git", "-C", str(r), *cmd], check=True)
    return r


def commit(repo, msg="work", **env):
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "commit", "-q", "-m", msg], check=True, env={**__import__("os").environ, **env})


def check(repo, *args, capsys=None):
    rc = cs.main(["--repo", str(repo), *args])
    out = capsys.readouterr() if capsys else None
    return rc, out


def test_clean_repo_passes(repo, private, capsys):
    (repo / "a.py").write_text("print('hello DEMO-12')\n")
    rc, out = check(repo, capsys=capsys)
    assert rc == 0 and "clean" in out.out


def test_private_term_in_a_file_is_found_but_never_printed(repo, private, capsys):
    (repo / "a.py").write_text(f"x = 1\n# written at {SECRET.upper()} headquarters\n")
    rc, out = check(repo, capsys=capsys)
    assert rc == 1 and "a.py:2: private pattern #3" in out.out
    assert SECRET not in (out.out + out.err).lower()


def test_multiword_regex_and_case_insensitivity(repo, private, capsys):
    (repo / "n.md").write_text("we Frobnicate   Widgets daily\n")
    assert check(repo, capsys=capsys)[0] == 1


def test_file_names_are_checked(repo, private, capsys):
    (repo / f"{SECRET}-notes.md").write_text("fine\n")
    rc, out = check(repo, capsys=capsys)
    assert rc == 1 and "***-notes.md: private pattern" in out.out and SECRET not in out.out


def test_a_term_inside_a_file_name_is_masked_in_every_report_line(repo, private, capsys):
    (repo / f"{SECRET}.md").write_text(f"{SECRET} inside\n")
    rc, out = check(repo, capsys=capsys)
    assert rc == 1 and SECRET not in (out.out + out.err).lower() and "***.md:1: private pattern" in out.out
    commit(repo)
    rc, out = check(repo, "--history", capsys=capsys)
    assert SECRET not in (out.out + out.err).lower()


def test_untracked_files_count_but_gitignored_ones_do_not(repo, private, capsys):
    (repo / ".gitignore").write_text("local/\n")
    (repo / "local").mkdir()
    (repo / "local" / "x.txt").write_text(SECRET)
    assert check(repo, capsys=capsys)[0] == 0
    (repo / "new.txt").write_text(SECRET)  # untracked, not ignored
    assert check(repo, capsys=capsys)[0] == 1


def test_history_catches_a_term_removed_in_a_later_commit(repo, private, capsys):
    (repo / "a.txt").write_text(f"{SECRET}\n")
    commit(repo)
    (repo / "a.txt").write_text("clean\n")
    commit(repo, "remove it")
    assert check(repo, capsys=capsys)[0] == 0  # the tree is clean...
    rc, out = check(repo, "--history", capsys=capsys)
    assert rc == 1 and "history " in out.out  # ...the history is not


def test_history_checks_messages_and_author_emails(repo, private, capsys):
    (repo / "a.txt").write_text("ok\n")
    commit(repo, f"Add things for {SECRET}")
    rc, out = check(repo, "--history", capsys=capsys)
    assert rc == 1 and "message/author" in out.out
    (repo / "b.txt").write_text("ok\n")
    other = "someone" + AT + "elsewhere.org"
    commit(repo, "second", GIT_AUTHOR_EMAIL=other, GIT_COMMITTER_EMAIL=other)
    rc, out = check(repo, "--history", capsys=capsys)
    assert "generic pattern" in out.out  # a non-noreply address is flagged by the generic list


def test_history_checks_other_branches_and_file_names(repo, private, capsys):
    (repo / "a.txt").write_text("ok\n")
    commit(repo)
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "-b", "side"], check=True)
    (repo / f"{SECRET}.txt").write_text("x\n")
    commit(repo, "side work")
    subprocess.run(["git", "-C", str(repo), "checkout", "-q", "main"], check=True)
    rc, out = check(repo, "--history", capsys=capsys)
    assert rc == 1 and "file" in out.out


def test_empty_repo_history_scan_is_fine(repo, private, capsys):
    assert check(repo, "--history", capsys=capsys)[0] == 0


@pytest.mark.parametrize("text", BAD_SHAPES)
def test_generic_patterns_flag_these_shapes(repo, capsys, text):
    (repo / "a.txt").write_text(text + "\n")
    rc, out = check(repo, "--generic-only", capsys=capsys)
    assert rc == 1 and "generic pattern" in out.out


@pytest.mark.parametrize("text", [
    "DEMO-1 and DEMO-123", "UTF-8, SHA-256, ISO-8601, RFC-2119, CVE-2024-1", "me" + AT + "users.noreply.github.com",
    "you" + AT + "example.com", "plain prose with numbers 42 and 2026-11-03",
    "uuid 0f3a9c1e-0000-4abc-9def-123456789abc and lowercase proj-1234 are fine",
])
def test_generic_patterns_allow_these(repo, capsys, text):
    (repo / "a.txt").write_text(text + "\n")
    assert check(repo, "--generic-only", capsys=capsys)[0] == 0


def test_generic_only_ignores_the_private_list(repo, private, capsys):
    (repo / "a.txt").write_text(SECRET)
    assert check(repo, "--generic-only", capsys=capsys)[0] == 0


def test_missing_private_list_is_an_error_when_strict_and_a_warning_when_lenient(repo, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("WPB_DENYLIST", str(tmp_path / "nope.txt"))
    rc, out = check(repo, capsys=capsys)
    assert rc == 2 and "private denylist not found" in out.err
    rc, out = check(repo, "--lenient", capsys=capsys)
    assert rc == 0 and "warning" in out.err


def test_bad_regex_in_the_private_list_is_a_setup_error(repo, private, capsys):
    private.write_text("([unclosed\n")
    rc, out = check(repo, capsys=capsys)
    assert rc == 2 and "bad regex" in out.err and "unclosed" not in out.err


def test_path_mode_scans_only_the_given_files(repo, private, capsys, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "article.md").write_text(f"about {SECRET}\n")
    (tmp_path / "ok.md").write_text("fine\n")
    assert check(repo, "--path", "ok.md", capsys=capsys)[0] == 0
    assert check(repo, "--path", "article.md", "ok.md", capsys=capsys)[0] == 1


def test_binary_files_are_reported_as_not_scanned(repo, private, capsys):
    (repo / "img.png").write_bytes(b"\x89PNG\0\0" + SECRET.encode())
    rc, out = check(repo, capsys=capsys)
    assert rc == 0 and "img.png is binary" in out.err


def test_the_generic_list_file_is_checked_against_the_private_list_only(repo, private, tmp_path, capsys):
    (repo / "scripts").mkdir()
    (repo / "scripts" / "scrub_generic.txt").write_text("one" + "drive\n/ho" + "me/[a-z]+/\n")  # matches itself: allowed
    assert check(repo, capsys=capsys)[0] == 0
    (repo / "scripts" / "scrub_generic.txt").write_text(f"one" + f"drive\n{SECRET}\n")  # but an org term in it is caught
    assert check(repo, capsys=capsys)[0] == 1


def test_the_committed_generic_list_loads_and_has_no_org_specific_terms():
    pats = cs.load_patterns(GENERIC, private=False)
    assert len(pats) >= 8
    assert all(p.regex.search("a perfectly ordinary sentence") is None for p in pats)


def test_hooks_run_the_scanner_and_are_executable():
    root = SCRIPT.parent.parent
    for name, flag in (("pre-commit", ""), ("pre-push", "--history")):
        hook = root / ".githooks" / name
        assert hook.stat().st_mode & 0o111 and "check_scrub.py" in hook.read_text()
        assert flag in hook.read_text()


def test_history_scan_survives_the_scanners_own_marker_text_and_decorators(repo, private, capsys):
    (repo / "tool.py").write_text("\n".join(['marker = "@@COMMIT@@"', "@pytest.fixture", "def f(): pass", ""]))
    commit(repo)
    rc, out = check(repo, "--history", capsys=capsys)
    assert rc == 0, out.out


def test_history_findings_carry_the_right_commit_and_line_context(repo, private, capsys):
    (repo / "a.txt").write_text("fine\n")
    commit(repo, "one")
    (repo / "b.txt").write_text(f"{SECRET}\n")
    commit(repo, "two")
    git_out = lambda *a: subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True).stdout  # noqa: E731
    second, first = git_out("rev-parse", "HEAD")[:10], git_out("rev-parse", "HEAD~1")[:10]
    rc, out = check(repo, "--history", capsys=capsys)
    assert f"history {second}:" in out.out and first not in out.out
