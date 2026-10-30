"""The run lock: decaf.lock in the decaf cache folder (#155).

Each `decaf run` holds a shared lock, and `decaf self-update` takes the exclusive lock,
so an update never runs under an active run. No call waits. The OS frees a lock when
its file closes, also when the process ends after a crash or a kill.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import BinaryIO

from . import engines


class LockBusy(Exception):
    """Another process holds a lock that conflicts."""


class Lock:
    """A held lock: release() closes the file, and the OS frees the lock."""

    def __init__(self, file: BinaryIO) -> None:
        self._file = file

    def release(self) -> None:
        self._file.close()


def lock_path() -> Path:
    return engines.cache_root() / "decaf.lock"


if sys.platform == "win32":
    import ctypes
    import msvcrt
    from ctypes import wintypes

    _K32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _LOCKFILE_FAIL_IMMEDIATELY = 0x1
    _LOCKFILE_EXCLUSIVE_LOCK = 0x2
    _ERROR_LOCK_VIOLATION = 33  # another handle holds a lock that conflicts

    class _Overlapped(ctypes.Structure):
        _fields_ = [
            ("Internal", ctypes.c_size_t), ("InternalHigh", ctypes.c_size_t),  # ULONG_PTR
            ("Offset", wintypes.DWORD), ("OffsetHigh", wintypes.DWORD),
            ("hEvent", wintypes.HANDLE),
        ]

    _K32.LockFileEx.restype = wintypes.BOOL
    _K32.LockFileEx.argtypes = (
        wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
        ctypes.POINTER(_Overlapped),
    )

    def _lock(file: BinaryIO, exclusive: bool) -> bool:
        # One byte at offset 0 (the zeroed OVERLAPPED). A lock past the end of the file is valid.
        flags = _LOCKFILE_FAIL_IMMEDIATELY | (_LOCKFILE_EXCLUSIVE_LOCK if exclusive else 0)
        if _K32.LockFileEx(msvcrt.get_osfhandle(file.fileno()), flags, 0, 1, 0, ctypes.byref(_Overlapped())):
            return True
        if ctypes.get_last_error() == _ERROR_LOCK_VIOLATION:
            raise LockBusy
        return False
else:
    import fcntl

    def _lock(file: BinaryIO, exclusive: bool) -> bool:
        try:
            fcntl.flock(file.fileno(), (fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH) | fcntl.LOCK_NB)
        except BlockingIOError:
            raise LockBusy from None
        except OSError:  # for example ENOLCK: a file system with no locks
            return False
        return True


def acquire(*, exclusive: bool, path: Path | None = None) -> Lock | None:
    """Take the lock with no wait.

    None: the folder or the file cannot be made or opened, or the OS cannot lock the
    file. LockBusy: another holder has a lock that conflicts.
    """
    if path is None:
        path = lock_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        file = open(path, "a+b")
    except OSError:
        return None
    try:
        held = _lock(file, exclusive)
    except LockBusy:
        file.close()
        raise
    if not held:
        file.close()
        return None
    return Lock(file)
