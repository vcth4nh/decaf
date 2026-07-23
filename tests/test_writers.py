import json
from pathlib import Path

from decaf.pipeline import (
    ArtifactReport,
    MergeWriter,
    MirrorWriter,
    RunReport,
    compute_totals,
    normalize_java_rel,
)


def tree(base: Path, files: dict[str, str]) -> Path:
    for rel, content in files.items():
        p = base / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content)
    return base


def test_normalize_java_rel():
    assert normalize_java_rel("com/x/A.java") == "com/x/A.java"
    assert normalize_java_rel("WEB-INF/classes/com/x/A.java") == "com/x/A.java"
    assert normalize_java_rel("BOOT-INF/classes/com/x/A.java") == "com/x/A.java"
    assert normalize_java_rel("META-INF/versions/9/com/x/A.java") == "com/x/A.java"
    assert normalize_java_rel("app.war/WEB-INF/classes/com/x/A.java") == "com/x/A.java"


def test_merge_dedupes_identical_content(tmp_path: Path):
    w = MergeWriter(tmp_path / "src")
    t1 = tree(tmp_path / "t1", {"com/x/A.java": "class A {}"})
    t2 = tree(tmp_path / "t2", {"com/x/A.java": "class A {}"})
    assert w.add_tree(t1, "a.jar") == (1, [])
    assert w.add_tree(t2, "b.jar") == (1, [])
    assert (tmp_path / "src/com/x/A.java").read_text() == "class A {}"


def test_merge_collision_lowest_key_wins_either_order(tmp_path: Path):
    for order in [("a/x.jar", "b/y.jar"), ("b/y.jar", "a/x.jar")]:
        root = tmp_path / order[0].replace("/", "_")
        w = MergeWriter(root / "src")
        first = tree(root / "t1", {"com/x/A.java": f"// from {order[0]}\n"})
        second = tree(root / "t2", {"com/x/A.java": f"// from {order[1]}\n"})
        _, c1 = w.add_tree(first, order[0])
        _, c2 = w.add_tree(second, order[1])
        assert c1 == []
        assert len(c2) == 1
        assert c2[0]["path"] == "com/x/A.java"
        assert c2[0]["kept"] == "a/x.jar"
        assert (root / "src/com/x/A.java").read_text() == "// from a/x.jar\n"


def test_merge_skips_resources_and_normalizes(tmp_path: Path):
    w = MergeWriter(tmp_path / "src")
    t = tree(
        tmp_path / "t",
        {
            "WEB-INF/classes/com/x/A.java": "class A {}",
            "META-INF/MANIFEST.MF": "Manifest-Version: 1.0",
        },
    )
    assert w.add_tree(t, "app.war") == (1, [])
    assert (tmp_path / "src/com/x/A.java").is_file()
    assert not (tmp_path / "src/META-INF").exists()


def test_mirror_writer_copies_sources_only(tmp_path: Path):
    w = MirrorWriter(tmp_path / "out")
    t = tree(tmp_path / "t", {"com/x/A.java": "class A {}", "res.properties": "k=v"})
    java, collisions = w.add_tree(t, "libs/app.war!/WEB-INF/lib/dep.jar")
    assert (java, collisions) == (1, [])
    dest = tmp_path / "out/libs/app.war/WEB-INF/lib/dep.jar"
    assert (dest / "com/x/A.java").is_file()
    assert not (dest / "res.properties").exists()  # engine strays never land


def test_mirror_writer_skips_archive_blobs(tmp_path: Path):
    w = MirrorWriter(tmp_path / "out")
    t = tree(tmp_path / "t", {"com/x/A.java": "class A {}", "WEB-INF/lib/dep.jar": "blob-bytes"})
    java, collisions = w.add_tree(t, "app.war")
    assert (java, collisions) == (1, [])
    assert not (tmp_path / "out/app.war/WEB-INF/lib/dep.jar").exists()


def test_report_json_and_totals(tmp_path: Path):
    reports = [
        ArtifactReport(rel="a.jar", kind="archive", outcome="ok", method="maven", java_files=3),
        ArtifactReport(rel="b.jar", kind="archive", outcome="ok", method="cfr", java_files=2),
        ArtifactReport(rel="s.jar", kind="sources_jar", outcome="ok", method="extracted", java_files=1),
        ArtifactReport(rel="r.jar", kind="resource_only", outcome="skipped"),
        ArtifactReport(rel="x.jar", kind="archive", outcome="failed", failure="all engines failed"),
    ]
    totals = compute_totals(reports)
    assert totals == {
        "artifacts": 5,
        "ok": 3,
        "failed": 1,
        "skipped": 1,
        "maven_sources": 1,
        "extracted": 1,
        "decompiled": 1,
        "java_files": 6,
        "resources_copied": 0,
        "collisions": 0,
        "network_misses": 0,
    }
    report = RunReport(settings={"engine": "vineflower"}, artifacts=reports, totals=totals, duration_seconds=1.5)
    parsed = json.loads(report.to_json())
    assert parsed["totals"]["artifacts"] == 5
    assert parsed["artifacts"][0]["method"] == "maven"


def test_merge_writer_accepts_kotlin(tmp_path):
    tree = tmp_path / "tree"
    (tree / "com").mkdir(parents=True)
    (tree / "com/A.kt").write_text("fun a() {}")
    (tree / "com/B.java").write_text("class B {}")
    (tree / "com/data.bin").write_bytes(b"\x00")
    w = MergeWriter(tmp_path / "src")
    java, collisions = w.add_tree(tree, "a.jar")
    assert (java, collisions) == (2, [])
    assert (tmp_path / "src/com/A.kt").is_file()
    assert (tmp_path / "src/com/B.java").is_file()


