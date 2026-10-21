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


def test_case_groups_join_two_normal_forms():
    """Review low item: APFS ignores the normal form, so \u00e9 (NFC) and e + U+0301 (NFD) are one file there."""
    names = ["p/Caf\u00e9", "p/Cafe\u0301", "p/Cafe"]
    assert casefs.case_groups(names) == [sorted(["p/Caf\u00e9", "p/Cafe\u0301"])]


def test_fold_uses_full_case_folding():
    """ext4 casefold joins \u00df and ss, and the Kelvin sign and k: the key must join them too."""
    assert casefs.fold("Stra\u00dfe") == casefs.fold("STRASSE")
    assert casefs.fold("\u212a") == casefs.fold("k")
    assert casefs.fold("\u1f84") == casefs.fold("\u1f80\u0301")  # NFD before the folding: two forms of one letter


# Pairs that a case-insensitive file system can join: case, normal forms, and special case rules.
_PAIRS = [
    ("A", "a"), ("\u00e9", "e\u0301"), ("\u00c5", "a\u030a"), ("\u212b", "\u00e5"), ("\u00df", "ss"),
    ("\u00df", "\u1e9e"), ("\u212a", "k"), ("\u0131", "i"), ("\u0131", "I"), ("\u0130", "i"),
    ("\u017f", "s"), ("\u03c2", "\u03c3"), ("\u00b5", "\u03bc"), ("\u01c5", "\u01c6"), ("\ufb01", "fi"),
    ("\uff21", "\uff41"), ("\U00010400", "\U00010428"),
]


def test_fold_joins_each_pair_that_the_file_system_joins(tmp_path: Path):
    """Ask-box ruling "Safe key": two names that share one file get one key, so no file overwrites
    another. The CI runners check NTFS (Windows) and APFS (macOS)."""
    if not casefs.is_case_insensitive(tmp_path):
        pytest.skip("needs a case-insensitive tmp")
    rows = []
    for i, (a, b) in enumerate(_PAIRS):
        d = tmp_path / str(i)
        d.mkdir()
        (d / f"x{a}").write_bytes(b"")
        rows.append((a, b, (d / f"x{b}").exists(), casefs.fold(a) == casefs.fold(b)))
    # CI 2026-10-21: APFS (macOS runner) joins all pairs but the three with \u0131 and \u0130, as NFD and
    # full case folding do; NTFS (Windows runner) joins only A/a and the fullwidth A/a.
    assert [(a, b) for a, b, joined, folded in rows if joined and not folded] == [], rows


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
