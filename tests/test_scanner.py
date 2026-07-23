from pathlib import Path

import pytest

from decaf.scanner import (
    Artifact,
    ArtifactKind,
    ScanError,
    classify_counted,
    classify_zip,
    copy_class_tree,
    find_nested_archives,
    safe_extract_zip,
    scan_counted,
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


def test_classify_kotlin_sources_jar(make_jar):
    jar = make_jar("a-sources.jar", {"com/x/A.kt": "fun a() {}"})
    assert classify_zip(jar) == ArtifactKind.SOURCES_JAR


def test_classify_kotlin_with_classes_is_archive(make_jar):
    jar = make_jar("a.jar", {"com/x/A.class": b"\xca\xfe\xba\xbe", "com/x/A.kt": "fun a() {}"})
    assert classify_zip(jar) == ArtifactKind.ARCHIVE


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


def test_scan_counted_nested_names(make_jar, tmp_path: Path):
    inner = make_jar("dep.jar", {"com/d/D.class": b"d"})
    make_jar(
        "app.war",
        {
            "WEB-INF/classes/W.class": b"w",
            "WEB-INF/lib/dep.jar": inner.read_bytes(),
            "WEB-INF/web.xml": b"<web/>",
        },
        base=tmp_path / "in",
    )
    make_jar("libs/plain.jar", {"com/x/A.class": b"x"}, base=tmp_path / "in")
    arts, counts = scan_counted(tmp_path / "in")
    assert [(a.rel, a.kind) for a in arts] == [
        ("app.war", ArtifactKind.ARCHIVE),
        ("libs/plain.jar", ArtifactKind.ARCHIVE),
    ]
    assert counts == {"app.war": 1, "libs/plain.jar": 0}


def test_scan_counted_resource_only_war_counted(make_jar, tmp_path: Path):
    inner = make_jar("dep.jar", {"com/d/D.class": b"d"})
    make_jar("only-libs.war", {"WEB-INF/lib/dep.jar": inner.read_bytes()}, base=tmp_path / "in")
    arts, counts = scan_counted(tmp_path / "in")
    assert [(a.rel, a.kind) for a in arts] == [("only-libs.war", ArtifactKind.RESOURCE_ONLY)]
    assert counts == {"only-libs.war": 1}


def test_scan_counted_skips_sources_and_corrupt(make_jar, tmp_path: Path):
    make_jar(
        "lib-sources.jar",
        {"A.java": "class A {}", "vendor/tool.jar": b"z"},
        base=tmp_path / "in",
    )
    (tmp_path / "in" / "bad.jar").write_bytes(b"not a zip")
    arts, counts = scan_counted(tmp_path / "in")
    kinds = {a.rel: a.kind for a in arts}
    assert kinds["bad.jar"] == ArtifactKind.CORRUPT
    assert kinds["lib-sources.jar"] == ArtifactKind.SOURCES_JAR
    assert counts == {}


def test_scan_counted_single_file(make_jar):
    inner = make_jar("dep.jar", {"com/d/D.class": b"d"})
    war = make_jar("one.war", {"WEB-INF/lib/dep.jar": inner.read_bytes()})
    arts, counts = scan_counted(war)
    assert [(a.rel, a.kind) for a in arts] == [("one.war", ArtifactKind.RESOURCE_ONLY)]
    assert counts == {"one.war": 1}


def test_scan_input_delegates_to_scan_counted(make_jar, tmp_path: Path):
    make_jar("a.jar", {"A.class": b"x"}, base=tmp_path / "in")
    assert scan_input(tmp_path / "in") == scan_counted(tmp_path / "in")[0]


def test_classify_counted_counts_class_entries(make_jar):
    jar = make_jar("a.jar", {"com/A.class": b"x", "com/B$1.class": b"y", "r.txt": b"z"})
    assert classify_counted(jar) == (ArtifactKind.ARCHIVE, 2)


def test_classify_counted_corrupt_is_zero(tmp_path):
    bad = tmp_path / "bad.jar"
    bad.write_bytes(b"not a zip")
    assert classify_counted(bad) == (ArtifactKind.CORRUPT, 0)


def test_scan_counted_populates_artifact_classes(make_jar, tmp_path):
    input_dir = tmp_path / "in"
    make_jar("a.jar", {"com/A.class": b"x", "com/B.class": b"y"}, base=input_dir)
    (input_dir / "C.class").write_bytes(b"c")
    artifacts, _ = scan_counted(input_dir)
    by_rel = {a.rel: a for a in artifacts}
    assert by_rel["a.jar"].classes == 2
    assert by_rel["_classes"].kind is ArtifactKind.CLASS_TREE
    assert by_rel["_classes"].classes == 1
