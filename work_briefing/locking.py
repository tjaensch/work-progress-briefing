"""A lock that dies with its process, so a crash never leaves a stale lock behind (flock, not a lock file's existence)."""
from __future__ import annotations

import fcntl
import os
import time
from contextlib import contextmanager
from pathlib import Path


class LockBusy(Exception):
    def __init__(self, holder: str):
        super().__init__(f"locked by pid {holder}" if holder else "locked")
        self.holder = holder


@contextmanager
def file_lock(path: Path, wait: float = 0, poll: float = 0.1):
    """Hold an exclusive lock on `path` (created if needed). Waits up to `wait` seconds, then raises LockBusy."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o644)
    deadline = time.monotonic() + wait
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise LockBusy(os.pread(fd, 32, 0).decode(errors="replace").strip()) from None
                time.sleep(poll)
        os.ftruncate(fd, 0)
        os.pwrite(fd, str(os.getpid()).encode(), 0)
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock
