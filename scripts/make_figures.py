#!/usr/bin/env python3
"""Regenerate the article's figures from the demo, so they always match what the tool really prints.

Needs Pillow, which is not a dependency of the project:  python3 -m venv /tmp/fig && /tmp/fig/bin/pip install pillow
Run:  /tmp/fig/bin/python scripts/make_figures.py

Every figure is rendered from fictional demo output (fixed clock, invented names). No prompt, path, user name or window
title from the machine it runs on is drawn, and the PNGs carry no metadata."""
from __future__ import annotations

import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "article" / "figures"
FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
SANS = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
SANS_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
BG, FG, DIM, ACCENT, WARN, OK = "#1e2127", "#d7dae0", "#7f848e", "#61afef", "#e5c07b", "#98c379"
SIZE, LINE, PAD = 17, 25, 24


def run(args: list[str], stdin: str = "") -> str:
    p = subprocess.run([sys.executable, "-m", "work_briefing", *args], cwd=ROOT, input=stdin, capture_output=True, text=True)
    return p.stdout


def demo_home() -> str:
    tmp = tempfile.mkdtemp(prefix="wpb-fig-")
    run(["--home", tmp, "demo"])
    return str(Path(tmp) / "demo")


def section(md: str, heading: str, stop: int = 2) -> list[str]:
    """Lines from `heading` up to the next heading of the same or higher level."""
    lines = md.splitlines()
    start = lines.index(heading)
    out = [lines[start]]
    for line in lines[start + 1:]:
        if line.startswith("## ") and stop >= 2:
            break
        out.append(line)
    return out


def tidy(lines: list[str]) -> list[str]:
    """Plain terminal look: drop markdown bold and trailing double-space line breaks."""
    return [l.replace("**", "").rstrip() for l in lines]


