"""Writing briefing files and cleaning up old ones."""
from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path

from .models import Briefing
from .render import render_markdown, to_json
from .util import atomic_write


def write_outputs(b: Briefing, out_dir: Path) -> tuple[Path, Path]:
    """briefing-DATE.md/.json plus latest.md/.json; all written atomically."""
    md, js = render_markdown(b), json.dumps(to_json(b), indent=1)
    md_path, json_path = out_dir / f"briefing-{b.now:%Y-%m-%d}.md", out_dir / f"briefing-{b.now:%Y-%m-%d}.json"
    for path, text in ((md_path, md), (json_path, js), (out_dir / "latest.md", md), (out_dir / "latest.json", js)):
        atomic_write(path, text)
    return md_path, json_path


def prune_old(out_dir: Path, keep_days: int, now: datetime | None = None) -> None:
    """Delete dated briefings, drafts and reports older than keep_days."""
    cutoff = (now or datetime.now()).date() - timedelta(days=keep_days)
    for pattern in ("briefing-????-??-??.*", "drafts-????-??-??.json", "drafts-eod-????-??-??.json", "report-*-????-??-??.md"):
        for p in out_dir.glob(pattern):
            try:
                if datetime.strptime("-".join(p.stem.split("-")[-3:]), "%Y-%m-%d").date() < cutoff:
                    p.unlink()
            except ValueError:
                pass
