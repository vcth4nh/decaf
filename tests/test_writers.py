import json
from pathlib import Path

import pytest

from decaf import casefs
from decaf.maven import MAVEN_SOURCE_SUFFIXES
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


def test_merge_keeps_the_base_version_of_a_multi_release_class(tmp_path: Path):
    """#107: CFR and JD also write META-INF/versions/N twins, and a merged tree has room for one."""
    w = MergeWriter(tmp_path / "src")
    t = tree(tmp_path / "t", {"META-INF/versions/11/com/x/A.java": "// v11", "com/x/A.java": "// base"})
    java, collisions = w.add_tree(t, "mr.jar")
    assert (tmp_path / "src/com/x/A.java").read_text() == "// base"
    assert java == 2 and collisions == [{"path": "com/x/A.java", "kept": "mr.jar", "dropped": "mr.jar",
                                         "kept_file": "com/x/A.java", "dropped_file": "META-INF/versions/11/com/x/A.java"}]


@pytest.mark.parametrize("twin_first", [True, False], ids=["twin-first", "base-first"])
def test_merge_keeps_the_base_version_from_another_tree_of_the_artifact(tmp_path: Path, twin_first):
    """Review A1: a held or retried base class comes in a later tree, and the twin of the same jar won."""
    w = MergeWriter(tmp_path / "src")
    twin = tree(tmp_path / "t1", {"META-INF/versions/11/com/x/A.java": "// v11"})
    base = tree(tmp_path / "t2", {"com/x/A.java": "// base"})
    collisions = [c for t in ((twin, base) if twin_first else (base, twin)) for c in w.add_tree(t, "mr.jar")[1]]
    assert (tmp_path / "src/com/x/A.java").read_text() == "// base"
    assert collisions == [{"path": "com/x/A.java", "kept": "mr.jar", "dropped": "mr.jar",  # as for one tree
                           "kept_file": "com/x/A.java", "dropped_file": "META-INF/versions/11/com/x/A.java"}]


def test_merge_lowest_key_wins_also_over_a_base_version(tmp_path: Path):
    """The base version wins only within one artifact: across artifacts, the lowest sort key wins."""
    w = MergeWriter(tmp_path / "src")
    w.add_tree(tree(tmp_path / "t1", {"com/x/A.java": "// base of b"}), "b.jar")
    w.add_tree(tree(tmp_path / "t2", {"META-INF/versions/11/com/x/A.java": "// v11 of a"}), "a.jar")
    assert (tmp_path / "src/com/x/A.java").read_text() == "// v11 of a"


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
    w.nest("libs/app.war!/WEB-INF/lib/dep.jar", "libs/app.war")
    java, collisions = w.add_tree(t, "libs/app.war!/WEB-INF/lib/dep.jar")
    assert (java, collisions) == (1, [])
    dest = tmp_path / "out/libs/app.war/WEB-INF/lib/dep.jar"
    assert (dest / "com/x/A.java").is_file()
    assert not (dest / "res.properties").exists()  # engine strays never land


def test_mirror_writer_puts_a_nested_archive_in_the_folder_of_its_container(tmp_path: Path):
    w = MirrorWriter(tmp_path / "out")
    w.nest("lib!/a.war!/WEB-INF/lib/b.jar", "lib!/a.war")
    w.nest("lib!/a.war!/WEB-INF/lib/b.jar!/c!/d.jar", "lib!/a.war!/WEB-INF/lib/b.jar")
    assert w.dest_for("lib!/a.war") == tmp_path / "out/lib!/a.war"  # a top-level archive keeps its rel
    assert w.dest_for("lib!/a.war!/WEB-INF/lib/b.jar!/c!/d.jar") == tmp_path / "out/lib!/a.war/WEB-INF/lib/b.jar/c!/d.jar"


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
        "partial": 0,
        "maven_sources": 1,
        "extracted": 1,
        "decompiled": 1,
        "java_files": 6,
        "resources_copied": 0,
        "collisions": 0,
        "network_misses": 0,
        "stubbed_classes": 0,
        "out_of_memory_attempts": 0,
        "checked_files": 0,
        "flagged_files": 0,
        "noted_files": 0,
        "unchecked_files": 0,
        "refused_entries": 0,
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


