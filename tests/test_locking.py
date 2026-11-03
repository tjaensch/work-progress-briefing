import multiprocessing
import os
import time

import pytest

from work_briefing import cli, locking


def hold(path, seconds, ready):
    with locking.file_lock(path):
        ready.set()
        time.sleep(seconds)


def test_lock_is_exclusive_across_processes_and_reports_the_holder(tmp_path):
    p = tmp_path / "x" / ".lock"
    ready = multiprocessing.Event()
    proc = multiprocessing.Process(target=hold, args=(p, 1.0, ready))
    proc.start()
    assert ready.wait(5)
    with pytest.raises(locking.LockBusy) as e:
        with locking.file_lock(p):
            pass
    assert e.value.holder == str(proc.pid)
    proc.join()
    with locking.file_lock(p):  # free again once the holder exits
        pass


def test_waiting_succeeds_when_the_holder_finishes_in_time(tmp_path):
    p = tmp_path / ".lock"
    ready = multiprocessing.Event()
    proc = multiprocessing.Process(target=hold, args=(p, 0.4, ready))
    proc.start()
    assert ready.wait(5)
    start = time.monotonic()
    with locking.file_lock(p, wait=5, poll=0.05):
        assert time.monotonic() - start >= 0.2
    proc.join()


def test_a_killed_holder_leaves_no_stale_lock(tmp_path):
    p = tmp_path / ".lock"
    ready = multiprocessing.Event()
    proc = multiprocessing.Process(target=hold, args=(p, 60, ready))
    proc.start()
    assert ready.wait(5)
    proc.kill()
    proc.join()
    with locking.file_lock(p):
        pass


def test_the_lock_file_records_our_pid(tmp_path):
    p = tmp_path / ".lock"
    with locking.file_lock(p):
        assert p.read_text() == str(os.getpid())


def test_commands_that_write_state_wait_then_report_busy(tmp_path, capsys):
    out = tmp_path / "out"
    ready = multiprocessing.Event()
    proc = multiprocessing.Process(target=hold, args=(out / cli.LOCK_FILE, 1.5, ready))
    proc.start()
    assert ready.wait(5)
    rc = cli.main(["--home", str(tmp_path), "--lock-wait", "0", "unsnooze", "x"])
    err = capsys.readouterr().err
    assert rc == cli.EXIT_BUSY and f"pid {proc.pid}" in err
    proc.join()
    assert cli.main(["--home", str(tmp_path), "unsnooze", "x"]) == 1  # lock free: runs (and reports it was not snoozed)