def terminal(lines: list[str], path: Path, title: str, width_chars: int | None = None) -> None:
    font, bold = ImageFont.truetype(FONT, SIZE), ImageFont.truetype(FONT_BOLD, SIZE)
    width_chars = width_chars or max(len(l) for l in lines)
    w, h = int(width_chars * 10.3) + 2 * PAD, len(lines) * LINE + 2 * PAD + 34
    img = Image.new("RGB", (w, h), BG)
    d = ImageDraw.Draw(img)
    d.rectangle([0, 0, w, 34], fill="#2c313a")
    for i, c in enumerate(("#ff5f56", "#ffbd2e", "#27c93f")):
        d.ellipse([16 + i * 22, 11, 28 + i * 22, 23], fill=c)
    d.text((w // 2, 17), title, font=ImageFont.truetype(SANS, 14), fill=DIM, anchor="mm")
    y = 34 + PAD
    for line in lines:
        color, f = FG, font
        if line.startswith("$ "):
            color, f = OK, bold
        elif line.startswith("#"):
            color, f = ACCENT, bold
        elif "needs you" in line or line.lstrip().startswith("!"):
            color = WARN
        elif line.lstrip().startswith(">") or line.lstrip().startswith("Why:") or line.lstrip().startswith("Next:"):
            color = DIM if line.lstrip().startswith("Why:") else FG
        d.text((PAD, y), line, font=f, fill=color)
        y += LINE
    img.save(path, optimize=True)  # no pnginfo: nothing but pixels


def boxes(path: Path) -> None:
    """The one diagram: the main pipeline on top, then two lanes that branch off "Classify"."""
    w, h = 1580, 600
    img = Image.new("RGB", (w, h), "#ffffff")
    d = ImageDraw.Draw(img)
    fb, fs = ImageFont.truetype(SANS_BOLD, 22), ImageFont.truetype(SANS, 17)
    INK, LINE_C = "#2e3440", "#4c566a"

    def box(x, y, bw, bh, title, sub, fill):
        d.rounded_rectangle([x, y, x + bw, y + bh], radius=14, fill=fill, outline="#3b4252", width=2)
        d.text((x + bw / 2, y + bh / 2 - 14), title, font=fb, fill=INK, anchor="mm")
        d.text((x + bw / 2, y + bh / 2 + 16), sub, font=fs, fill=LINE_C, anchor="mm")

    def right(x1, x2, y):
        d.line([x1, y, x2, y], fill=LINE_C, width=3)
        d.polygon([(x2, y), (x2 - 12, y - 7), (x2 - 12, y + 7)], fill=LINE_C)

    bh, y = 110, 80
    steps = [("Sources", "issues, PRs, requests", "#e5f0ff"), ("Normalise", "one status vocabulary", "#e5f0ff"),
             ("Classify", "bucket, next action", "#e8f6e8"), ("Snooze", "until a date or a change", "#e8f6e8"),
             ("Score", "points have reasons", "#e8f6e8"), ("Plan", "top N incl. requests", "#fff3d6")]
    xs = [30 + i * 235 for i in range(6)]
    for x, (t, sub, c) in zip(xs, steps):
        box(x, y, 215, bh, t, sub, c)
    for a, b in zip(xs, xs[1:]):
        right(a + 219, b - 4, y + bh // 2)

    trunk_x = 580  # left edge of the lane boxes
    tx = xs[2] + 30  # where the branch leaves "Classify"
    d.line([tx, y + bh + 4, tx, 258], fill=LINE_C, width=3)
    lane_a, lane_b = 240, 430
    for ly, label in ((lane_a, "It remembers"), (lane_b, "It never writes by itself")):
        d.text((30, ly + bh // 2 - 14), label, font=fb, fill=INK, anchor="lm")
    d.line([tx, 258, tx, lane_b + bh // 2], fill=LINE_C, width=3)
    right(tx, trunk_x - 4, lane_a + bh // 2)
    right(tx, trunk_x - 4, lane_b + bh // 2)

    lane = [(trunk_x, 190, "Snapshot", "saved every run", "#f3e8ff"), (trunk_x + 230, 200, "Diff", "against the last run", "#f3e8ff"),
            (trunk_x + 470, 270, "Since your last check", "needs you / progress", "#fff3d6"),
            (trunk_x + 780, 190, "Reports", "stand-up, eod, week", "#fff3d6")]
    for x, bw, t, sub, c in lane:
        box(x, lane_a, bw, bh, t, sub, c)
    for (x1, w1, *_), (x2, *_r) in zip(lane, lane[1:]):
        right(x1 + w1 + 4, x2 - 4, lane_a + bh // 2)
    drafts = [(trunk_x, 190, "Drafts", "plain-code text", "#ffe8e8"), (trunk_x + 230, 300, "Preview + confirm", "status re-checked first", "#ffe8e8"),
              (trunk_x + 570, 200, "Post", "only after a yes", "#ffe8e8")]
    for x, bw, t, sub, c in drafts:
        box(x, lane_b, bw, bh, t, sub, c)
    for (x1, w1, *_), (x2, *_r) in zip(drafts, drafts[1:]):
        right(x1 + w1 + 4, x2 - 4, lane_b + bh // 2)
    d.text((w // 2, 30), "What happens on every refresh", font=fb, fill=INK, anchor="mm")
    img.save(path, optimize=True)


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    home = demo_home()
    md = (Path(home) / "out" / "latest.md").read_text()

    boxes(OUT / "fig1-flow.png")

    plan = tidy(section(md, "## Today's plan"))[:17]
    terminal(["$ work-briefing", "", *plan], OUT / "fig2-plan.png", "work-briefing: today's plan")

    since = tidy(section(md, "## Since your last check (18h ago)"))[:10]
    progress = tidy(section(md, "## Progress today"))[:3]
    terminal(["$ work-briefing", "", *since, "", *progress], OUT / "fig3-since-last-check.png", "work-briefing: what changed")

    stale = "acme-labs/widget-api#21:status_update"
    raw = run(["--home", home, "apply", stale], stdin="n\n").replace("Post this? [y/N] Cancelled", "Post this? [y/N] n\nCancelled")
    preview = [part for line in raw.splitlines() for part in (textwrap.wrap(line, 104, subsequent_indent="  ") or [""])]
    terminal([f"$ work-briefing apply {stale}", "", *preview], OUT / "fig4-apply-preview.png", "work-briefing: apply asks first")

    week = tidy(run(["--home", home, "report", "week"]).splitlines())
    terminal(["$ work-briefing report week", "", *week[:20]], OUT / "fig5-week.png", "work-briefing: weekly report")
    print("wrote", ", ".join(sorted(p.name for p in OUT.glob("*.png"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
