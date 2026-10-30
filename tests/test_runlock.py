import errno
import sys
import time

import pytest

from decaf import engines, runlock


def test_shared_holder_blocks_exclusive(tmp_path, lock_holder):
    path = tmp_path / "decaf.lock"
    with lock_holder(path, exclusive=False):
        with pytest.raises(runlock.LockBusy):
            runlock.acquire(exclusive=True, path=path)
    deadline = time.monotonic() + 2  # Windows can free the lock of an ended process a moment late
    while True:
        try:
            lock = runlock.acquire(exclusive=True, path=path)  # the holder ended
            break
        except runlock.LockBusy:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)
    assert lock is not None
    lock.release()


def test_exclusive_holder_blocks_shared(tmp_path, lock_holder):
    path = tmp_path / "decaf.lock"
    with lock_holder(path, exclusive=True):
        with pytest.raises(runlock.LockBusy):
            runlock.acquire(exclusive=False, path=path)


def test_two_shared_holders_coexist(tmp_path, lock_holder):
    # two runs at the same time are normal use: both get the shared lock
    path = tmp_path / "decaf.lock"
    with lock_holder(path, exclusive=False):
        lock = runlock.acquire(exclusive=False, path=path)
        assert lock is not None
        lock.release()


def test_release_frees_the_lock(tmp_path):
    path = tmp_path / "decaf.lock"
    runlock.acquire(exclusive=False, path=path).release()
    runlock.acquire(exclusive=True, path=path).release()


def test_unusable_lock_folder_gives_none(tmp_path):
    (tmp_path / "file").write_text("x")
    assert runlock.acquire(exclusive=False, path=tmp_path / "file" / "decaf.lock") is None


@pytest.mark.skipif(sys.platform == "win32", reason="flock is POSIX only")
def test_no_lock_support_gives_none(tmp_path, monkeypatch):
    # a cache folder on NFS can have no lock support (ENOLCK): acquire gives None, and the run goes on
    def no_locks(fd, op):
        raise OSError(errno.ENOLCK, "No locks available")

    monkeypatch.setattr(runlock.fcntl, "flock", no_locks)
    assert runlock.acquire(exclusive=False, path=tmp_path / "decaf.lock") is None


def test_lock_path_is_in_the_cache_root(tmp_path, monkeypatch):
    monkeypatch.setattr(engines, "cache_root", lambda: tmp_path)
    assert runlock.lock_path() == tmp_path / "decaf.lock"
