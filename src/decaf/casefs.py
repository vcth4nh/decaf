"""Case-(in)sensitivity of output trees: probe, Windows NTFS flag, name grouping (#93).

NTFS and default APFS ignore case but keep the case of the first write, so two
classes that differ only by case collapse into one file. This module answers
"does this directory ignore case?", tries to switch that off on Windows, and
finds the names that would collide.
"""

from __future__ import annotations

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
