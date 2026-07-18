from pathlib import Path

import pytest

from decaf.scanner import (
    Artifact,
    ArtifactKind,
    ScanError,
    classify_zip,
    copy_class_tree,
    find_nested_archives,
    safe_extract_zip,
    scan_input,
)


def test_classify_archive(make_jar):
    jar = make_jar("a.jar", {"com/x/A.class": b"\xca\xfe\xba\xbe"})
    assert classify_zip(jar) == ArtifactKind.ARCHIVE


def test_classify_sources_jar(make_jar):
    jar = make_jar("a-sources.jar", {"com/x/A.java": "class A {}"})
    assert classify_zip(jar) == ArtifactKind.SOURCES_JAR


def test_classify_resource_only(make_jar):
    jar = make_jar("r.jar", {"META-INF/MANIFEST.MF": "Manifest-Version: 1.0\n"})
    assert classify_zip(jar) == ArtifactKind.RESOURCE_ONLY


def test_classify_corrupt(tmp_path: Path):
    bad = tmp_path / "bad.jar"
    bad.write_bytes(b"this is not a zip")
    assert classify_zip(bad) == ArtifactKind.CORRUPT


def test_scan_directory(make_jar, tmp_path: Path):
    make_jar("libs/a.jar", {"A.class": b"x"})
    make_jar("b.war", {"WEB-INF/classes/B.class": b"x"})
    (tmp_path / "loose/com/x").mkdir(parents=True)
    (tmp_path / "loose/com/x/C.class").write_bytes(b"x")
    (tmp_path / "notes.txt").write_text("ignore me")

    arts = scan_input(tmp_path)
    rels = [(a.rel, a.kind) for a in arts]
    assert rels == [
        ("b.war", ArtifactKind.ARCHIVE),
        ("libs/a.jar", ArtifactKind.ARCHIVE),
        ("_classes", ArtifactKind.CLASS_TREE),
    ]
    assert arts[-1].path == tmp_path


def test_scan_single_archive(make_jar):
    jar = make_jar("one.jar", {"A.class": b"x"})
    arts = scan_input(jar)
    assert [(a.rel, a.kind) for a in arts] == [("one.jar", ArtifactKind.ARCHIVE)]


def test_scan_single_non_archive(tmp_path: Path):
    f = tmp_path / "x.txt"
    f.write_text("hi")
    with pytest.raises(ScanError):
        scan_input(f)


def test_find_nested_archives():
    names = ["WEB-INF/lib/dep.jar", "WEB-INF/classes/A.class", "sub/inner.aar", "dir.jar/"]
    assert find_nested_archives(names) == ["WEB-INF/lib/dep.jar", "sub/inner.aar"]


def test_safe_extract_skips_traversal(make_jar, tmp_path: Path):
    jar = make_jar("evil.jar", {"ok.java": "class A {}", "../escape.java": "bad"})
    dest = tmp_path / "out"
    count = safe_extract_zip(jar, dest, suffixes=(".java",))
    assert count == 1
    assert (dest / "ok.java").is_file()
    assert not (tmp_path / "escape.java").exists()


def test_safe_extract_members_filter(make_jar, tmp_path: Path):
    jar = make_jar("d.zip", {"tool.jar": b"j", "README": b"r"})
    dest = tmp_path / "out"
    assert safe_extract_zip(jar, dest, members=["tool.jar"]) == 1
    assert (dest / "tool.jar").read_bytes() == b"j"


def test_copy_class_tree(tmp_path: Path):
    src = tmp_path / "src"
    (src / "com/x").mkdir(parents=True)
    (src / "com/x/A.class").write_bytes(b"a")
    (src / "com/x/notes.txt").write_text("skip")
    dest = tmp_path / "dst"
    assert copy_class_tree(src, dest) == 1
    assert (dest / "com/x/A.class").read_bytes() == b"a"
    assert not (dest / "com/x/notes.txt").exists()
