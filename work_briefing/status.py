"""A failed refresh must be visible: `status.json` records it, the briefing files get a banner, and a notice is sent once
per failing streak (not on every retry)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from .util import atomic_write, redact

FILE = "status.json"
PERMANENT = ("not logged in", "authentication", "gh: not found", "command not found")


def _path(out_dir: Path) -> Path:
    return out_dir / FILE


def load(out_dir: Path) -> dict:
    try:
        data = json.loads(_path(out_dir).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(out_dir: Path, st: dict) -> None:
    atomic_write(_path(out_dir), json.dumps(st, indent=1))


def describe_error(e: BaseException) -> str:
    """One redacted line: enough to act on, never a secret."""
    return redact(f"{type(e).__name__}: {' '.join(str(e).split())}")[:300]


def is_permanent(message: str) -> bool:
    """Errors that retrying cannot fix."""
    return any(p in message.lower() for p in PERMANENT)


def record_success(out_dir: Path, now: datetime) -> None:
    prev = load(out_dir)
    _save(out_dir, {"last_attempt": now.isoformat(), "last_success": now.isoformat(), "last_error": "",
                    "consecutive_failures": 0, "failure_notified": False, "previous_failures": prev.get("consecutive_failures", 0)})
    clear_banner(out_dir)


def record_failure(out_dir: Path, message: str, now: datetime) -> dict:
    prev = load(out_dir)
    st = {"last_attempt": now.isoformat(), "last_success": prev.get("last_success", ""), "last_error": message,
          "consecutive_failures": int(prev.get("consecutive_failures", 0)) + 1,
          "failure_notified": bool(prev.get("failure_notified", False))}
    _save(out_dir, st)
    add_banner(out_dir, st)
    return st


def banner_text(st: dict) -> str:
    since = f"The data below is from {st['last_success'][:16].replace('T', ' ')}." if st.get("last_success") else "There is no earlier briefing."
    return (f"> **The last refresh failed** ({st['consecutive_failures']} in a row): {st['last_error']}\n> {since}\n\n")


MARK = "<!-- wpb-banner -->"


def add_banner(out_dir: Path, st: dict) -> None:
    """Prepend a banner to latest.md (replacing an older one) so an open editor tab shows the problem."""
    p = out_dir / "latest.md"
    if not p.exists():
        return
    body = _strip(p.read_text())
    atomic_write(p, f"{MARK}\n{banner_text(st)}{MARK}\n{body}")


def clear_banner(out_dir: Path) -> None:
    p = out_dir / "latest.md"
    if p.exists() and MARK in p.read_text():
        atomic_write(p, _strip(p.read_text()))


def _strip(text: str) -> str:
    if text.startswith(MARK):
        end = text.find(MARK, len(MARK))
        if end != -1:
            return text[end + len(MARK):].lstrip("\n")
    return text


def should_notify(out_dir: Path, threshold: int = 1) -> bool:
    """True once per failing streak, after `threshold` failures in a row."""
    st = load(out_dir)
    return st.get("consecutive_failures", 0) >= threshold and not st.get("failure_notified", False)


def mark_notified(out_dir: Path) -> None:
    st = load(out_dir)
    st["failure_notified"] = True
    _save(out_dir, st)