def test_writers_copy_groovy_and_scala_only_with_the_maven_suffixes(tmp_path: Path):
    """#180: the Maven sources of a row can be .groovy or .scala files; engine strays still never land."""
    t = tree(tmp_path / "t", {"g/G.groovy": "class G {}", "s/S.scala": "class S", "a/A.java": "class A {}"})
    assert MergeWriter(tmp_path / "m1").add_tree(t, "x.jar") == (1, [])
    assert MergeWriter(tmp_path / "m2").add_tree(t, "x.jar", suffixes=MAVEN_SOURCE_SUFFIXES) == (3, [])
    assert (tmp_path / "m2/g/G.groovy").is_file() and not (tmp_path / "m1/g/G.groovy").exists()
    assert MirrorWriter(tmp_path / "o1").add_tree(t, "x.jar") == (1, [])
    assert MirrorWriter(tmp_path / "o2").add_tree(t, "x.jar", suffixes=MAVEN_SOURCE_SUFFIXES) == (3, [])
    assert (tmp_path / "o2/x.jar/s/S.scala").is_file() and not (tmp_path / "o1/x.jar/s/S.scala").exists()


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
    w.nest("dep.jar!/lib/inner.jar", "dep.jar")
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


def test_merge_case_insensitive_records_case_clash_lowest_key_wins(tmp_path: Path):
    files = {"a/x.jar": {"p/A.java": "class A {}"}, "b/y.jar": {"p/a.java": "class a {}"}}
    for order in [("a/x.jar", "b/y.jar"), ("b/y.jar", "a/x.jar")]:
        root = tmp_path / order[0].replace("/", "_")
        w = MergeWriter(root / "src", case_insensitive=True)
        _, c1 = w.add_tree(tree(root / "t1", files[order[0]]), order[0])
        _, c2 = w.add_tree(tree(root / "t2", files[order[1]]), order[1])
        assert c1 == []
        assert c2 == [
            {"path": "p/A.java", "kept": "a/x.jar", "dropped": "b/y.jar", "dropped_path": "p/a.java"}
        ]
        assert sorted(q.name for q in (root / "src/p").iterdir()) == ["A.java"]
        assert (root / "src/p/A.java").read_text() == "class A {}"


def test_merge_case_insensitive_joins_two_normal_forms(tmp_path: Path):
    """Review low item: on APFS, \u00e9 (NFC) and e + U+0301 (NFD) are one file, so the merge records a clash."""
    w = MergeWriter(tmp_path / "src", case_insensitive=True)
    _, c1 = w.add_tree(tree(tmp_path / "t1", {"p/Caf\u00e9.java": "class A {}"}), "a/x.jar")
    _, c2 = w.add_tree(tree(tmp_path / "t2", {"p/Cafe\u0301.java": "class B {}"}), "b/y.jar")
    assert c1 == []
    assert [(c["kept"], c["dropped"]) for c in c2] == [("a/x.jar", "b/y.jar")]


def test_merge_flag_off_keeps_exact_keys(tmp_path: Path):
    if casefs.is_case_insensitive(tmp_path):
        pytest.skip("needs a case-sensitive tmp to hold both names")
    w = MergeWriter(tmp_path / "src")
    _, c1 = w.add_tree(tree(tmp_path / "t1", {"p/A.java": "class A {}"}), "a/x.jar")
    _, c2 = w.add_tree(tree(tmp_path / "t2", {"p/a.java": "class a {}"}), "b/y.jar")
    assert (c1, c2) == ([], [])
    assert sorted(q.name for q in (tmp_path / "src/p").iterdir()) == ["A.java", "a.java"]


# --- a class folder above its package root merges by package (#199) ---


def test_merge_cuts_the_root_prefix_of_a_class_folder(tmp_path: Path):
    w = MergeWriter(tmp_path / "src", roots={"_classes": {"target/classes/p/A": "target/classes/"}})
    t = tree(tmp_path / "t", {"target/classes/p/A.java": "class A {}", "other/q/B.java": "class B {}"})
    assert w.add_tree(t, "_classes") == (2, [])
    assert sorted(p.relative_to(tmp_path / "src").as_posix() for p in (tmp_path / "src").rglob("*.java")) == [
        "other/q/B.java", "p/A.java",
    ]


