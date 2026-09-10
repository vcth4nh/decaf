import sys
import warnings
from pathlib import Path

import pytest

from decaf import casefs


def test_case_groups_empty_and_disjoint():
    assert casefs.case_groups([]) == []
    assert casefs.case_groups(["p/A", "p/B", "q/a"]) == []


def test_case_groups_mixed_case_and_dirs():
    names = ["p/A", "q/C", "p/a", "P/b", "p/B", "p/A$Inner"]
    assert casefs.case_groups(names) == [["p/A", "p/a"], ["P/b", "p/B"]]


def test_probe_matches_independent_check(tmp_path: Path):
    (tmp_path / "x").write_bytes(b"")
    expected = (tmp_path / "X").exists()  # True on NTFS/APFS/casefold, False on ext4
    assert casefs.is_case_insensitive(tmp_path) is expected
    assert [p.name for p in tmp_path.iterdir()] == ["x"]  # probe file cleaned up
