#!/usr/bin/env python3
"""Fail if anything that must not be public is in this repo: files, file names, commit messages, authors or history.

Two pattern lists, both one case-insensitive regex per line (blank lines and `#` comments are ignored):

* generic patterns, committed next to this script (`scrub_generic.txt`): shapes only, no organisation terms;
* a private denylist that is never committed: `$WPB_DENYLIST`, else `~/.config/work-progress-briefing/denylist.txt`.

The text that matched a *private* pattern is never printed (CI logs of a public repo are public): only the place and
the pattern's number. Exit codes: 0 clean, 1 findings, 2 setup problem (for example a missing private list in strict mode).

    check_scrub.py                  working tree (tracked and untracked, not ignored) and file names
    check_scrub.py --history        also every commit message, author, committer and patch on all branches
    check_scrub.py --path FILE...   scan only these files (for the article and its alt text)
    check_scrub.py --generic-only   skip the private list (what CI does)
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
GENERIC_FILE = HERE / "scrub_generic.txt"
DEFAULT_PRIVATE = Path.home() / ".config" / "work-progress-briefing" / "denylist.txt"
MAX_BYTES = 2_000_000
# The generic pattern list necessarily contains text that matches itself; it is checked against the private list only.
SELF = "scripts/scrub_generic.txt"


@dataclass(frozen=True)
class Pattern:
    regex: re.Pattern
    number: int
    private: bool


@dataclass(frozen=True)
class Finding:
    where: str
    line: int | None
    pattern: Pattern
    shown: str

    def render(self, private_patterns: list[Pattern] | None = None) -> str:
        where = self.where
        for p in private_patterns or []:  # a file name can contain the term too; mask it like any other private match
            where = p.regex.sub("***", where)
        loc = f"{where}:{self.line}" if self.line else where
        tag = f"private pattern #{self.pattern.number}" if self.pattern.private else f"generic pattern #{self.pattern.number}"
        return f"{loc}: {tag}" + ("" if self.pattern.private else f"  ({self.shown})")


class SetupError(Exception):
    pass


def load_patterns(path: Path, private: bool) -> list[Pattern]:
    out = []
    for n, raw in enumerate(path.read_text().splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        try:
            out.append(Pattern(re.compile(line, re.IGNORECASE), n, private))
        except re.error as e:
            raise SetupError(f"{path.name} line {n}: bad regex ({e})") from None
    return out


def private_path() -> Path:
    return Path(os.environ.get("WPB_DENYLIST") or DEFAULT_PRIVATE)


def gather(generic_only: bool, strict: bool, generic_file: Path = GENERIC_FILE) -> list[Pattern]:
    patterns = load_patterns(generic_file, private=False) if generic_file.exists() else []
    if generic_only:
        return patterns
    p = private_path()
    if not p.is_file():
        if strict:
            raise SetupError(f"private denylist not found at {p}. Create it (see the plan), set WPB_DENYLIST, "
                             "or pass --generic-only / --lenient to run without it.")
        print(f"warning: private denylist not found at {p}; only generic patterns were checked", file=sys.stderr)
        return patterns
    return patterns + load_patterns(p, private=True)


def scan_text(where: str, text: str, patterns: list[Pattern]) -> list[Finding]:
    found = []
    for i, line in enumerate(text.splitlines(), 1):
        for pat in patterns:
            if (m := pat.regex.search(line)):
                found.append(Finding(where, i, pat, m.group(0)))
    return found


def scan_name(name: str, patterns: list[Pattern]) -> list[Finding]:
    return [Finding(name, None, p, m.group(0)) for p in patterns if (m := p.regex.search(name))]


def git(repo: Path, *args: str) -> str:
    r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, errors="replace")
    if r.returncode != 0:
        raise SetupError(f"git {' '.join(args[:2])} failed: {r.stderr.strip()[:200]}")
    return r.stdout


def tree_files(repo: Path) -> list[str]:
    out = git(repo, "ls-files", "-z", "--cached", "--others", "--exclude-standard")
    return sorted({f for f in out.split("\0") if f and (repo / f).is_file()})


def read_text(path: Path) -> str | None:
    """None for binary or oversized files (reported by the caller, never silently treated as clean)."""
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) > MAX_BYTES or b"\0" in data:
        return None
    return data.decode("utf-8", errors="replace")


def patterns_for(path: str, patterns: list[Pattern]) -> list[Pattern]:
    return [p for p in patterns if p.private] if path.replace("\\", "/").endswith(SELF) else patterns


def scan_files(repo: Path, files: list[str], patterns: list[Pattern]) -> tuple[list[Finding], list[str]]:
    found, skipped = [], []
    for f in files:
        use = patterns_for(f, patterns)
        found += scan_name(f, use)
        text = read_text(repo / f)
        if text is None:
            skipped.append(f)
            continue
        found += scan_text(f, text, use)
    return found, skipped


def scan_history(repo: Path, patterns: list[Pattern]) -> list[Finding]:
    found = []
    meta = git(repo, "log", "--all", "--format=%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%B%x1e")
    for rec in meta.split("\x1e"):
        parts = rec.strip("\n").split("\x1f")
        if len(parts) < 6:
            continue
        found += scan_text(f"commit {parts[0][:10]} (message/author)", "\n".join(parts[1:]), patterns)
    marker = "@@COMMIT@@"  # a patch line starts with +, -, space or an @@ hunk header followed by a space, never with this
    patch = git(repo, "log", "--all", "-p", "--no-color", f"--format={marker}%H")
    sha, use, block = "", patterns, []

    def flush():
        if block:
            found.extend(scan_text(f"history {sha[:10]}", "\n".join(block), use))

    for line in patch.splitlines():
        if line.startswith(marker):
            flush()
            sha, use, block = line[len(marker):], patterns, []
        elif line.startswith("diff --git "):
            flush()
            path = line.rsplit(" b/", 1)[-1]
            use, block = patterns_for(path, patterns), []
            found.extend(scan_name(f"history {sha[:10]} file {path}", use))
        else:
            block.append(line)
    flush()
    return found


def _has_commits(repo: Path) -> bool:
    r = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", "-q", "HEAD"], capture_output=True)
    return r.returncode == 0


def run(repo: Path, patterns: list[Pattern], history: bool, paths: list[str] | None, out=None) -> int:
    out = out or sys.stdout
    if paths:
        files = [str(Path(p)) for p in paths]
        found, skipped = scan_files(Path("."), files, patterns)
    else:
        found, skipped = scan_files(repo, tree_files(repo), patterns)
    if history and _has_commits(repo):
        found += scan_history(repo, patterns)
    private = [p for p in patterns if p.private]
    for f in found:
        print(f.render(private), file=out)
    for s in skipped:
        print(f"note: {s} is binary or too large and was not text-scanned (check images and metadata by hand)", file=sys.stderr)
    if found:
        print(f"\n{len(found)} finding(s). Fix them, and if one is already committed, rewrite history before pushing.", file=out)
        return 1
    print("scrub check clean" + (" (tree and history)" if history else " (tree)"), file=out)
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--history", action="store_true", help="also scan every commit (messages, authors, patches)")
    ap.add_argument("--path", nargs="+", metavar="FILE", help="scan only these files")
    ap.add_argument("--generic-only", action="store_true", help="skip the private denylist (CI)")
    ap.add_argument("--lenient", action="store_true", help="warn instead of failing when the private denylist is missing")
    ap.add_argument("--repo", default=str(HERE.parent), help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    try:
        patterns = gather(a.generic_only, strict=not a.lenient)
        return run(Path(a.repo), patterns, a.history, a.path)
    except SetupError as e:
        print(f"setup problem: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
