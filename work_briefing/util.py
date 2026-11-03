"""Small helpers: time parsing and wording, safe file writes, secret redaction, running commands."""
from __future__ import annotations

import os
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

Runner = Callable[..., subprocess.CompletedProcess]

_TZ_NO_COLON = re.compile(r"([+-]\d\d)(\d\d)$")


def parse_ts(s: str) -> datetime:
    """Parse ISO timestamps (`...Z`, `+0100`, `+01:00`, or none) into an aware datetime; naive means UTC."""
    s = s.strip()
    s = s[:-1] + "+00:00" if s[-1:] in ("Z", "z") else _TZ_NO_COLON.sub(r"\1:\2", s)
    dt = datetime.fromisoformat(s)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ago(d: datetime, now: datetime) -> str:
    days = (now - d).days
    return "today" if days <= 0 else f"{days}d ago"


def since_text(d: datetime, now: datetime) -> str:
    """'just now', '5 min ago', '2h ago', '3d ago'."""
    mins = int((now - d).total_seconds() // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return f"{mins} min ago"
    if mins < 60 * 24:
        return f"{mins // 60}h ago"
    return f"{mins // (60 * 24)}d ago"


def squash(text: str) -> str:
    return " ".join(text.split())


def clip(text: str, n: int) -> str:
    """Shorten to n characters at a word boundary, with an ellipsis."""
    text = squash(text)
    if len(text) <= n:
        return text
    cut = text[: n - 1].rsplit(" ", 1)[0] or text[: n - 1]
    return cut.rstrip(".,;: ") + "…"


def atomic_write(path: Path, text: str) -> None:
    """Write via a temp file and rename, so a crash or a concurrent reader never sees a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(text)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


SECRET_PATTERNS = [
    re.compile(r"gh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"(?i)\b(?:token|secret|password|passwd|api[_-]?key)\b\s*[:=]\s*\S+"),
    re.compile(r"(?i)bearer\s+[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
]


def redact(text: str) -> str:
    for p in SECRET_PATTERNS:
        text = p.sub("[redacted]", text)
    return text


def run_cmd(cmd: list[str], timeout: float = 60, cwd: str | None = None) -> subprocess.CompletedProcess:
    """Run a command without a shell. A missing program or a timeout becomes a non-zero result, not an exception."""
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
    except FileNotFoundError:
        return subprocess.CompletedProcess(cmd, 127, "", f"{cmd[0]}: not found")
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(cmd, 124, "", f"timed out after {timeout}s")
