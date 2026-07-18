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
    assert w.add_tree(t1, "a.jar") == (1, 0, [])
    assert w.add_tree(t2, "b.jar") == (1, 0, [])
    assert (tmp_path / "src/com/x/A.java").read_text() == "class A {}"


def test_merge_collision_lowest_key_wins_either_order(tmp_path: Path):
    for order in [("a/x.jar", "b/y.jar"), ("b/y.jar", "a/x.jar")]:
        root = tmp_path / order[0].replace("/", "_")
        w = MergeWriter(root / "src")
        first = tree(root / "t1", {"com/x/A.java": f"// from {order[0]}\n"})
        second = tree(root / "t2", {"com/x/A.java": f"// from {order[1]}\n"})
        _, _, c1 = w.add_tree(first, order[0])
        _, _, c2 = w.add_tree(second, order[1])
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
    assert w.add_tree(t, "app.war") == (1, 1, [])
    assert (tmp_path / "src/com/x/A.java").is_file()
    assert not (tmp_path / "src/META-INF").exists()


def test_mirror_writer_copies_everything(tmp_path: Path):
    w = MirrorWriter(tmp_path / "out")
    t = tree(tmp_path / "t", {"com/x/A.java": "class A {}", "res.properties": "k=v"})
    java, resources, collisions = w.add_tree(t, "libs/app.war!/WEB-INF/lib/dep.jar")
    assert (java, resources, collisions) == (1, 1, [])
    dest = tmp_path / "out/libs/app.war/WEB-INF/lib/dep.jar"
    assert (dest / "com/x/A.java").is_file()
    assert (dest / "res.properties").is_file()


def test_mirror_writer_skips_archive_blobs(tmp_path: Path):
    w = MirrorWriter(tmp_path / "out")
    t = tree(tmp_path / "t", {"com/x/A.java": "class A {}", "WEB-INF/lib/dep.jar": "blob-bytes"})
    java, resources, collisions = w.add_tree(t, "app.war")
    assert (java, resources, collisions) == (1, 1, [])
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
        "collisions": 0,
    }
    report = RunReport(settings={"engine": "vineflower"}, artifacts=reports, totals=totals, duration_seconds=1.5)
    parsed = json.loads(report.to_json())
    assert parsed["totals"]["artifacts"] == 5
    assert parsed["artifacts"][0]["method"] == "maven"