def test_merge_cuts_only_the_prefix_of_the_class_file_with_that_path(tmp_path: Path):
    """lib/p/L.class is the class lib.p.L, lib/q/B.class is q.B. Before, the merge cut lib/ of each path, and wrote
    src/p/L.java with package lib.p;."""
    w = MergeWriter(tmp_path / "src", roots={"_classes": {"lib/q/B": "lib/", "lib/q/B$Nested": "lib/"}})
    t = tree(tmp_path / "t", {"lib/p/L.java": "package lib.p;", "lib/q/B.java": "package q;"})
    assert w.add_tree(t, "_classes") == (2, [])
    assert sorted(p.relative_to(tmp_path / "src").as_posix() for p in (tmp_path / "src").rglob("*.java")) == [
        "lib/p/L.java", "q/B.java",
    ]


def test_merge_cuts_the_root_prefix_of_an_inner_class_file_written_apart(tmp_path: Path):
    roots = {"target/classes/p/A": "target/classes/", "target/classes/p/A$1": "target/classes/"}
    w = MergeWriter(tmp_path / "src", roots={"_classes": roots})
    t = tree(tmp_path / "t", {"target/classes/p/A.java": "class A {}", "target/classes/p/A$1.java": "class A$1 {}"})
    w.add_tree(t, "_classes")
    assert sorted(p.relative_to(tmp_path / "src").as_posix() for p in (tmp_path / "src").rglob("*.java")) == [
        "p/A$1.java", "p/A.java",
    ]


def test_merge_sees_a_versions_twin_after_the_root_prefix_cut(tmp_path: Path):
    """mr/META-INF/versions/11/p/A.java is the versions twin of mr/p/A.java (#107)."""
    stems = ("mr/META-INF/versions/11/p/A", "mr/p/A", "mr/META-INF/versions/11/q/B", "mr/q/B")
    w = MergeWriter(tmp_path / "src", roots={"_classes": dict.fromkeys(stems, "mr/")})
    twin = tree(tmp_path / "t1", {"mr/META-INF/versions/11/p/A.java": "// v11"})
    base = tree(tmp_path / "t2", {"mr/p/A.java": "// base"})
    collisions = [c for t in (twin, base) for c in w.add_tree(t, "_classes")[1]]
    assert (tmp_path / "src/p/A.java").read_text() == "// base"
    assert collisions == [{"path": "p/A.java", "kept": "_classes", "dropped": "_classes",
                           "kept_file": "mr/p/A.java", "dropped_file": "mr/META-INF/versions/11/p/A.java"}]
    both = tree(tmp_path / "t3", {"mr/META-INF/versions/11/q/B.java": "// v11", "mr/q/B.java": "// base"})
    w.add_tree(both, "_classes")
    assert (tmp_path / "src/q/B.java").read_text() == "// base"


@pytest.mark.parametrize("order", [("alice", "bob"), ("bob", "alice")])
def test_merge_keeps_the_first_engine_path_of_two_classes_with_one_name(tmp_path: Path, order):
    """alice/Top.class and bob/Top.class are two classes Top of one class folder. Before, the file of the engine run
    that came first stayed, and the entry did not name the two files."""
    w = MergeWriter(tmp_path / "src", roots={"_classes": {"alice/Top": "alice/", "bob/Top": "bob/"}})
    collisions = []
    for name in order:
        collisions += w.add_tree(tree(tmp_path / name, {f"{name}/Top.java": f"// {name}"}), "_classes")[1]
    assert (tmp_path / "src/Top.java").read_text() == "// alice"
    assert collisions == [{"path": "Top.java", "kept": "_classes", "dropped": "_classes",
                           "kept_file": "alice/Top.java", "dropped_file": "bob/Top.java"}]


def test_merge_keeps_the_path_of_an_artifact_without_root_prefixes(tmp_path: Path):
    w = MergeWriter(tmp_path / "src", roots={"_classes": {"target/classes/p/A": "target/classes/"}})
    t = tree(tmp_path / "t", {"target/classes/p/A.java": "class A {}", "WEB-INF/classes/q/B.java": "class B {}"})
    w.add_tree(t, "app.war")
    assert (tmp_path / "src/target/classes/p/A.java").is_file() and (tmp_path / "src/q/B.java").is_file()


