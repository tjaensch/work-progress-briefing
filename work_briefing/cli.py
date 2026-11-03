"""Command line entry point."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from . import __version__
from .config import home_dir, load_config


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="work-briefing", description="Where your work stands right now.")
    ap.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    ap.add_argument("--home", default=str(home_dir()), help="state and output folder (default: $WPB_HOME or ~/.local/share/...)")
    ap.add_argument("--config", default=None, help="config file (default: <home>/config.json)")
    return ap.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    a = parse_args(argv)
    try:
        load_config(Path(a.config) if a.config else Path(a.home) / "config.json")
    except (ValueError, OSError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print("work-briefing: nothing to do yet", file=sys.stderr)
    return 0