def test_mirror_writer_counts_kotlin_as_source(tmp_path):
    tree = tmp_path / "tree"
    (tree / "com").mkdir(parents=True)
    (tree / "com/A.kt").write_text("fun a() {}")
    (tree / "com/notes.txt").write_text("x")
    w = MirrorWriter(tmp_path / "out")
    java, _ = w.add_tree(tree, "a.jar")
    assert java == 1
    assert (tmp_path / "out/a.jar/com/A.kt").is_file()
    assert not (tmp_path / "out/a.jar/com/notes.txt").exists()


def test_mirror_add_resources_extracts_non_class_non_archive(make_jar, tmp_path: Path):
    jar = make_jar("a.jar", {
        "META-INF/MANIFEST.MF": "m",
        "res/config.properties": "k=v",
        "com/x/A.class": b"bytes",
        "lib/inner.jar": b"blob",
        "com/x/A.java": "class A {}",  # stray source inside a binary jar: excluded
    })
    w = MirrorWriter(tmp_path / "out")
    assert w.add_resources(jar, "a.jar") == (2, 0)
    dest = tmp_path / "out/a.jar"
    assert (dest / "META-INF/MANIFEST.MF").is_file()
    assert (dest / "res/config.properties").is_file()
    assert not (dest / "com/x/A.class").exists()
    assert not (dest / "lib/inner.jar").exists()
    assert not (dest / "com/x/A.java").exists()


def test_mirror_add_resources_include_sources(make_jar, tmp_path: Path):
    jar = make_jar("k.jar", {"com/x/A.kt": "fun a() {}", "META-INF/MANIFEST.MF": "m"})
    w = MirrorWriter(tmp_path / "out")
    assert w.add_resources(jar, "k.jar", include_sources=True) == (2, 0)
    assert (tmp_path / "out/k.jar/com/x/A.kt").is_file()


def test_mirror_add_resources_disabled_counts_only(make_jar, tmp_path: Path):
    jar = make_jar("a.jar", {"res.properties": "k=v"})
    w = MirrorWriter(tmp_path / "out", resources=False)
    assert w.add_resources(jar, "a.jar") == (0, 1)
    assert not (tmp_path / "out/a.jar").exists()


def test_mirror_add_resources_corrupt_member_degrades_to_zero(make_jar, tmp_path: Path):
    jar = make_jar("a.jar", {"com/x/A.class": b"x", "big.properties": b"A" * 1024})
    jar.write_bytes(jar.read_bytes().replace(b"A" * 64, b"B" * 64, 1))
    assert MirrorWriter(tmp_path / "out").add_resources(jar, "a.jar") == (0, 0)


def test_merge_add_resources_counts_without_writing(make_jar, tmp_path: Path):
    jar = make_jar("a.jar", {"res.properties": "k=v", "com/x/A.class": b"b"})
    w = MergeWriter(tmp_path / "src")
    assert w.add_resources(jar, "a.jar") == (0, 1)
    assert not (tmp_path / "src").exists()


def test_add_resources_unreadable_zip_is_zero(tmp_path: Path):
    bad = tmp_path / "bad.jar"
    bad.write_bytes(b"junk")
    assert MirrorWriter(tmp_path / "out").add_resources(bad, "bad.jar") == (0, 0)
    assert MergeWriter(tmp_path / "src").add_resources(bad, "bad.jar") == (0, 0)


def test_mirror_add_blob_writes_member_as_file(make_jar, tmp_path: Path):
    inner = make_jar("inner.jar", {"com/i/I.class": b"i"})
    parent = make_jar("dep.jar", {"lib/inner.jar": inner.read_bytes()})
    w = MirrorWriter(tmp_path / "out")
    assert w.add_blob(parent, "lib/inner.jar", "dep.jar!/lib/inner.jar") == (1, 0)
    blob = tmp_path / "out/dep.jar/lib/inner.jar"
    assert blob.is_file()
    assert blob.read_bytes() == inner.read_bytes()


def test_add_blob_merge_and_disabled_count_only(make_jar, tmp_path: Path):
    parent = make_jar("dep.jar", {"lib/inner.jar": b"blob"})
    assert MergeWriter(tmp_path / "src").add_blob(parent, "lib/inner.jar", "dep.jar!/lib/inner.jar") == (0, 1)
    w = MirrorWriter(tmp_path / "out", resources=False)
    assert w.add_blob(parent, "lib/inner.jar", "dep.jar!/lib/inner.jar") == (0, 1)
    assert not (tmp_path / "out/dep.jar/lib/inner.jar").exists()


def test_mirror_add_blob_missing_member_is_zero(make_jar, tmp_path: Path):
    parent = make_jar("dep.jar", {"other.txt": "x"})
    assert MirrorWriter(tmp_path / "out").add_blob(parent, "lib/inner.jar", "dep.jar!/lib/inner.jar") == (0, 0)


def test_mirror_add_blob_unreadable_zip_is_zero(tmp_path: Path):
    bad = tmp_path / "bad.jar"
    bad.write_bytes(b"junk")
    w = MirrorWriter(tmp_path / "out")
    assert w.add_blob(bad, "lib/inner.jar", "dep.jar!/lib/inner.jar") == (0, 0)
    assert not (tmp_path / "out/dep.jar/lib/inner.jar").exists()