def test_merge_gives_a_class_folder_and_a_jar_with_one_class_one_file(tmp_path: Path):
    """Before, the class folder wrote target/classes/p/A.java, and the jar wrote p/A.java (#199)."""
    w = MergeWriter(tmp_path / "src", roots={"_classes": {"target/classes/p/A": "target/classes/"}})
    assert w.add_tree(tree(tmp_path / "t1", {"target/classes/p/A.java": "class A {}"}), "_classes") == (1, [])
    assert w.add_tree(tree(tmp_path / "t2", {"p/A.java": "class A {}"}), "app.jar") == (1, [])  # identical
    _, collisions = w.add_tree(tree(tmp_path / "t3", {"p/A.java": "class A { /* b */ }"}), "b.jar")
    assert collisions == [{"path": "p/A.java", "kept": "_classes", "dropped": "b.jar"}]
    assert [p.relative_to(tmp_path / "src").as_posix() for p in (tmp_path / "src").rglob("*.java")] == ["p/A.java"]


def test_out_path_is_the_path_of_the_written_file(tmp_path: Path):
    """The report path of a held file (#174): below src/ with --merge, below the artifact folder in mirror mode."""
    merge = MergeWriter(tmp_path / "src", roots={"_classes": {"target/classes/p/A": "target/classes/"}})
    assert merge.out_path("_classes", "target/classes/p/A.java") == "p/A.java"
    assert merge.out_path("app.war", "WEB-INF/classes/com/x/A.java") == "com/x/A.java"
    assert merge.out_path("mr.jar", "META-INF/versions/11/com/x/A.java") == "com/x/A.java"
    assert merge.out_path("app.jar", "com/x/A.java") == "com/x/A.java"
    mirror = MirrorWriter(tmp_path / "out")
    assert mirror.out_path("app.war", "WEB-INF/classes/com/x/A.java") == "WEB-INF/classes/com/x/A.java"
    assert mirror.out_path("_classes", "target/classes/p/A.java") == "target/classes/p/A.java"


# --- the report path of a versions twin with --merge ---


def test_out_path_of_a_versions_twin_with_a_base_class_keeps_the_versions_folder(tmp_path: Path):
    """Before, the report gave com/y/S.java for the twin, and src/com/y/S.java was the base class."""
    w = MergeWriter(tmp_path / "src", roots={"_classes": {"mr/p/A": "mr/", "mr/META-INF/versions/11/p/A": "mr/"}})
    w.add_tree(tree(tmp_path / "t1", {"com/y/S.java": "// base", "META-INF/versions/11/com/y/S.java": "// v11"}), "mr.jar")
    w.add_tree(tree(tmp_path / "t2", {
        "WEB-INF/classes/q/B.java": "// base", "WEB-INF/classes/META-INF/versions/9/q/B.java": "// v9",
    }), "app.war")
    w.add_tree(tree(tmp_path / "t3", {"mr/p/A.java": "// base", "mr/META-INF/versions/11/p/A.java": "// v11"}), "_classes")
    assert w.out_path("mr.jar", "META-INF/versions/11/com/y/S.java") == "META-INF/versions/11/com/y/S.java"
    assert w.out_path("mr.jar", "com/y/S.java") == "com/y/S.java"
    assert w.out_path("app.war", "WEB-INF/classes/META-INF/versions/9/q/B.java") == "META-INF/versions/9/q/B.java"
    assert w.out_path("_classes", "mr/META-INF/versions/11/p/A.java") == "META-INF/versions/11/p/A.java"
    assert (tmp_path / "src/com/y/S.java").read_text() == "// base"


def test_out_path_of_a_versions_twin_without_a_base_class_is_the_output_path(tmp_path: Path):
    w = MergeWriter(tmp_path / "src")
    w.add_tree(tree(tmp_path / "t1", {"META-INF/versions/11/com/y/S.java": "// v11"}), "mr.jar")
    w.add_tree(tree(tmp_path / "t2", {"com/y/S.java": "// base of z"}), "z.jar")  # another artifact: the twin stays
    assert w.out_path("mr.jar", "META-INF/versions/11/com/y/S.java") == "com/y/S.java"
    assert (tmp_path / "src/com/y/S.java").read_text() == "// v11"


def test_out_path_of_a_versions_twin_in_mirror_mode_is_the_engine_path(tmp_path: Path):
    mirror = MirrorWriter(tmp_path / "out")
    mirror.add_tree(tree(tmp_path / "t", {"com/y/S.java": "// base", "META-INF/versions/11/com/y/S.java": "// v11"}), "mr.jar")
    assert mirror.out_path("mr.jar", "META-INF/versions/11/com/y/S.java") == "META-INF/versions/11/com/y/S.java"
    assert mirror.out_path("mr.jar", "com/y/S.java") == "com/y/S.java"
