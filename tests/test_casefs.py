import os
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


@pytest.mark.skipif(sys.platform == "win32", reason="Windows sets the real flag")
def test_enable_is_noop_off_windows(tmp_path: Path):
    d = tmp_path / "root"
    d.mkdir()
    assert casefs.enable_case_sensitive(d) is False
    assert list(d.iterdir()) == []


@pytest.mark.skipif(sys.platform != "win32", reason="NTFS per-directory flag is Windows-only")
def test_enable_on_windows_consistent_with_probe(tmp_path: Path):
    d = tmp_path / "root"
    d.mkdir()
    enabled = casefs.enable_case_sensitive(d)
    # Whether or not the runner has the feature gate, the probe must agree with the result.
    assert casefs.is_case_insensitive(d) is (not enabled)
    if not enabled:
        warnings.warn(
            "NTFS case-sensitivity flag not set on this runner "
            f"(Win32 error {casefs._set_case_sensitive_flag(d)})",
            stacklevel=1,
        )


def test_prepare_roots_labels(tmp_path: Path, monkeypatch):
    out, tmp = tmp_path / "out", tmp_path / "tmp"
    enabled_on: list[Path] = []
    monkeypatch.setattr(casefs, "enable_case_sensitive", lambda d: enabled_on.append(d) or False)

    def probe(insensitive: set[Path]):
        return lambda d: d in insensitive

    monkeypatch.setattr(casefs, "is_case_insensitive", probe(set()))
    assert casefs.prepare_roots(out, tmp) is None
    assert out.is_dir() and tmp.is_dir()
    assert enabled_on == [out, tmp]  # flag attempted before the probe, on both roots
    monkeypatch.setattr(casefs, "is_case_insensitive", probe({out}))
    assert casefs.prepare_roots(out, tmp) == "output directory"
    monkeypatch.setattr(casefs, "is_case_insensitive", probe({tmp}))
    assert casefs.prepare_roots(out, tmp) == "temp directory"
    monkeypatch.setattr(casefs, "is_case_insensitive", probe({out, tmp}))
    assert casefs.prepare_roots(out, tmp) == "output and temp directories"


@pytest.mark.skipif(sys.platform == "win32" or os.geteuid() == 0, reason="needs POSIX permissions as non-root")
def test_probe_error_names_the_directory(tmp_path: Path):
    d = tmp_path / "ro"
    d.mkdir()
    d.chmod(0o500)
    try:
        with pytest.raises(OSError) as info:
            casefs.is_case_insensitive(d)
        assert info.value.filename == str(d)  # the directory, not the hidden probe file
    finally:
        d.chmod(0o700)
