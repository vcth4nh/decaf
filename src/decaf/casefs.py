"""Case-(in)sensitivity of output trees: probe, Windows NTFS flag, name grouping (#93).

NTFS and default APFS ignore case but keep the case of the first write, so two
classes that differ only by case collapse into one file. This module answers
"does this directory ignore case?", tries to switch that off on Windows, and
finds the names that would collide.
"""

from __future__ import annotations

import ctypes
import sys
import uuid
from collections import defaultdict
from collections.abc import Iterable
from pathlib import Path


def case_groups(names: Iterable[str]) -> list[list[str]]:
    """Names that would share one path on a case-insensitive tree, grouped and sorted."""
    by_fold: dict[str, set[str]] = defaultdict(set)
    for name in names:
        by_fold[name.casefold()].add(name)
    return [sorted(group) for _, group in sorted(by_fold.items()) if len(group) > 1]


def is_case_insensitive(directory: Path) -> bool:
    """Probe: does a case-swapped name resolve to a file just created in ``directory``?

    OSError propagates: a root decaf cannot write to fails the run anyway.
    """
    marker = directory / f".decaf-CaseProbe-{uuid.uuid4().hex}"
    marker.write_bytes(b"")
    try:
        return (directory / marker.name.swapcase()).exists()
    finally:
        marker.unlink(missing_ok=True)


# Win32 constants for the per-directory NTFS case-sensitivity flag (Windows 10 1803+).
_FILE_WRITE_ATTRIBUTES = 0x100
_FILE_SHARE_ALL = 0x7  # READ | WRITE | DELETE
_OPEN_EXISTING = 3
_FILE_FLAG_BACKUP_SEMANTICS = 0x02000000  # needed to open a directory handle
_INVALID_HANDLE_VALUE = ctypes.c_void_p(-1).value
_FILE_CASE_SENSITIVE_INFO = 23  # FILE_INFO_BY_HANDLE_CLASS::FileCaseSensitiveInfo
_FILE_CS_FLAG_CASE_SENSITIVE_DIR = 0x1


def _set_case_sensitive_flag(directory: Path) -> int:
    """Win32 only: 0 on success, else the GetLastError code (-1 when unavailable). Never raises.

    The kernel wants an empty local NTFS directory, write-attributes plus
    add/delete-child rights (no admin), and the feature gate a WSL install sets.
    """
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)  # type: ignore[attr-defined]
        k32.CreateFileW.argtypes = [
            ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
            ctypes.c_uint32, ctypes.c_uint32, ctypes.c_void_p,
        ]
        k32.CreateFileW.restype = ctypes.c_void_p
        k32.SetFileInformationByHandle.argtypes = [
            ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
        ]
        k32.SetFileInformationByHandle.restype = ctypes.c_int
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k32.CreateFileW(
            str(directory), _FILE_WRITE_ATTRIBUTES, _FILE_SHARE_ALL, None,
            _OPEN_EXISTING, _FILE_FLAG_BACKUP_SEMANTICS, None,
        )
        if handle is None or handle == _INVALID_HANDLE_VALUE:
            return ctypes.get_last_error() or -1
        try:
            info = ctypes.c_ulong(_FILE_CS_FLAG_CASE_SENSITIVE_DIR)
            ok = k32.SetFileInformationByHandle(
                handle, _FILE_CASE_SENSITIVE_INFO, ctypes.byref(info), ctypes.sizeof(info)
            )
            return 0 if ok else (ctypes.get_last_error() or -1)
        finally:
            k32.CloseHandle(handle)
    except (AttributeError, OSError, ctypes.ArgumentError):
        return -1


def enable_case_sensitive(directory: Path) -> bool:
    """Best effort: make ``directory`` case-sensitive on Windows NTFS. False elsewhere/on failure."""
    if sys.platform != "win32":
        return False
    return _set_case_sensitive_flag(directory) == 0


def prepare_roots(output: Path, tmp_root: Path) -> str | None:
    """Create both roots, try the Windows flag, then probe each.

    Returns a label naming the roots that still ignore case, or None. The flag
    is attempted before the probe file exists because it needs an empty directory.
    """
    ignoring: list[str] = []
    for root, label in ((output, "output directory"), (tmp_root, "temp directory")):
        root.mkdir(parents=True, exist_ok=True)
        enable_case_sensitive(root)  # result ignored on purpose: the probe decides
        if is_case_insensitive(root):
            ignoring.append(label)
    if not ignoring:
        return None
    return "output and temp directories" if len(ignoring) == 2 else ignoring[0]
